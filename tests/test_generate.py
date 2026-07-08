"""generate.py 검증: 중복 방지(문자·조합), 트렌드 주입, 이벤트 인텐트, 프롬프트 구성."""
from __future__ import annotations

import random
import threading

import pytest

import db
import generate as gen
from helpers import question

EVENT_INTENTS = {"typhoon_track", "typhoon_impact", "hot_item_where", "ipo_schedule"}


def jac(a: str, b: str) -> float:
    ta, tb = gen._trigrams(a), gen._trigrams(b)
    return len(ta & tb) / len(ta | tb)


def combo_keys(records: list[dict]) -> list[str]:
    return [gen.combo_key(r["intent"], r["slots"]) for r in records]


# ---------------------------------------------------------------
# 문자 유사도의 한계(기록용)와 조합 중복 회피
# ---------------------------------------------------------------

def test_trigram_jaccard_misses_paraphrases():
    """짧은 한국어 질문에서 문자 검사(0.85)가 왜 준중복을 통과시키는지의 증거.
    이 한계 때문에 '같은 주제 반복' 차단은 조합 회피가 담당한다."""
    assert jac("디딤돌대출 조건 뭐야?", "디딤돌대출 조건은 뭐야?") < gen.DEDUP_JACCARD
    assert jac("주택청약 1순위 조건은 뭐야?", "주택청약 1순위 자격이 뭐야?") < gen.DEDUP_JACCARD


def test_combo_dedup_within_run():
    r = gen.generate(count=30, domains=["finance"], mode="template", seed=1)
    ks = combo_keys(r)
    assert len(ks) == len(set(ks)), "실행 내 (인텐트, 엔티티) 조합 중복"


def test_combo_and_text_dedup_across_runs():
    r1 = gen.generate(count=15, domains=["finance"], mode="template", seed=1)
    r2 = gen.generate(count=15, domains=["finance"], mode="template", seed=1,
                      existing_questions=[r["question"] for r in r1],
                      existing_combos=set(combo_keys(r1)))
    assert not set(combo_keys(r1)) & set(combo_keys(r2)), "실행 간 조합 중복"
    texts = [r["question"] for r in r1 + r2]
    assert len(set(map(gen._normalize, texts))) == 30, "실행 간 문자 중복"


def test_combo_exhaustion_allows_repeats_without_hang(monkeypatch):
    tiny_taxonomy = {
        "styles": {"formality": ["반말"]},
        "domains": {"mini": {"name": "미니", "intents": [{
            "id": "cond", "name": "조건", "slots": ["term"],
            "templates": ["{term} 조건이 뭐야?", "{term} 자격 알려줘",
                          "{term} 조건 어떻게 돼?", "{term} 받으려면 뭐가 필요해?",
                          "{term} 신청 자격이 궁금해"],
        }]}},
    }
    monkeypatch.setattr(gen, "load_taxonomy", lambda: tiny_taxonomy)
    monkeypatch.setattr(gen, "load_seeds", lambda: {"term": ["A상품", "B상품"]})
    r = gen.generate(count=5, domains=["mini"], mode="template", seed=3)
    assert len(r) == 5                                  # 소진돼도 멈추지 않음
    assert len(set(combo_keys(r))) == 2                 # 조합 공간을 다 쓴 뒤 반복
    assert len({x["question"] for x in r}) == 5         # 반복 조합도 문구는 다름


# ---------------------------------------------------------------
# 유사 질문 경고 (직접 추가용, 낮은 임계값)
# ---------------------------------------------------------------

def test_similar_questions_warning_threshold():
    past = ["가평역 근처 브런치 되는 카페 있어?", "판교역 앞 약국 어디야?"]
    hits = gen.similar_questions("가평역 근처에 브런치 되는 카페 있어", past)
    assert hits and hits[0] == "가평역 근처 브런치 되는 카페 있어?"
    assert not gen.similar_questions("서울 내일 미세먼지 어때?", past)


# ---------------------------------------------------------------
# 트렌드 주입·태깅·이벤트 인텐트
# ---------------------------------------------------------------

TREND = {"typhoon_name": ["바비"], "ipo_stock": ["가상IPO"], "hot_item": ["두쫀쿠"],
         "region": ["울릉도"]}


def test_trend_ratio_extremes():
    r1 = gen.generate(count=4, domains=["weather"], mode="template", seed=7,
                      trend_pools={"region": ["울릉도"]}, trend_ratio=1.0)
    assert all("울릉도" in r["question"] and r["seed_origin"] == "trend" for r in r1)
    r0 = gen.generate(count=4, domains=["weather"], mode="template", seed=7,
                      trend_pools={"region": ["울릉도"]}, trend_ratio=0.0)
    assert all(r["seed_origin"] == "base" for r in r0)


def test_event_intents_closed_without_seeds():
    pools, tax = gen.load_seeds(), gen.load_taxonomy()
    rng = random.Random(0)
    for d in ("weather", "local", "finance"):
        for _ in range(200):
            assert gen.sample_combo(d, tax, pools, rng)["intent"] not in EVENT_INTENTS


def test_event_intents_open_with_seeds_and_tagged():
    r = gen.generate(count=18, domains=["weather", "local", "finance"], mode="template",
                     seed=7, trend_pools=TREND, trend_ratio=0.4)
    ev = [x for x in r if x["intent"] in EVENT_INTENTS]
    assert ev, "이벤트 인텐트가 안 열림"
    for x in ev:
        assert x["seed_origin"] == "trend"
        assert any(v in x["question"] for v in ("바비", "가상IPO", "두쫀쿠"))
    ks = combo_keys(r)
    assert len(ks) == len(set(ks))


