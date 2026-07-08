"""
generate.py — 경량 LLM 평가용 한국어 질문지 생성 파이프라인

구조: 시드 샘플링(코드) -> 문장화(LLM 또는 템플릿) -> 검증 -> JSONL
- LLM은 '한국 지식'이 아니라 '한국어 작문'만 담당한다.
- Ollama가 없어도 템플릿 모드로 동작한다. (mode=auto가 자동 판단)

CLI 예시:
    python generate.py --count 100 --domain all --out data/questions.jsonl
    python generate.py --count 30 --domain finance --mode template
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
TAXONOMY_PATH = BASE_DIR / "taxonomy.yaml"
SEEDS_PATH = BASE_DIR / "seeds.yaml"

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:4b"

# localhost(Ollama) 호출 전용 세션. trust_env=False로 시스템 프록시/WPAD를 우회한다 —
# 윈도우에서 프록시 설정이 있으면 localhost 요청조차 프록시를 타려다 수 초씩 지연될 수 있음.
_session = requests.Session()
_session.trust_env = False

MIN_LEN, MAX_LEN = 6, 90        # 질문 길이 허용 범위(문자)
DEDUP_JACCARD = 0.85            # 문자 trigram 유사도 임계값
DEFAULT_BATCH = 8               # LLM 호출 1회당 생성 질문 수 (고정 프롬프트 prefill 비용을 N분의 1로)
KEEP_ALIVE = "30m"              # 호출 간 모델 메모리 상주 시간 (재로딩 방지)


# ---------------------------------------------------------------
# 설정 로드
# ---------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seeds() -> dict[str, list[str]]:
    """seeds.yaml 로드. {include: [...]} 형태의 합성 풀을 해석한다."""
    raw = load_yaml(SEEDS_PATH)
    pools: dict[str, list[str]] = {}
    pending = dict(raw)
    # include가 참조하는 풀이 먼저 풀릴 때까지 반복
    for _ in range(10):
        for key, val in list(pending.items()):
            if isinstance(val, list):
                pools[key] = [str(v) for v in val]
                pending.pop(key)
            elif isinstance(val, dict) and "include" in val:
                refs = val["include"]
                if all(r in pools for r in refs):
                    merged: list[str] = []
                    for r in refs:
                        merged.extend(pools[r])
                    pools[key] = merged
                    pending.pop(key)
        if not pending:
            break
    if pending:
        raise ValueError(f"seeds.yaml include 해석 실패: {list(pending)}")
    return pools


def load_taxonomy() -> dict:
    return load_yaml(TAXONOMY_PATH)


# ---------------------------------------------------------------
# 슬롯 조합 샘플링
# ---------------------------------------------------------------

def combo_key(intent: str, slots: dict[str, str]) -> str:
    """(인텐트, 엔티티 값들)로 만든 조합 식별자.
    문자 유사도 검사는 조사 하나만 달라도 통과시키므로(짧은 문장에서 trigram 급락),
    '같은 것을 또 묻는' 중복은 문구가 아니라 이 조합 단위로 회피한다."""
    return intent + "|" + "|".join(sorted(slots.values()))

def sample_combo(domain_key: str, taxonomy: dict, pools: dict, rng: random.Random,
                 trend_pools: dict[str, list[str]] | None = None,
                 trend_ratio: float = 0.0) -> dict:
    """슬롯 조합 샘플링. trend_pools에 해당 슬롯 값이 있으면 trend_ratio 확률로
    트렌드 시드에서 뽑고, 하나라도 쓰이면 seed_origin='trend'로 태깅한다.

    기본 풀이 빈 슬롯(seeds.yaml의 트렌드 전용 풀, 예: typhoon_name)은 트렌드 시드가
    있을 때만 그 인텐트가 후보에 들어가고, 값은 비율과 무관하게 트렌드에서 뽑는다.
    """
    domain = taxonomy["domains"][domain_key]
    tp = trend_pools or {}

    def intent_available(it: dict) -> bool:
        for s in it["slots"]:
            if s not in pools:
                raise KeyError(f"seeds.yaml에 풀이 없음: {s}")
            if not pools[s] and not tp.get(s):
                return False  # 트렌드 전용 풀인데 시드가 없음(만료 등) → 인텐트 비활성
        return True

    available = [it for it in domain["intents"] if intent_available(it)]
    if not available:
        raise ValueError(f"도메인 {domain_key}에 값을 채울 수 있는 인텐트가 없음")
    intent = rng.choice(available)

    slots: dict[str, str] = {}
    trend_used = False
    for slot in intent["slots"]:
        trend_vals = tp.get(slot) or []
        base_vals = pools[slot]
        # 기본 풀이 비어 있으면(트렌드 전용) 무조건 트렌드에서
        pick_trend = bool(trend_vals) and (not base_vals or rng.random() < trend_ratio)
        pool_vals = trend_vals if pick_trend else base_vals
        value = rng.choice(pool_vals)
        # 'xxx2' 슬롯은 같은 계열 풀에서 중복 없이 뽑는다 (예: fin_product2)
        base = slot[:-1] if slot.endswith("2") else None
        if base and base in slots:
            candidates = [v for v in pool_vals if v != slots[base]]
            value = rng.choice(candidates) if candidates else value
        if pick_trend:
            trend_used = True
        slots[slot] = value

    styles = taxonomy.get("styles", {})
    style = {axis: rng.choice(options) for axis, options in styles.items()}

    return {
        "domain": domain_key,
        "domain_name": domain.get("name", domain_key),
        "intent": intent["id"],
        "intent_name": intent.get("name", intent["id"]),
        "slots": slots,
        "style": style,
        "templates": intent.get("templates", []),
        "seed_origin": "trend" if trend_used else "base",
    }


# ---------------------------------------------------------------
# 문장화 (1) 템플릿 모드
# ---------------------------------------------------------------

def render_template(combo: dict, rng: random.Random) -> str:
    templates = combo["templates"]
    if not templates:
        raise ValueError(f"인텐트 {combo['intent']}에 템플릿이 없음")
    tpl = rng.choice(templates)
    return tpl.format(**combo["slots"]).strip()


# ---------------------------------------------------------------
# 문장화 (2) LLM(Ollama) 모드
# ---------------------------------------------------------------

SYSTEM_PROMPT = (
    "너는 한국 사용자가 AI 비서 앱에 실제로 입력할 법한 질문을 만드는 작성기다.\n"
    "번호가 매겨진 [스펙]이 여러 개 주어진다. 스펙마다 질문(또는 요청) 한 문장씩 만든다.\n"
    "규칙:\n"
    "1) 각 스펙의 '필수 포함' 값을 표기 그대로 문장에 포함할 것 (뒤에 조사를 붙이는 것은 허용).\n"
    "2) 각 스펙의 말투 지시를 따를 것.\n"
    "3) 스펙끼리 서로 다른 문형을 쓸 것. 같은 틀의 반복 금지.\n"
    "4) 한국어만 쓸 것. 영어 단어·한자·일본어 표기 금지 (필수 포함 값에 있는 영문 고유명사는 예외).\n"
    "5) 자연스러운 한국어 문장일 것. 어색한 어순이나 비문 금지.\n"
    "6) 질문에 답하지 말 것. 설명을 덧붙이지 말 것.\n"
    '7) 반드시 JSON 한 개로만 출력하고 배열 순서는 스펙 번호 순서와 같게: {"questions": ["...", "..."]}'
)

# 검수 데이터가 쌓이기 전에 쓰는 기본 few-shot 예시
DEFAULT_EXEMPLARS = [
    {
        "domain_name": "날씨", "intent_name": "미세먼지",
        "slots": {"region": "부여", "date_ref": "오늘"},
        "style": {"formality": "반말", "completeness": "완전한 문장"},
        "question": "부여 오늘 미세먼지 나빠?",
    },
    {
        "domain_name": "로컬", "intent_name": "맛집 추천",
        "slots": {"anchor": "가평역", "food_category": "한식집",
                  "local_constraint": "오전 11시 전에 식사 가능한"},
        "style": {"formality": "반말", "completeness": "완전한 문장"},
        "question": "가평역 근처 한식집 추천, 오전 11시 전에 식사 가능한 곳이 있을까?",
    },
    {
        "domain_name": "금융", "intent_name": "납부 시기·기한",
        "slots": {"fin_pay_term": "중도상환수수료"},
        "style": {"formality": "반말", "completeness": "조사를 생략한 짧은 검색어풍"},
        "question": "중도상환수수료 언제까지 내야 해?",
    },
]


def build_few_shot(exemplars: list[dict]) -> tuple[str, str]:
    """예시 목록으로 few-shot 한 쌍(user, assistant)을 구성.
    검수를 통과한 실제 질문이 쌓이면 그걸 예시로 써서 품질이 점점 좋아진다."""
    user = build_batch_prompt(exemplars)
    assistant = json.dumps(
        {"questions": [e["question"] for e in exemplars]}, ensure_ascii=False)
    return user, assistant


def build_batch_prompt(combos: list[dict]) -> str:
    """스펙 라인 구성. 슬롯/말투가 빈 스펙(직접 작성 질문 예시)도 자연스럽게 렌더링한다."""
    lines = []
    for i, c in enumerate(combos, 1):
        parts = [f"{c['domain_name']}/{c['intent_name']}"]
        required = ", ".join(c["slots"].values())
        if required:
            parts.append(f"필수 포함: {required}")
        parts.append(f"말투: {', '.join(c['style'].values()) or '자유'}")
        lines.append(f"[스펙 {i}] " + " | ".join(parts))
    return "\n".join(lines)


def check_ollama(host: str) -> bool:
    try:
        r = _session.get(f"{host}/api/tags", timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False


def call_ollama_batch(combos: list[dict], host: str, model: str, temperature: float,
                      few_shot: tuple[str, str] | None = None) -> list[str]:
    """한 번의 호출로 len(combos)개 질문 생성. 실패 항목은 ''로 채워 길이를 맞춘다."""
    fs_user, fs_assistant = few_shot or build_few_shot(DEFAULT_EXEMPLARS)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": fs_user},
        {"role": "assistant", "content": fs_assistant},
        {"role": "user", "content": build_batch_prompt(combos)},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "think": False,            # qwen3 thinking 비활성화 (미지원 모델은 Ollama가 무시)
        "keep_alive": KEEP_ALIVE,  # 호출 간 모델 재로딩 방지
        "options": {
            "temperature": temperature,
            "num_predict": 100 * len(combos) + 50,  # 출력 폭주 방지 상한
        },
    }
    r = _session.post(f"{host}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    content = r.json().get("message", {}).get("content", "")
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, flags=re.S)
        try:
            data = json.loads(m.group()) if m else {}
        except json.JSONDecodeError:
            data = {}
    qs = data.get("questions", [])
    if not isinstance(qs, list):
        qs = []
    qs = [str(q).strip() if q else "" for q in qs]
    qs += [""] * (len(combos) - len(qs))  # 부족분 패딩
    return qs[: len(combos)]


# ---------------------------------------------------------------
# 트렌드 시드 후보 추출 (기사/리뷰 붙여넣기 → 엔티티만 뽑기)
# ---------------------------------------------------------------

EXTRACT_SYSTEM_TMPL = (
    "너는 한국어 기사·리뷰 본문에서 AI 비서 평가 질문에 쓸 '최신 엔티티'를 뽑는 추출기다.\n"
    "규칙:\n"
    "1) 본문에 실제로 등장한 표현만 뽑는다. 지어내거나 일반화하지 말 것.\n"
    "2) 광고·구독 안내·기자 이름·언론사명·메뉴/배너 문구는 무시한다.\n"
    "3) 각 항목은 아래 풀 중 정확히 하나에 배정한다:\n{pool_desc}\n"
    "4) 어울리는 풀이 없으면 그 항목은 버린다. 값은 30자 이내 명사구.\n"
    '5) JSON 한 개로만 출력: {{"candidates": [{{"value": "...", "pool": "..."}}]}}'
)


def extract_trend_candidates(text: str, pool_guide: dict[str, str],
                             host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                             max_chars: int = 3500) -> list[dict]:
    """붙여넣은 기사/리뷰 본문에서 트렌드 시드 후보를 추출한다 (Ollama 필요).

    pool_guide: {풀 이름: 설명}. 반환: [{"value": ..., "pool": ...}] — pool은
    pool_guide에 있는 것만, 값은 정제·중복 제거됨. 본문 원문은 저장하지 않는다.
    본문은 max_chars에서 자른다 (CPU prefill 비용 억제).
    """
    pool_desc = "\n".join(f"- {p}: {d}" for p, d in pool_guide.items())
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM_TMPL.format(pool_desc=pool_desc)},
            {"role": "user", "content": text[:max_chars]},
        ],
        "stream": False,
        "format": "json",
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.2, "num_predict": 800},
    }
    r = _session.post(f"{host}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    content = r.json().get("message", {}).get("content", "")
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, flags=re.S)
        try:
            data = json.loads(m.group()) if m else {}
        except json.JSONDecodeError:
            data = {}
    cands = data.get("candidates")
    if not isinstance(cands, list):
        cands = []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in cands:
        if not isinstance(c, dict):
            continue
        value = str(c.get("value", "")).strip()
        pool = str(c.get("pool", "")).strip()
        if not value or len(value) > 30 or pool not in pool_guide:
            continue
        if (value, pool) in seen:
            continue
        seen.add((value, pool))
        out.append({"value": value, "pool": pool})
    return out


# ---------------------------------------------------------------
# 검증
# ---------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"[\s\?\.,!~]", "", text)


def _trigrams(text: str) -> set[str]:
    t = _normalize(text)
    return {t[i:i + 3] for i in range(max(len(t) - 2, 1))}


def is_near_duplicate(question: str, accepted_trigrams: list[set[str]]) -> bool:
    tg = _trigrams(question)
    for other in accepted_trigrams:
        union = tg | other
        if union and len(tg & other) / len(union) >= DEDUP_JACCARD:
            return True
    return False


def similar_questions(question: str, candidates: list[str],
                      threshold: float = 0.5, top_k: int = 3) -> list[str]:
    """과거 질문 중 비슷한 것을 유사도 순으로 반환 (직접 추가 시 중복 검수 경고용).
    생성 중복 기준(0.85)은 거의 같은 문자열만 잡으므로, 사람 눈에 '사실상 같은 질문'을
    넓게 잡도록 임계값을 낮춰서 쓴다. 경고일 뿐 추가를 막지는 않는다."""
    tg = _trigrams(question)
    scored = []
    for c in candidates:
        other = _trigrams(c)
        union = tg | other
        score = len(tg & other) / len(union) if union else 0.0
        if score >= threshold:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]


def required_token(value: str) -> str:
    """검사용 필수 문자열. 괄호 보충어는 완화한다. 예: '골프장(필드)' -> '골프장'"""
    return value.split("(")[0].strip()


_FOREIGN_RE = re.compile(r"[一-鿿㐀-䶿぀-ヿ]")  # 한자·가나


def has_foreign_text(question: str, combo: dict) -> bool:
    """한자·가나 혼입, 또는 슬롯 값(영문 고유명사)을 제외한 로마자 혼입 검사.
    예: '더划算?', 'where 뭐야?' 를 잡아낸다."""
    if _FOREIGN_RE.search(question):
        return True
    residual = question
    for v in combo["slots"].values():
        residual = residual.replace(v, " ").replace(required_token(v), " ")
    return re.search(r"[A-Za-z]", residual) is not None


def validate(question: str, combo: dict, seen_exact: set[str], accepted_trigrams: list[set[str]]) -> str | None:
    """문제가 있으면 사유 문자열, 통과면 None."""
    if not question:
        return "빈 출력"
    if "\n" in question:
        return "여러 줄 출력"
    if not (MIN_LEN <= len(question) <= MAX_LEN):
        return f"길이 벗어남({len(question)}자)"
    for slot, value in combo["slots"].items():
        if required_token(value) not in question:
            return f"엔티티 누락: {slot}={value}"
    if not re.search(r"[가-힣]", question):
        return "한글 없음"
    if has_foreign_text(question, combo):
        return "외국어 혼입"
    if _normalize(question) in seen_exact:
        return "완전 중복"
    if is_near_duplicate(question, accepted_trigrams):
        return "준중복"
    return None


# ---------------------------------------------------------------
# 메인 생성 루프
# ---------------------------------------------------------------

def generate(
    count: int,
    domains: list[str],
    mode: str = "auto",
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    temperature: float = 0.8,
    seed: int | None = None,
    batch: int = DEFAULT_BATCH,
    exemplars: list[dict] | None = None,
    existing_questions: list[str] | None = None,
    trend_pools: dict[str, list[str]] | None = None,
    trend_ratio: float = 0.0,
    existing_combos: set[str] | None = None,
    on_progress=None,
    on_record=None,
) -> list[dict]:
    """질문 레코드 리스트를 반환. on_progress(done, total)로 진행 콜백 지원(UI용).

    exemplars: 검수를 통과한 실제 질문 레코드 목록. 주어지면 few-shot 예시로 사용되어
    사용(검수)이 쌓일수록 생성 품질이 따라 올라간다. 없으면 기본 예시 사용.

    existing_questions: 과거 실행에서 이미 생성된 질문 텍스트 목록. 주어지면 이번
    실행분이 과거분과 완전/준중복(문자 기준)되지 않도록 막는다. 단, 문자 검사는
    거의 동일한 문구만 잡으므로 '같은 주제 반복'은 existing_combos가 담당한다.

    existing_combos: 이미 다룬 (인텐트, 엔티티) 조합 키 집합 (combo_key() 형식).
    이번 실행 내 중복 포함, 이미 나온 조합은 피해서 샘플링한다 — 예: '주택청약
    1순위 조건'을 문구만 바꿔 또 묻는 것 방지. 조합 공간이 소진되면 반복 허용.

    on_record: 질문 1건이 확정될 때마다 호출되는 콜백(레코드 1개). UI에서 즉시
    저장용으로 쓰면 생성이 도중에 중단(rerun 등)되어도 그때까지의 결과가 보존된다.

    trend_pools/trend_ratio: 최신 기사·리뷰에서 추출한 트렌드 시드({풀: [값...]})와
    주입 확률. 트렌드 값이 쓰인 질문은 seed_origin='trend'로 태깅된다.
    """
    taxonomy = load_taxonomy()
    pools = load_seeds()
    rng = random.Random(seed)

    valid_domains = list(taxonomy["domains"].keys())
    if domains == ["all"]:
        domains = valid_domains
    for d in domains:
        if d not in valid_domains:
            raise ValueError(f"알 수 없는 도메인: {d} (가능: {valid_domains})")

    if mode == "auto":
        mode = "ollama" if check_ollama(host) else "template"
        print(f"[mode=auto] -> '{mode}' 모드로 진행", file=sys.stderr)

    # few-shot 구성: 검수 통과 예시가 있으면 그걸 쓰고, 3개 미만이면 기본 예시로 보충
    pool_ex = list(exemplars or [])
    if len(pool_ex) < 3:
        pool_ex += DEFAULT_EXEMPLARS
    few_shot = build_few_shot(pool_ex[:6])

    records: list[dict] = []
    # 과거 실행분을 중복 검사 기준에 미리 넣는다 — 세션을 거듭해도 준중복이 쌓이지 않게
    seen_exact: set[str] = {_normalize(q) for q in existing_questions or []}
    accepted_trigrams: list[set[str]] = [_trigrams(q) for q in existing_questions or []]
    used_combos: set[str] = set(existing_combos or ())
    failures = 0
    batch = max(1, batch)

    while len(records) < count:
        n = min(batch, count - len(records)) if mode == "ollama" else 1
        combos = []
        for i in range(n):
            domain_key = domains[(len(records) + i) % len(domains)]
            # 이미 다룬 (인텐트, 엔티티) 조합은 피해서 뽑는다. 여러 번 시도해도
            # 새 조합이 없으면(공간 소진) 반복을 허용 — 문구 중복은 문자열 검사가 차단.
            for _ in range(12):
                cand = sample_combo(domain_key, taxonomy, pools, rng,
                                    trend_pools, trend_ratio)
                if combo_key(cand["intent"], cand["slots"]) not in used_combos:
                    break
            used_combos.add(combo_key(cand["intent"], cand["slots"]))
            combos.append(cand)

        if mode == "ollama":
            try:
                questions = call_ollama_batch(combos, host, model, temperature, few_shot)
            except requests.RequestException as e:
                print(f"[warn] Ollama 호출 실패: {e}", file=sys.stderr)
                questions = [""] * len(combos)
            # 검증 탈락 항목만 모아 한 번 더 시도
            retry_idx = [
                i for i, (c, q) in enumerate(zip(combos, questions))
                if not q or validate(q, c, seen_exact, accepted_trigrams) is not None
            ]
            if retry_idx:
                try:
                    retry_qs = call_ollama_batch(
                        [combos[i] for i in retry_idx], host, model, temperature, few_shot)
                    for j, i in enumerate(retry_idx):
                        if retry_qs[j]:
                            questions[i] = retry_qs[j]
                except requests.RequestException:
                    pass
        else:
            questions = [render_template(c, rng) for c in combos]

        for combo, question in zip(combos, questions):
            if len(records) >= count:
                break
            gen_mode = mode
            if mode == "ollama" and (
                not question or validate(question, combo, seen_exact, accepted_trigrams) is not None
            ):
                question = render_template(combo, rng)  # 최후 폴백
                gen_mode = "template(fallback)"

            reason = validate(question, combo, seen_exact, accepted_trigrams)
            if reason is not None:
                failures += 1
                continue

            records.append({
                "id": uuid.uuid4().hex[:12],
                "domain": combo["domain"],
                "domain_name": combo["domain_name"],
                "intent": combo["intent"],
                "intent_name": combo["intent_name"],
                "slots": combo["slots"],
                "style": combo["style"],
                "question": question,
                "gen_mode": gen_mode,
                "model": model if gen_mode == "ollama" else None,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "seed_origin": combo.get("seed_origin", "base"),
            })
            seen_exact.add(_normalize(question))
            accepted_trigrams.append(_trigrams(question))
            if on_record:
                on_record(records[-1])
            if on_progress:
                on_progress(len(records), count)

        if failures > count * 20:
            print(f"[warn] 탈락 과다({failures}회). {len(records)}건에서 중단. "
                  f"시드/템플릿 다양성을 늘려주세요.", file=sys.stderr)
            break

    return records


def save_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="한국어 평가 질문지 생성기")
    p.add_argument("--count", type=int, default=30, help="생성할 질문 수")
    p.add_argument("--domain", default="all", help="weather,local,finance 또는 all (쉼표 구분)")
    p.add_argument("--mode", choices=["auto", "ollama", "template"], default="auto")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Ollama 모델 태그")
    p.add_argument("--host", default=DEFAULT_HOST, help="Ollama 주소")
    p.add_argument("--temperature", type=float, default=0.8)
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="LLM 호출 1회당 생성 수")
    p.add_argument("--seed", type=int, default=None, help="랜덤 시드(재현용)")
    p.add_argument("--out", default="data/questions.jsonl", help="출력 JSONL 경로(누적 append)")
    args = p.parse_args()

    domains = [d.strip() for d in args.domain.split(",") if d.strip()]

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = BASE_DIR / out_path
    # 출력 파일에 이미 쌓인 질문·조합과 중복되지 않게 (append 운용 전제)
    existing: list[str] = []
    existing_combos: set[str] = set()
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                existing.append(rec["question"])
                existing_combos.add(combo_key(rec.get("intent", ""), rec.get("slots", {})))
        print(f"기존 {len(existing)}건과 중복 방지: {out_path}", file=sys.stderr)

    records = generate(
        count=args.count, domains=domains, mode=args.mode, model=args.model,
        host=args.host, temperature=args.temperature, seed=args.seed, batch=args.batch,
        existing_questions=existing, existing_combos=existing_combos,
        on_progress=lambda d, t: print(f"\r생성 중... {d}/{t}", end="", file=sys.stderr),
    )
    print(file=sys.stderr)

    save_jsonl(records, out_path)
    print(f"{len(records)}건 저장: {out_path}", file=sys.stderr)

    # 미리보기
    for rec in records[:5]:
        print(f"  [{rec['domain_name']}/{rec['intent_name']}] {rec['question']}")


if __name__ == "__main__":
    main()