def test_typhoon_impact_mixes_trend_and_base_slots():
    pools, tax = gen.load_seeds(), gen.load_taxonomy()
    rng = random.Random(2)
    for _ in range(500):
        c = gen.sample_combo("weather", tax, pools, rng,
                             {"typhoon_name": ["바비"]}, trend_ratio=0.0)
        if c["intent"] == "typhoon_impact":
            assert c["slots"]["typhoon_name"] == "바비"     # 트렌드 전용 → 강제 주입
            assert c["slots"]["region"] in pools["region"]  # ratio=0 → 기본 풀
            return
    raise AssertionError("typhoon_impact가 500회 안에 안 뽑힘")


# ---------------------------------------------------------------
# 프롬프트 구성 (few-shot / 스펙 라인)
# ---------------------------------------------------------------

def test_batch_prompt_renders_manual_exemplar_naturally():
    combo = {"domain_name": "날씨", "intent_name": "미세먼지",
             "slots": {"region": "부여"}, "style": {"formality": "반말"}}
    assert gen.build_batch_prompt([combo]) == "[스펙 1] 날씨/미세먼지 | 필수 포함: 부여 | 말투: 반말"

    manual = {"domain_name": "로컬", "intent_name": "직접 작성",
              "slots": {"entity1": "가평역", "entity2": "브런치"}, "style": {},
              "question": "가평역 근처 브런치 되는 카페 있어?"}
    user, assistant = gen.build_few_shot([manual] + gen.DEFAULT_EXEMPLARS[:2])
    assert "필수 포함: 가평역, 브런치" in user and "말투: 자유" in user
    assert "필수 포함:  " not in user and "필수 포함: |" not in user
    assert manual["question"] in assistant


# ---------------------------------------------------------------
# 증분 저장(on_record): 중단돼도 보존, 이어서 생성 시 중복 없음
# ---------------------------------------------------------------

class _Abort(Exception):
    pass


def test_on_record_preserves_progress_on_abort():
    saved = {"n": 0}

    def on_record(rec):
        saved["n"] += db.insert_questions([rec])
        if saved["n"] == 3:
            raise _Abort  # 페이지 이동 등으로 실행이 중단되는 상황 재현

    with pytest.raises(_Abort):
        gen.generate(count=10, domains=["weather"], mode="template", seed=5,
                     on_record=on_record)
    assert saved["n"] == 3 and len(db.all_question_texts()) == 3

    gen.generate(count=7, domains=["weather"], mode="template", seed=5,
                 existing_questions=db.all_question_texts(),
                 existing_combos={gen.combo_key(i, s)
                                  for i, s in db.all_question_combo_pairs()},
                 on_record=lambda r: db.insert_questions([r]))
    texts = db.all_question_texts()
    assert len(texts) == 10 and len(set(texts)) == 10


# ---------------------------------------------------------------
# 동시성: 백그라운드 생성(스레드 저장)과 메인 스레드 검수가 같은 DB를 공유
# ---------------------------------------------------------------

def test_concurrent_generation_and_review():
    errors: list[str] = []

    def worker() -> None:
        try:
            gen.generate(count=120, domains=["weather", "local", "finance"],
                         mode="template", seed=11,
                         on_record=lambda r: db.insert_questions([r]))
        except Exception as e:  # noqa: BLE001
            errors.append(f"worker: {e}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    reviewed = 0
    while t.is_alive():
        try:
            db.status_counts()
            db.pending_queue()
            db.curation_df()
            row = db.next_pending()
            if row and reviewed < 5:
                db.save_review(row["id"], "응답", "pass", "", "", "pass", "", "",
                               f"s{reviewed}", "t", "cbt")
                reviewed += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"main: {e}")
            break
    t.join(30)
    assert not errors, errors
    counts = db.status_counts()
    assert counts["pending"] + counts["done"] == 120, counts  # 유실 없음
    assert counts["done"] == reviewed, counts                 # 동시 검수 저장 정합


# ---------------------------------------------------------------
# localhost 프록시 우회
# ---------------------------------------------------------------

def test_ollama_calls_bypass_system_proxy(monkeypatch):
    assert gen._session.trust_env is False
    # 가짜 프록시 환경에서도 localhost 확인이 지연 없이 끝나야 한다
    monkeypatch.setenv("HTTP_PROXY", "http://10.255.255.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.255.255.1:9999")
    import time
    t0 = time.time()
    gen.check_ollama(gen.DEFAULT_HOST)  # 연결 여부와 무관하게 즉시 반환이면 통과
    assert time.time() - t0 < 1.5


# ---------------------------------------------------------------
# 트렌드 시드 후보 추출 (Ollama 필요 — 미연결 시 스킵)
# ---------------------------------------------------------------

@pytest.mark.skipif(not gen.check_ollama(gen.DEFAULT_HOST), reason="Ollama 미연결")
def test_extract_trend_candidates_live():
    text = ("성수동 베이커리마다 두쫀쿠(두바이 쫀득 쿠키)를 사려는 줄이 이어졌다. "
            "연남동에서도 버터떡 가게가 늘고 있다. ※ 지금 구독하고 뉴스레터를 받아보세요!")
    guide = {"hot_item": "지금 유행하는 음식·디저트 품목 (예: 두쫀쿠)",
             "anchor": "로컬 질문의 기준점이 되는 역·동네·상권 이름"}
    cands = gen.extract_trend_candidates(text, guide)
    assert isinstance(cands, list)
    for c in cands:
        assert c["pool"] in guide and 0 < len(c["value"]) <= 30
