"""
app.py — E2E 검수 도우미 (Streamlit 로컬 앱)

흐름: ① 질문 생성 → ② 복사해서 카나나 앱에 붙여넣기 → 응답/판정 기록 → ③ 집계·xlsx 추출
실행: streamlit run app.py
모든 데이터는 로컬(data/eval_assistant.db)에만 저장된다.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta
from io import BytesIO

import pandas as pd
import streamlit as st

import db
import generate as gen

st.set_page_config(page_title="E2E 검수 도우미", page_icon="✅", layout="wide")
db.init_db()

DOMAIN_LABELS = {"weather": "날씨", "local": "로컬", "finance": "금융"}
# E2E 품질평가 양식 기준: 판정은 정확도/LLM 출력 2축, 값은 pass/fail
VERDICTS = ["pass", "fail"]
FAIL_TYPES = ["최신성", "할루시네이션", "수치 오류", "조건 불일치", "사실관계 오류",
              "정보 누락", "날짜 오류", "의도와 다름", "기타"]
OUTPUT_ERROR_TYPES = ["위첨자", "마크다운 노출", "영문으로 뱉음", "응답 끊김",
                      "표현 오류", "출처없음", "기타"]
# 제출 양식 기본 컬럼 순서. 카카오 양식이 바뀌면 ③ 탭에서 줄 단위로 수정한다.
# 데이터에 없는 이름("기획 검수" 등 카카오 측 기입란)은 제목만 있는 빈 컬럼으로 내보냄.
DEFAULT_SUBMIT_LAYOUT = ["단계", "날짜", "테스터", "search ID", "검색 키워드", "1) 정확도",
                         "N 사유", "정확도 코멘트", "기획 검수", "2) LLM 출력", "오류 유형",
                         "출력 오류 코멘트", "기획 검수"]
# 검수 데이터에서 가공해 만드는 컬럼. 그 외 이름은 results_df 컬럼과 같으면 그대로 사용.
SUBMIT_DERIVED = {
    "날짜": lambda df: pd.to_datetime(df["검수일시"]).dt.strftime("%Y. %m. %d"),
}
# 트렌드 시드를 배정할 수 있는 풀과, 추출기(LLM)에 주는 풀 설명.
# typhoon_name/ipo_stock/hot_item은 트렌드 전용 — 시드가 있어야 이벤트형 인텐트가 열린다.
TREND_POOL_GUIDE = {
    "region": "날씨 질문에 쓸 국내 지역·도시 이름",
    "typhoon_name": "본문에 등장하는 태풍 이름 (예: 바비, 힌남노)",
    "anchor": "로컬 질문의 기준점이 되는 역·동네·상권 이름",
    "food_category": "음식점 종류 (예: 한식집, 브런치 카페)",
    "hot_item": "지금 유행하는 음식·디저트·상품 품목 (예: 두쫀쿠)",
    "place_category": "생활 시설·업종 이름",
    "local_constraint": "가게 이용 조건 표현 (예: 24시간 하는)",
    "stock": "국내 상장 종목명",
    "ipo_stock": "청약·상장을 앞둔 공모주 종목명",
    "fin_term": "금융 용어·제도 이름",
    "fin_condition_term": "조건·자격을 묻기 자연스러운 금융 상품·제도",
    "fin_pay_term": "납부 기한을 묻기 자연스러운 세금·수수료",
    "fin_calc_term": "금액 계산을 묻기 자연스러운 항목",
    "fin_product": "금융 상품 유형",
}

@st.cache_resource
def _llm_lock() -> threading.Lock:
    """서버 전역 LLM 작업 잠금 (생성·추출 공용). 앱 스크립트는 rerun마다 재실행되므로
    모듈 변수는 리셋된다 — cache_resource로 서버 수명 동안 하나만 유지한다.
    로컬 Ollama는 CPU 하나를 쓰는 순차 처리라, 생성과 추출이 동시에 돌면 서로의
    요청 뒤에 줄을 서며 둘 다 몇 배로 느려진다 → 상호 배제로 순차를 강제한다.
    (서로 다른 브라우저 탭의 동시 생성으로 인한 준중복 방지도 겸한다)"""
    return threading.Lock()


def _generation_worker(job: dict, params: dict, lock: threading.Lock) -> None:
    """백그라운드 생성 스레드. Streamlit 호출 금지 — rerun과 무관하게 계속 돈다.
    진행 상황은 job dict에 쓰고, 질문은 확정되는 즉시 DB에 저장한다."""

    def on_record(rec: dict) -> None:
        job["saved"] += db.insert_questions([rec])
        job["mode"] = rec["gen_mode"]
        if rec.get("seed_origin") == "trend":
            job["trend"] += 1

    def on_progress(done: int, total: int) -> None:
        job["done"] = done

    try:
        gen.generate(**params, on_record=on_record, on_progress=on_progress)
    except Exception as e:  # noqa: BLE001 — 스레드에서는 화면 대신 job에 기록
        job["status"] = "error"
        job["error"] = str(e)
    else:
        job["status"] = "done"
    finally:
        lock.release()


def _extraction_worker(job: dict, text: str, lock: threading.Lock) -> None:
    """백그라운드 트렌드 시드 추출 스레드. CPU에서 분 단위로 걸릴 수 있는 작업이라
    화면 스레드에서 떼어낸다 — 추출 중에도 페이지 이동·검수가 가능해진다."""
    try:
        cands = gen.extract_trend_candidates(text, TREND_POOL_GUIDE)
        job["cands"] = [{**c, "verbatim": c["value"] in text} for c in cands]
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)
    else:
        job["status"] = "done"
    finally:
        lock.release()


# ---------------------------------------------------------------
# 페이지 내비게이션 — 반드시 첫 위젯으로 그린다.
# st.tabs는 위젯 변경 rerun 시 활성 탭이 첫 탭으로 튕기는 문제가 있어(예: ② 탭에서
# 검수할 질문을 고르면 ① 화면으로 이동) 세션 상태에 고정되는 페이지 방식을 쓴다.
# 실행이 도중에 중단(연타·이른 st.rerun)되면 아직 안 그려진 위젯 상태가 정리될 수 있어
# ① 첫 위젯으로 배치하고 ② 위젯 정리 대상이 아닌 nav_last로 유실 시 자동 복원한다.
# ---------------------------------------------------------------
PAGES = ["① 질문 생성", "② 검수 진행", "③ 결과·내보내기", "④ 트렌드 시드"]
if "nav" not in st.session_state:  # 최초 실행 또는 위젯 상태 유실 → 마지막 페이지 복원
    st.session_state["nav"] = st.session_state.get("nav_last", PAGES[0])
# segmented_control은 rerun 뒤 값과 하이라이트가 어긋나는 사례가 있어(내용은 ①인데
# 불은 ②) 검증이 오래된 radio를 쓴다. 값-표시 동기화가 엄격해 어긋날 수 없다.
page = st.radio("페이지 이동", PAGES, key="nav", horizontal=True,
                label_visibility="collapsed")
st.session_state["nav_last"] = page

# 페이지를 오가도 작성 중이던 입력이 날아가지 않도록 위젯 상태를 고정한다.
# (렌더링되지 않은 위젯의 상태는 Streamlit이 정리해 버리므로 재대입으로 보존 표시)
_PRESERVE_KEYS = ("rv_response", "rv_search", "rv_acc", "rv_fail", "rv_acc_comment",
                  "rv_out", "rv_err", "rv_out_comment", "rv_allow_dup", "browse_pick",
                  "review_newest", "mq_text", "mq_entities", "submit_layout",
                  "trend_src", "trend_raw", "trend_val")
for _k in _PRESERVE_KEYS:
    if _k in st.session_state:
        st.session_state[_k] = st.session_state[_k]

# ---------------------------------------------------------------
# 사이드바: 검수자 / 상태
# ---------------------------------------------------------------
with st.sidebar:
    st.header("검수 설정")
    reviewer = st.text_input("테스터 이름", value=st.session_state.get("reviewer", ""))
    st.session_state["reviewer"] = reviewer
    phase = st.text_input("검수 단계 라벨", value=st.session_state.get("phase", "cbt"),
                          help="제출 양식 첫 컬럼(예: cbt)")
    st.session_state["phase"] = phase

    counts = db.status_counts()
    total = sum(counts.values())
    st.metric("전체 질문", total)
    c1, c2, c3 = st.columns(3)
    c1.metric("대기", counts["pending"])
    c2.metric("완료", counts["done"])
    c3.metric("건너뜀", counts["skipped"])
    if counts["rejected"]:
        st.caption(f"결함 제외: {counts['rejected']}건")

    if counts["skipped"] and st.button("건너뛴 항목 다시 대기열로"):
        n = db.restore_skipped()
        st.toast(f"{n}건을 대기열로 되돌렸습니다.")
        st.rerun()

    st.divider()

    # rerun(모든 클릭)마다 네트워크 확인을 하면 환경에 따라 매번 수 초씩 걸릴 수 있어 캐시.
    @st.cache_data(ttl=20, show_spinner=False)
    def _ollama_status() -> bool:
        return gen.check_ollama(gen.DEFAULT_HOST)

    ollama_ok = _ollama_status()
    st.caption(f"Ollama: {'🟢 연결됨' if ollama_ok else '⚪ 미연결 (템플릿 모드 사용 가능)'}")
    # 데이터 경로를 노출해 두면 같은 포트를 점유한 다른 인스턴스(WSL vs 윈도우)에
    # 접속했을 때 즉시 알아챌 수 있다.
    st.caption(f"데이터 위치: `{db.DB_PATH}`")

    # ---- 백그라운드 LLM 작업(생성·추출) 진행 패널 — 어느 페이지에서든 보이도록 사이드바에 ----
    def _running(job_key: str, thread_key: str) -> bool:
        j = st.session_state.get(job_key)
        t = st.session_state.get(thread_key)
        return (j is not None and j["status"] == "running"
                and t is not None and t.is_alive())

    _gen_running = _running("gen_job", "gen_thread")
    _ext_running = _running("ext_job", "ext_thread")

    @st.fragment(run_every=2.0 if (_gen_running or _ext_running) else None)
    def _llm_progress_panel() -> None:
        j = st.session_state.get("gen_job")
        if j is not None:
            t = st.session_state.get("gen_thread")
            alive = t is not None and t.is_alive()
            if j["status"] == "running" and alive:
                st.progress(min(j["done"] / max(j["total"], 1), 1.0),
                            text=f"백그라운드 생성 {j['done']}/{j['total']} (저장 {j['saved']}건)")
                st.caption("생성 중에도 페이지 이동·검수를 계속할 수 있습니다.")
            else:
                if j["status"] == "running" and not alive:
                    j["status"] = "stopped"  # 스레드가 비정상 종료된 경우
                if j["status"] == "error":
                    st.error(f"생성 실패: {j['error']} — 그 전까지 {j['saved']}건은 저장됨")
                elif j["status"] == "stopped":
                    st.warning(f"생성이 중단되었습니다 — {j['saved']}건은 저장됨")
                else:
                    st.success(f"생성 완료: {j['saved']}건 저장"
                               + (f" (트렌드 {j['trend']}건)" if j["trend"] else ""))
                if st.button("알림 지우기", key="gen_job_dismiss"):
                    st.session_state.pop("gen_job", None)
                    st.session_state.pop("gen_thread", None)
                    st.rerun()

        ej = st.session_state.get("ext_job")
        if ej is not None:
            et = st.session_state.get("ext_thread")
            ealive = et is not None and et.is_alive()
            if ej["status"] == "running" and ealive:
                st.caption("🔎 트렌드 시드 후보 추출 중... 완료되면 ④ 탭에 표시됩니다. "
                           "(CPU에서는 몇 분 걸릴 수 있음)")
            elif ej["status"] == "done":
                st.caption(f"✅ 추출 완료 — 후보 {len(ej['cands'] or [])}건. ④ 탭에서 검토하세요.")
            else:
                st.caption("⚠️ 추출이 끝나지 못했습니다. ④ 탭에서 상태를 확인하세요.")

        if (_gen_running or _ext_running) and not (
                _running("gen_job", "gen_thread") or _running("ext_job", "ext_thread")):
            st.rerun()  # 완료 전환 시 1회 전체 rerun으로 2초 폴링 종료

    st.divider()
    _llm_progress_panel()

# ---------------------------------------------------------------
# ① 질문 생성
# ---------------------------------------------------------------
if page == "① 질문 생성":
    st.subheader("평가 질문 생성")
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        picked = st.multiselect(
            "도메인", options=list(DOMAIN_LABELS.keys()),
            default=list(DOMAIN_LABELS.keys()),
            format_func=lambda k: DOMAIN_LABELS[k],
        )
    with col2:
        count = st.number_input("생성 개수", min_value=1, max_value=1000, value=30, step=10)
    with col3:
        mode = st.selectbox("생성 모드", ["auto", "ollama", "template"],
                            help="auto: Ollama 연결 시 LLM, 아니면 템플릿")

    col_m, col_b = st.columns([3, 1])
    with col_m:
        model = st.text_input("Ollama 모델 태그", value=gen.DEFAULT_MODEL,
                              disabled=(mode == "template"))
    with col_b:
        batch = st.number_input(
            "배치 크기", min_value=1, max_value=32, value=gen.DEFAULT_BATCH,
            disabled=(mode == "template"),
            help="LLM 호출 1회당 생성 질문 수. 고정 프롬프트 비용을 나눠 가져 CPU에서 클수록 빠릅니다.")

    trend_pools = db.active_trend_seeds()
    n_trend = sum(len(v) for v in trend_pools.values())
    trend_ratio = st.slider(
        "트렌드 시드 주입 비율", 0.0, 1.0, 0.3, 0.05, disabled=(n_trend == 0),
        help="슬롯을 채울 때 이 확률로 트렌드 시드(④ 탭에서 등록)에서 값을 뽑습니다. "
             f"현재 유효한 트렌드 시드 {n_trend}개"
             + ("" if n_trend else " — ④ 탭에서 먼저 등록하세요."))

    gen_thread = st.session_state.get("gen_thread")
    gen_running = ((gen_thread is not None and gen_thread.is_alive())
                   or _llm_lock().locked())  # 추출·다른 세션(탭)의 LLM 작업도 포함

    st.caption("생성은 백그라운드에서 진행됩니다 — 페이지를 이동하거나 검수를 계속해도 끊기지 "
               "않고, 질문은 만들어지는 즉시 저장됩니다. 진행률은 사이드바에 표시되고, "
               "새 질문은 아래 '질문 정리'와 ② 대기열에서 바로 확인할 수 있습니다. "
               "생성과 ④ 시드 추출은 같은 로컬 LLM을 쓰므로 한 번에 하나만 실행됩니다.")
    if st.button("생성 시작" if not gen_running else "LLM 작업 진행 중...",
                 type="primary", disabled=not picked or gen_running):
        lock = _llm_lock()
        if not lock.acquire(blocking=False):
            st.warning("다른 LLM 작업(생성 또는 ④ 추출)이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
        else:
            try:
                job = {"total": int(count), "done": 0, "saved": 0, "trend": 0,
                       "status": "running", "error": None, "mode": None}
                params = dict(
                    count=int(count), domains=picked, mode=mode, model=model,
                    batch=int(batch), exemplars=db.exemplar_records(6),
                    existing_questions=db.all_question_texts(),  # 과거 생성분과 준중복 방지
                    existing_combos={gen.combo_key(i, s)  # 같은 (인텐트, 엔티티) 반복 회피
                                     for i, s in db.all_question_combo_pairs()},
                    trend_pools=trend_pools, trend_ratio=float(trend_ratio),
                )
                worker = threading.Thread(target=_generation_worker,
                                          args=(job, params, lock), daemon=True)
                st.session_state["gen_job"] = job
                st.session_state["gen_thread"] = worker
                worker.start()  # 이후 잠금 해제는 워커의 finally가 담당
            except BaseException:
                lock.release()  # 시작에 실패하면 잠금이 남지 않게
                raise
            st.rerun()

    st.divider()
    st.subheader("질문 정리 — 검수 전에 걸러내기")
    curation = db.curation_df()
    if curation.empty:
        st.caption("정리할 질문이 없습니다. (검수 완료 건은 여기 나오지 않습니다)")
    else:
        st.caption(
            "표는 **실제 검수 순서**로 정렬되고 대기 건에는 순번이 붙습니다. "
            "체크박스로 질문을 선택하세요 (헤더 체크박스 = 전체 선택). "
            "**먼저 검수**를 누르면 선택한 질문이 맨 위로 올라오는 것이 순번으로 보입니다. "
            "**결함 제외**는 기록이 남아 생성 품질 신호로 쓰이고, **영구 삭제**는 DB에서 흔적 없이 지웁니다. "
            "특정 질문 하나를 지금 바로 검수하려면 ② 탭 상단 드롭다운에서 검색해 선택하세요."
        )
        event = st.dataframe(
            curation, width="stretch", hide_index=True,
            on_select="rerun", selection_mode="multi-row",
            column_config={"id": None},
            key="curation_table",
        )
        sel_rows = [i for i in event.selection.rows if i < len(curation)]
        sel_ids = curation.iloc[sel_rows]["id"].tolist()
        c0, c1, c2, c3 = st.columns(4)
        if c0.button(f"선택 {len(sel_ids)}건 먼저 검수", disabled=not sel_ids,
                     help="선택한 질문을 검수 대기열 맨 앞으로 보냅니다. "
                          "건너뛰거나 제외했던 질문도 대기로 복원됩니다."):
            db.prioritize_questions(sel_ids)
            st.toast(f"{len(sel_ids)}건을 맨 앞으로 보냈습니다. ② 탭에서 바로 검수하세요.")
            st.rerun()
        if c1.button(f"선택 {len(sel_ids)}건 결함 제외", disabled=not sel_ids,
                     help="비문 등 별로인 질문. 검수 대상에서 빠지고 few-shot 예시에서도 배제됩니다."):
            db.reject_questions(sel_ids)
            st.toast(f"{len(sel_ids)}건을 결함 제외했습니다.")
            st.rerun()
        if c2.button(f"선택 {len(sel_ids)}건 영구 삭제", disabled=not sel_ids,
                     help="DB에서 완전히 제거 — 목록·통계 어디에도 남지 않습니다. 되돌릴 수 없습니다."):
            n = db.delete_questions(sel_ids)
            st.toast(f"{n}건을 삭제했습니다.")
            st.rerun()
        if c3.button("선택만 남기고 나머지 영구 삭제", disabled=not sel_ids,
                     help="마음에 드는 질문만 선택한 뒤 누르면, 나머지 미검수 질문을 전부 지웁니다. 되돌릴 수 없습니다."):
            others = [i for i in curation["id"].tolist() if i not in set(sel_ids)]
            n = db.delete_questions(others)
            st.toast(f"{n}건 삭제, {len(sel_ids)}건 유지.")
            st.rerun()

    with st.expander("전체 질문 목록 보기 (검수 완료 포함)"):
        st.dataframe(db.questions_df(), width="stretch", hide_index=True)

# ---------------------------------------------------------------
# ② 검수 세션 — "지금 검수 중인 질문"을 앱이 소유한다 (review_qid).
#
# 드롭다운·정렬 위젯 값에서 매 rerun마다 다시 계산하지 않는다. 화면 표시·입력 폼·저장이
# 전부 review_qid 하나에서 나오고, review_qid는 명시적 이동(저장 후 자동 진행 / 점프 /
# 직접 추가)일 때만 바뀌며 바뀔 때 입력 폼을 비운다. → 입력이 남아 있는 한 대상 질문은
# 절대 바뀌지 않으므로, 저장이 엉뚱한 질문에 붙는 오매칭이 구조적으로 불가능해진다.
#
# 입력을 st.form이 아니라 라이브 위젯으로 두는 이유: st.form은 '저장'을 누르기 전까지
# 입력값을 session_state에 넣지 않아 "입력이 남았는지"를 판정할 수 없었고, 바로 그게
# 이전 오매칭의 원인이었다. 라이브 위젯은 session_state가 실시간이라 판정이 정확하다.
# ---------------------------------------------------------------
RV_BLANK = {"rv_response": "", "rv_search": "", "rv_acc": VERDICTS[0], "rv_fail": [],
            "rv_acc_comment": "", "rv_out": VERDICTS[0], "rv_err": [],
            "rv_out_comment": "", "rv_allow_dup": False}


def _review_dirty() -> bool:
    """저장 안 한 검수 입력이 남아 있는가."""
    s = st.session_state
    return bool(s.get("rv_response", "").strip() or s.get("rv_search", "").strip()
                or s.get("rv_acc_comment", "").strip() or s.get("rv_out_comment", "").strip()
                or s.get("rv_fail") or s.get("rv_err"))


def _reset_review_form() -> None:
    """입력 폼을 비운다. 위젯 인스턴스화 전(페이지 상단 플래그 소비 또는 콜백)에서만 호출."""
    for k, v in RV_BLANK.items():
        st.session_state[k] = list(v) if isinstance(v, list) else v


def _set_active(qid: str | None, sync_browse: bool = True) -> None:
    """활성 검수 질문을 바꾸고 입력 폼을 비운다(새 질문에 결속). 콜백/상단 플래그에서만 호출."""
    st.session_state["review_qid"] = qid
    _reset_review_form()
    if sync_browse and qid is not None:
        st.session_state["browse_pick"] = qid


def _group_key(q: dict) -> str:
    return f"{q['domain_name']}/{q['intent_name']}"


def _order_queue(queue: list[dict], newest_first: bool) -> list[dict]:
    if newest_first:  # 방금 생성한 질문부터
        return sorted(queue, key=lambda q: (q["created_at"], q["id"]), reverse=True)
    return queue  # pending_queue()가 이미 검수 순서(우선·수동·생성일시)


def _next_after(done: dict, newest_first: bool) -> str | None:
    """저장/건너뛰기 후 다음 질문 — 같은 인텐트 → 같은 도메인 → 대기열 맨 앞.
    '날씨 인텐트 끝나면 첫 질문으로 튐'을 없애고 묶어서 검수를 자동화한다."""
    rest = _order_queue([q for q in db.pending_queue() if q["id"] != done["id"]],
                        newest_first)
    if not rest:
        return None
    same_intent = [q for q in rest if _group_key(q) == _group_key(done)]
    if same_intent:
        return same_intent[0]["id"]
    same_domain = [q for q in rest if q["domain_name"] == done["domain_name"]]
    if same_domain:
        return same_domain[0]["id"]
    return rest[0]["id"]


def _on_browse_jump() -> None:
    """점프 드롭다운 변경 콜백. 입력이 남아 있으면 확인을 거치고(활성 질문 유지),
    비어 있으면 즉시 이동한다."""
    req = st.session_state.get("browse_pick")
    if req is None or req == st.session_state.get("review_qid"):
        st.session_state.pop("pending_jump", None)
        return
    if _review_dirty():
        st.session_state["pending_jump"] = req  # 배너로 확인 요구, review_qid는 그대로
    else:
        _set_active(req, sync_browse=False)      # browse_pick은 이미 req


def _confirm_jump() -> None:
    req = st.session_state.pop("pending_jump", None)
    if req is not None:
        _set_active(req)


def _cancel_jump() -> None:
    st.session_state.pop("pending_jump", None)
    st.session_state["browse_pick"] = st.session_state.get("review_qid")  # 드롭다운 원위치


# ---------------------------------------------------------------
# ② 검수 진행
# ---------------------------------------------------------------
if page == "② 검수 진행":
    # 라이브 입력 위젯 기본값(없을 때만). 이후엔 콜백/상단 플래그로만 초기화한다.
    for _k, _v in RV_BLANK.items():
        st.session_state.setdefault(_k, list(_v) if isinstance(_v, list) else _v)
    # 이동 플래그 소비: 위젯이 그려지기 전에 활성 질문을 바꾼다
    # (위젯 인스턴스화 후 그 상태를 바꾸면 Streamlit이 예외를 던지므로 반드시 여기서).
    if "review_advance_to" in st.session_state:
        _set_active(st.session_state.pop("review_advance_to"))

    with st.expander("💡 직접 떠올린 질문 추가 — 저장하면 대기열에 들어갑니다"):
        st.caption(
            "테스트 중 생각난 질문을 여기 먼저 등록하면: ① 과거에 이미 (비슷하게) 테스트한 질문이면 "
            "알려줘서 중복 검수를 막고, ② 생성 질문과 같은 흐름으로 기록·집계·제출양식에 자동 포함되며, "
            "③ 검수를 통과하면 few-shot 예시로 재사용되어 이후 생성 품질을 끌어올립니다."
        )
        if st.session_state.pop("mq_clear", False):
            st.session_state["mq_text"] = ""
            st.session_state["mq_entities"] = ""
            st.session_state["mq_force"] = False
        mq_text = st.text_input("질문", key="mq_text",
                                placeholder="예: 가평역 근처 브런치 되는 카페 있어?")
        mc1, mc2 = st.columns(2)
        with mc1:
            mq_domain = st.selectbox("도메인", list(DOMAIN_LABELS) + ["etc"], key="mq_domain",
                                     format_func=lambda k: DOMAIN_LABELS.get(k, "기타"))
        with mc2:
            mq_entities = st.text_input(
                "핵심 엔티티 (쉼표 구분, 권장)", key="mq_entities", placeholder="가평역, 브런치",
                help="few-shot 예시로 쓰일 때 '필수 포함' 값이 됩니다. 비워도 됩니다.")
        mq_force = st.checkbox("비슷한 과거 질문이 있어도 추가 (예: 다른 단계에서 재검수)",
                               key="mq_force")
        if st.button("질문 추가", type="primary"):
            q_text = mq_text.strip()
            if not q_text:
                st.warning("질문을 입력해 주세요.")
            else:
                similar = gen.similar_questions(q_text, db.all_question_texts())
                if similar and not mq_force:
                    st.warning("이미 비슷한 질문이 있습니다. 그래도 추가하려면 위 체크박스를 켜세요:\n\n"
                               + "\n".join(f"- {t}" for t in similar[:3]))
                else:
                    slots = {f"entity{i + 1}": v.strip()
                             for i, v in enumerate(mq_entities.split(",")) if v.strip()}
                    new_id = uuid.uuid4().hex[:12]
                    db.insert_questions([{
                        "id": new_id,
                        "domain": mq_domain,
                        "domain_name": DOMAIN_LABELS.get(mq_domain, "기타"),
                        "intent": "manual", "intent_name": "직접 작성",
                        "slots": slots, "style": {},
                        "question": q_text, "gen_mode": "manual", "model": None,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }])
                    st.session_state["mq_clear"] = True
                    # 검수 입력이 없을 때만 방금 추가한 질문으로 바로 이동(입력 유실 방지).
                    if _review_dirty():
                        st.toast("대기열에 추가했습니다. 지금 검수를 저장한 뒤 이동하세요.")
                    else:
                        st.session_state["review_advance_to"] = new_id
                        st.toast("추가했습니다. 바로 아래에서 검수하세요.")
                    st.rerun()

    queue = db.pending_queue()
    if not queue:
        st.session_state.pop("review_qid", None)
        current = None
        st.info("대기 중인 질문이 없습니다. ① 탭에서 질문을 생성하세요.")
    else:
        newest_first = st.session_state.get("review_newest", False)
        ordered = _order_queue(queue, newest_first)
        qids = [q["id"] for q in ordered]

        # 활성 질문 확정(없거나 대기열에서 사라졌으면 정렬 순 맨 앞으로) — 위젯 그리기 전.
        active = st.session_state.get("review_qid")
        if active not in qids:
            active = qids[0]
            st.session_state["review_qid"] = active
            st.session_state["browse_pick"] = active
        current = next(q for q in ordered if q["id"] == active)

        st.caption(f"대기 {len(queue)}건 · 저장하면 **같은 인텐트의 다음 질문**으로 자동 진행합니다 "
                   "(그 인텐트가 끝나면 같은 도메인 → 대기열 순). 특정 질문을 지금 보려면 오른쪽에서 점프하세요.")
        cc1, cc2 = st.columns([1, 2])
        with cc1:
            st.toggle("최신 생성 먼저", key="review_newest",
                      help="켜면 방금 생성한 질문부터 검수합니다(점프 목록·자동 진행 순서에 반영).")
        with cc2:
            labels = {q["id"]: f"{i + 1}. [{q['domain_name']}/{q['intent_name']}] "
                               f"{q['question'][:60]}" for i, q in enumerate(ordered)}
            if st.session_state.get("browse_pick") not in qids:
                st.session_state["browse_pick"] = active
            st.selectbox(
                "다른 질문으로 점프 — 입력해서 검색·선택 (예: 바비)", qids,
                format_func=lambda i: labels[i], key="browse_pick",
                on_change=_on_browse_jump)

        # 저장 안 한 입력이 있는데 점프를 시도한 경우 — 확인 배너(활성 질문은 유지).
        pj = st.session_state.get("pending_jump")
        if pj is not None and pj in qids:
            pj_q = next(q for q in ordered if q["id"] == pj)
            st.warning(f"저장 안 한 검수 입력이 있습니다. **{pj_q['question'][:40]}**(으)로 "
                       "이동하면 지금 입력이 사라집니다. 저장 후 이동하거나, 버리고 이동하세요.")
            jc1, jc2, _ = st.columns([1, 1, 3])
            jc1.button("버리고 이동", on_click=_confirm_jump)
            jc2.button("현재 유지", on_click=_cancel_jump)
        elif pj is not None:
            st.session_state.pop("pending_jump", None)  # 대상이 사라짐

        left, right = st.columns([3, 2])
        with left:
            st.subheader("이 질문을 카나나 앱에 붙여넣기")
            st.caption("아래 박스에 마우스를 올리면 오른쪽 위에 복사 버튼이 나타납니다.")
            st.code(current["question"], language=None)

            slots = json.loads(current["slots_json"])
            st.caption(
                f"도메인 **{current['domain_name']}** / 인텐트 **{current['intent_name']}** / "
                + " · ".join(f"{k}={v}" for k, v in slots.items())
            )

            with st.expander("✏️ 질문 다듬기 — 조금만 고치면 쓸 수 있을 때"):
                st.caption(
                    "비문·어색한 어미만 고치면 되는 질문은 버리지 말고 여기서 고치세요. "
                    "고친 질문이 검수를 통과하면 few-shot 예시로 재사용되어, 좋은 예시를 "
                    "하나 더 만드는 효과가 있습니다. 필수 엔티티는 유지해야 합니다."
                )
                new_q = st.text_input("질문 수정", value=current["question"],
                                      key=f"edit_q_{current['id']}")
                if st.button("수정 저장", key=f"edit_q_btn_{current['id']}"):
                    fixed = new_q.strip()
                    missing = [v for v in slots.values()
                               if gen.required_token(v) not in fixed]
                    others = db.all_question_texts()
                    if current["question"] in others:
                        others.remove(current["question"])  # 자기 자신 제외
                    if not fixed:
                        st.warning("질문이 비어 있습니다.")
                    elif missing:
                        st.warning("필수 엔티티가 빠졌습니다: " + ", ".join(missing))
                    elif any(gen._normalize(t) == gen._normalize(fixed) for t in others):
                        st.warning("같은 질문이 이미 존재합니다.")
                    else:
                        db.update_question_text(current["id"], fixed)
                        st.toast("질문을 수정했습니다.")
                        st.rerun()  # review_qid 그대로 → 같은 질문(새 문구)이 유지된다

            b1, b2 = st.columns(2)
            if b1.button("이 질문 건너뛰기"):
                db.skip_question(current["id"])
                st.session_state["review_advance_to"] = _next_after(current, newest_first)
                st.rerun()
            if b2.button("질문 결함 → 제외",
                         help="비문·의미 붕괴 등 질문 자체가 잘못된 경우. "
                              "검수 대상에서 제외되고, 검수 통과 질문만 few-shot 예시로 재사용됩니다."):
                db.reject_question(current["id"])
                st.session_state["review_advance_to"] = _next_after(current, newest_first)
                st.toast("결함 질문으로 제외했습니다.")
                st.rerun()

        with right:
            st.subheader("검수 결과 기록")
            st.caption("입력은 **지금 왼쪽에 보이는 질문**에 저장됩니다. 다른 질문으로 점프하면 "
                       "입력이 비워지므로, 한 질문을 끝내 저장한 뒤 다음으로 넘어가세요.")
            response = st.text_area(
                "카나나 앱 응답 (전문 붙여넣기 — 내부 분석용)", height=160, key="rv_response",
                placeholder="앱에서 받은 응답을 그대로 붙여넣으세요.")
            search_id = st.text_input(
                "search ID (응답을 받으면 앱에 반드시 함께 생성됩니다)", key="rv_search")

            st.markdown("**1) 정확도**")
            acc_verdict = st.radio("정확도 판정", VERDICTS, horizontal=True,
                                   label_visibility="collapsed", key="rv_acc")
            fail_types = st.multiselect("N 사유 (fail 시 선택)", FAIL_TYPES, key="rv_fail")
            acc_comment = st.text_area("정확도 코멘트", key="rv_acc_comment", height=68)

            st.markdown("**2) LLM 출력**")
            out_verdict = st.radio("LLM 출력 판정", VERDICTS, horizontal=True,
                                   label_visibility="collapsed", key="rv_out")
            output_errors = st.multiselect("오류 유형 (fail 시 선택)", OUTPUT_ERROR_TYPES,
                                           key="rv_err")
            out_comment = st.text_area("출력 오류 코멘트", key="rv_out_comment", height=68)

            allow_dup = st.checkbox(
                "이전 검수와 같은 search ID/앱 응답이어도 저장", key="rv_allow_dup",
                help="서로 다른 질문에 동일한 폴백 응답이 온 경우처럼 드문 상황에서만 켜세요.")

            if st.button("저장하고 다음 →", type="primary", key="rv_save"):
                qid = st.session_state["review_qid"]  # 소유값 = 왼쪽에 보이는 그 질문
                dup_fields = db.duplicate_review_fields(
                    qid, search_id.strip(), response.strip())
                if not response.strip():
                    st.warning("앱 응답이 비어 있습니다. 응답 전문을 붙여넣어 주세요.")
                elif not search_id.strip():
                    st.warning("search ID가 비어 있습니다. 응답을 받았다면 search ID도 반드시 "
                               "생성됩니다 — 앱에서 확인해 입력해 주세요.")
                elif acc_verdict == "fail" and not fail_types and not acc_comment.strip():
                    st.warning("정확도 fail에는 N 사유를 선택하거나 코멘트를 적어주세요.")
                elif out_verdict == "fail" and not output_errors and not out_comment.strip():
                    st.warning("LLM 출력 fail에는 오류 유형을 선택하거나 코멘트를 적어주세요.")
                elif dup_fields and not allow_dup:
                    st.warning("이전 검수 기록과 동일한 **" + ", ".join(dup_fields) + "** 입니다. "
                               "직전 답변 것을 잘못 붙여넣지 않았는지 확인해 주세요. "
                               "실제로 같은 값이 맞다면 체크박스를 켜고 다시 저장하세요.")
                else:
                    db.save_review(
                        qid, response.strip(),
                        accuracy_verdict=acc_verdict,
                        fail_type=", ".join(fail_types),
                        accuracy_comment=acc_comment.strip(),
                        output_verdict=out_verdict,
                        output_error_type=", ".join(output_errors),
                        output_comment=out_comment.strip(),
                        search_id=search_id.strip(),
                        reviewer=st.session_state.get("reviewer", ""),
                        phase=st.session_state.get("phase", ""),
                    )
                    st.session_state["last_reviewed"] = qid
                    st.session_state["review_advance_to"] = _next_after(current, newest_first)
                    st.toast("저장 완료")
                    st.rerun()

    last_id = st.session_state.get("last_reviewed")
    if last_id:
        st.divider()
        if st.button("↩ 방금 저장한 검수 되돌리기",
                     help="직전 저장 건의 검수 기록을 지우고 그 질문을 재검수 대기로 되돌립니다. "
                          "붙여넣기 실수를 바로 알아챘을 때 쓰세요."):
            db.reopen_review(last_id)
            del st.session_state["last_reviewed"]
            st.session_state["review_advance_to"] = last_id  # 그 질문으로 돌아가 재검수
            st.toast("되돌렸습니다. 해당 질문이 다시 검수 차례로 돌아옵니다.")
            st.rerun()

# ---------------------------------------------------------------
# ③ 결과·내보내기
# ---------------------------------------------------------------
if page == "③ 결과·내보내기":
    results = db.results_df()
    if results.empty:
        st.info("아직 검수 완료된 항목이 없습니다.")
    else:
        n = len(results)
        acc_pass = (results["1) 정확도"] == "pass").mean() * 100
        out_pass = (results["2) LLM 출력"] == "pass").mean() * 100
        m1, m2, m3 = st.columns(3)
        m1.metric("검수 완료", n)
        m2.metric("정확도 pass율", f"{acc_pass:.1f}%")
        m3.metric("LLM 출력 pass율", f"{out_pass:.1f}%")

        st.markdown("**도메인별 정확도 현황**")
        pivot = (
            results.pivot_table(index="도메인", columns="1) 정확도", values="검색 키워드",
                                aggfunc="count", fill_value=0)
            .reindex(columns=VERDICTS, fill_value=0)
        )
        pivot["합계"] = pivot.sum(axis=1)
        pivot["pass율(%)"] = (pivot.get("pass", 0) / pivot["합계"] * 100).round(1)
        st.dataframe(pivot, width="stretch")

        trend_mask = results["시드출처"] == "trend"
        if trend_mask.any():
            st.markdown("**트렌드 시드 질문 체크** — 최신 엔티티에 대한 응답 품질 "
                        "(최신성 fail율이 기본 시드보다 높다면 카나나가 최신 정보를 못 따라오는 신호)")

            def _rates(df: pd.DataFrame) -> pd.Series:
                return pd.Series({
                    "검수 건수": len(df),
                    "정확도 pass율(%)": round((df["1) 정확도"] == "pass").mean() * 100, 1),
                    "최신성 fail율(%)": round(
                        df["N 사유"].fillna("").str.contains("최신성").mean() * 100, 1),
                })

            groups = [("트렌드 시드", results[trend_mask])]
            if (~trend_mask).any():
                groups.append(("기본 시드", results[~trend_mask]))
            st.dataframe(pd.DataFrame({name: _rates(df) for name, df in groups}).T,
                         width="stretch")

        st.markdown("**검수 결과 (제출양식 뷰)** — 셀을 드래그로 선택해 복사(Ctrl+C)한 뒤 "
                    "카카오 시트에 그대로 붙여넣으세요.")
        f1, f2 = st.columns([1, 3])
        with f1:
            scope = st.selectbox(
                "표시 범위", ["전체", "오늘 검수분", "날짜 지정"], key="submit_scope",
                help="매일 공유 시트에 그날 검수분만 붙여넣을 때 쓰세요. "
                     "아래 표와 xlsx '제출양식' 시트에 적용됩니다 (요약·내부분석 시트는 항상 전체).")
        if scope == "오늘 검수분":
            view_results = results[results["검수일시"].str[:10] == date.today().isoformat()]
        elif scope == "날짜 지정":
            with f2:
                scope_date = st.date_input("날짜", value=date.today(), key="submit_scope_date")
            view_results = results[results["검수일시"].str[:10] == scope_date.isoformat()]
        else:
            view_results = results
        if scope != "전체":
            st.caption(f"선택 범위 검수 {len(view_results)}건 (전체 {len(results)}건)")
        # 도메인 필터 — 본사 제출은 도메인별 시트라, 화면에서도 한 도메인씩 골라 복사할 수 있게.
        # (xlsx는 이 필터와 무관하게 항상 도메인별 시트로 나뉘어 나간다.)
        doms = sorted(d for d in view_results["도메인"].dropna().unique())
        dsel = st.selectbox(
            "도메인", ["전체"] + doms, key="submit_domain",
            help="화면 표는 고른 도메인만 보여줍니다. xlsx는 도메인별 시트로 나뉘어 나갑니다.")
        dview = view_results if dsel == "전체" else view_results[view_results["도메인"] == dsel]
        with st.expander("컬럼 구성 수정 — 카카오 양식에 열이 추가·삭제되면 여기서 맞추기"):
            st.caption(
                "한 줄이 컬럼 하나이고 순서 그대로 아래 표와 xlsx에 반영됩니다. "
                "줄을 지우면 그 컬럼이 빠지고, 검수 데이터에 없는 이름(예: 기획 검수, 비고)은 "
                "제목만 있는 빈 컬럼으로 들어갑니다. "
                f"사용 가능한 데이터 컬럼: {', '.join(list(SUBMIT_DERIVED) + list(results.columns))}"
            )
            layout_text = st.text_area(
                "표시·내보낼 컬럼 (한 줄에 하나)", "\n".join(DEFAULT_SUBMIT_LAYOUT),
                height=340, key="submit_layout", label_visibility="collapsed",
            )
        layout = [ln.strip() for ln in layout_text.splitlines() if ln.strip()]
        if not layout:
            layout = list(DEFAULT_SUBMIT_LAYOUT)

        def unique_columns(names: list[str]) -> list[str]:
            """화면 표시용 중복 해소 — 같은 이름 뒤에 보이지 않는 공백을 붙인다."""
            seen: dict[str, int] = {}
            out: list[str] = []
            for n in names:
                k = seen.get(n, 0)
                out.append(n + " " * k)
                seen[n] = k + 1
            return out

        def submit_series(df: pd.DataFrame, name: str) -> pd.Series:
            if name in SUBMIT_DERIVED:
                return SUBMIT_DERIVED[name](df)
            if name in df.columns:
                return df[name]
            return pd.Series("", index=df.index)  # 카카오 측 기입란 → 빈 컬럼

        def build_submit(df: pd.DataFrame, display: bool) -> pd.DataFrame:
            """제출양식 프레임. display=True면 화면용(중복 헤더에 보이지 않는 공백),
            False면 xlsx용(원래 이름 그대로 — '기획 검수' 2개도 유지)."""
            out = pd.concat([submit_series(df, n) for n in layout], axis=1)
            out.columns = unique_columns(layout) if display else list(layout)
            return out

        submit = build_submit(dview, display=True)
        st.dataframe(submit, width="stretch", hide_index=True)
        st.caption("열 머리글 메뉴로 화면에서 열을 숨길 수도 있습니다. "
                   "숨김은 화면에만 적용되고 xlsx에는 위 컬럼 구성이 그대로 나갑니다.")

        with st.expander("내부분석 전체 보기 (앱 응답·도메인·인텐트 포함)"):
            st.dataframe(results, width="stretch", hide_index=True)

        with st.expander("✏️ 검수 기록 수정·삭제 — 잘못 붙여넣은 응답·search ID, 코멘트 오타 교정"):
            qids = results["질문ID"].tolist()[::-1]  # 최근 검수가 위로
            by_qid = results.set_index("질문ID")
            if st.session_state.get("edit_target") not in qids:
                st.session_state.pop("edit_target", None)

            def fmt_review(qid: str) -> str:
                row = by_qid.loc[qid]
                return f"{row['검수일시']} | {row['search ID']} | {row['검색 키워드'][:40]}"

            target = st.selectbox("수정할 검수 건", qids, format_func=fmt_review,
                                  key="edit_target")
            detail = db.review_detail(target)
            if detail is None:
                st.warning("기록을 찾을 수 없습니다.")
            else:
                with st.form(f"edit_review_{target}"):
                    e_response = st.text_area("카나나 앱 응답", value=detail["response"],
                                              height=140, key=f"ed_resp_{target}")
                    e_search = st.text_input("search ID", value=detail["search_id"] or "",
                                             key=f"ed_sid_{target}")
                    e_acc = st.radio(
                        "1) 정확도", VERDICTS, horizontal=True, key=f"ed_acc_{target}",
                        index=VERDICTS.index(detail["verdict"]) if detail["verdict"] in VERDICTS else 0)
                    stored_fail = [v for v in (detail["fail_type"] or "").split(", ") if v]
                    fail_opts = FAIL_TYPES + [v for v in stored_fail if v not in FAIL_TYPES]
                    e_fail = st.multiselect("N 사유 (fail 시)", fail_opts, default=stored_fail,
                                            key=f"ed_fail_{target}")
                    e_acc_c = st.text_area("정확도 코멘트", value=detail["reason"] or "",
                                           key=f"ed_accc_{target}", height=68)
                    e_out = st.radio(
                        "2) LLM 출력", VERDICTS, horizontal=True, key=f"ed_out_{target}",
                        index=VERDICTS.index(detail["output_verdict"]) if detail["output_verdict"] in VERDICTS else 0)
                    stored_err = [v for v in (detail["output_error_type"] or "").split(", ") if v]
                    err_opts = OUTPUT_ERROR_TYPES + [v for v in stored_err if v not in OUTPUT_ERROR_TYPES]
                    e_err = st.multiselect("오류 유형 (fail 시)", err_opts, default=stored_err,
                                           key=f"ed_err_{target}")
                    e_out_c = st.text_area("출력 오류 코멘트", value=detail["output_comment"] or "",
                                           key=f"ed_outc_{target}", height=68)
                    e_allow_dup = st.checkbox("다른 검수와 같은 search ID/앱 응답이어도 저장",
                                              key=f"ed_dup_{target}")
                    save_edit = st.form_submit_button("수정 저장 (검수일시·테스터는 원본 유지)",
                                                      type="primary")
                if save_edit:
                    dup_fields = db.duplicate_review_fields(
                        target, e_search.strip(), e_response.strip())
                    if not e_response.strip() or not e_search.strip():
                        st.warning("앱 응답과 search ID는 비울 수 없습니다.")
                    elif e_acc == "fail" and not e_fail and not e_acc_c.strip():
                        st.warning("정확도 fail에는 N 사유를 선택하거나 코멘트를 적어주세요.")
                    elif e_out == "fail" and not e_err and not e_out_c.strip():
                        st.warning("LLM 출력 fail에는 오류 유형을 선택하거나 코멘트를 적어주세요.")
                    elif dup_fields and not e_allow_dup:
                        st.warning("다른 검수 기록과 동일한 **" + ", ".join(dup_fields) + "** 입니다. "
                                   "확인 후 체크박스를 켜고 저장하세요.")
                    else:
                        db.save_review(
                            target, e_response.strip(),
                            accuracy_verdict=e_acc, fail_type=", ".join(e_fail),
                            accuracy_comment=e_acc_c.strip(),
                            output_verdict=e_out, output_error_type=", ".join(e_err),
                            output_comment=e_out_c.strip(), search_id=e_search.strip(),
                            reviewer=detail["reviewer"], phase=detail["phase"],
                            reviewed_at=detail["reviewed_at"],
                        )
                        st.toast("수정했습니다.")
                        st.rerun()
                d1, d2 = st.columns(2)
                if d1.button("↩ 재검수로 되돌리기",
                             help="검수 기록을 지우고 이 질문을 다시 검수 대기로 보냅니다."):
                    db.reopen_review(target)
                    st.toast("재검수 대기로 되돌렸습니다. ② 탭에서 다시 검수하세요.")
                    st.rerun()
                if d2.button("🗑 이 건 영구 삭제 (질문+검수 기록)",
                             help="질문과 검수 기록을 DB에서 완전히 제거합니다. 되돌릴 수 없습니다."):
                    db.delete_questions([target])
                    st.toast("삭제했습니다.")
                    st.rerun()

        # ---- xlsx 추출: 도메인별 시트(본사 제출용) + 전체 + 요약 + 내부분석 ----
        # 시트는 날짜 범위(view_results) 기준이며, 화면 도메인 필터와 무관하게 도메인마다 나뉜다.
        # 헤더는 양식 원래 이름 그대로('기획 검수' 2개 유지).
        def _sheet_name(name: str) -> str:
            for ch in "[]:*?/\\":
                name = name.replace(ch, "_")
            return name[:31]  # 엑셀 시트명 31자 제한

        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for dname, dgroup in view_results.groupby("도메인"):
                build_submit(dgroup, display=False).to_excel(
                    writer, sheet_name=_sheet_name(f"제출_{dname}"), index=False)
            build_submit(view_results, display=False).to_excel(
                writer, sheet_name="제출양식_전체", index=False)
            pivot.reset_index().to_excel(writer, sheet_name="요약", index=False)
            results.to_excel(writer, sheet_name="내부분석", index=False)
        st.download_button(
            "📥 xlsx 다운로드 (도메인별 시트 + 전체 + 요약 + 내부분석)",
            data=buf.getvalue(),
            file_name=f"E2E_품질평가_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        st.caption("※ 도메인별 시트(제출_날씨 / 제출_로컬 / …)를 각 제출 시트에 그대로 복사하세요. "
                   "'내부분석' 시트에는 앱 응답 전문·도메인·인텐트가 포함되니 제출본에서는 빼세요.")

# ---------------------------------------------------------------
# ④ 트렌드 시드 (최신 기사·리뷰 → 엔티티 추출 → 생성에 혼입)
# ---------------------------------------------------------------
if page == "④ 트렌드 시드":
    st.subheader("트렌드 시드 — 최신 기사·리뷰에서 엔티티 수혈")
    st.caption(
        "기사·리뷰 본문을 붙여넣으면 로컬 LLM이 시드 후보를 추출합니다. 검토 후 등록하면 "
        "① 탭 생성 시 설정한 비율로 섞여 들어가고, 그 질문은 시드출처=trend로 태깅되어 "
        "③ 탭에서 최신성 fail율을 따로 볼 수 있습니다. **본문 원문은 저장하지 않고**, "
        "만료된 시드는 생성에 쓰이지 않습니다."
    )

    trend_src = st.text_input("출처 메모 (기사 제목·URL 등)", key="trend_src")
    trend_raw = st.text_area("기사/리뷰 본문 붙여넣기", height=180, key="trend_raw",
                             placeholder="본문을 통째로 붙여넣어도 됩니다. 광고 문구는 추출·검토 단계에서 걸러집니다.")
    ext_thread = st.session_state.get("ext_thread")
    ext_job = st.session_state.get("ext_job")
    ext_running = ext_thread is not None and ext_thread.is_alive()
    llm_busy = _llm_lock().locked()

    tc1, tc2 = st.columns([1, 3])
    with tc1:
        trend_days = st.number_input("유효기간(일)", min_value=1, max_value=365, value=14,
                                     help="만료되면 생성에 더 이상 쓰이지 않습니다 (기록은 남음).")
    with tc2:
        st.write("")
        st.write("")
        extract_clicked = st.button(
            "후보 추출" if not llm_busy else "LLM 작업 진행 중...",
            type="primary", disabled=not trend_raw.strip() or not ollama_ok or llm_busy,
            help="추출은 백그라운드로 진행됩니다 — 기다리는 동안 다른 페이지에서 검수를 "
                 "계속할 수 있습니다. ① 생성과 같은 LLM을 쓰므로 한 번에 하나만 실행됩니다.")
    if not ollama_ok:
        st.info("Ollama 미연결 — 자동 추출은 불가하지만 아래 '직접 등록'은 사용할 수 있습니다.")

    if extract_clicked:
        lock = _llm_lock()
        if not lock.acquire(blocking=False):
            st.warning("다른 LLM 작업(① 생성 등)이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
        else:
            try:
                job = {"status": "running", "cands": None, "error": None}
                worker = threading.Thread(target=_extraction_worker,
                                          args=(job, trend_raw, lock), daemon=True)
                st.session_state["ext_job"] = job
                st.session_state["ext_thread"] = worker
                st.session_state.pop("trend_cands", None)  # 이전 추출 결과 비우기
                worker.start()  # 이후 잠금 해제는 워커의 finally가 담당
            except BaseException:
                lock.release()
                raise
            st.rerun()

    # 백그라운드 추출이 끝났으면 결과를 후보 검토 단계로 인계
    if ext_job is not None:
        if ext_job["status"] == "running" and not ext_running:
            ext_job["status"] = "error"
            ext_job["error"] = "추출 스레드가 비정상 종료되었습니다. 다시 시도하세요."
        if ext_job["status"] == "running":
            st.caption("🔎 추출 진행 중... 완료되면 여기에 후보가 표시됩니다 "
                       "(사이드바에서도 상태를 볼 수 있습니다).")
        else:
            if ext_job["status"] == "done":
                st.session_state["trend_cands"] = ext_job["cands"]
                if not ext_job["cands"]:
                    st.warning("후보를 찾지 못했습니다. 본문을 더 넣거나 직접 등록해 보세요.")
            else:
                st.error(f"추출 실패: {ext_job['error']}")
            st.session_state.pop("ext_job", None)
            st.session_state.pop("ext_thread", None)

    cands = st.session_state.get("trend_cands")
    if cands:
        st.markdown("**추출 후보 검토** — 값·풀을 고치고, 등록할 항목만 체크하세요. "
                    "'본문일치'가 꺼진 항목은 본문에 없는 표현이니 특히 확인하세요.")
        cand_df = pd.DataFrame([
            {"등록": c["verbatim"], "값": c["value"], "풀": c["pool"], "본문일치": c["verbatim"]}
            for c in cands])
        edited = st.data_editor(
            cand_df, hide_index=True, width="stretch", key="trend_editor",
            column_config={
                "등록": st.column_config.CheckboxColumn("등록"),
                "값": st.column_config.TextColumn("값"),
                "풀": st.column_config.SelectboxColumn("풀", options=list(TREND_POOL_GUIDE)),
                "본문일치": st.column_config.CheckboxColumn("본문일치", disabled=True),
            })
        if st.button("체크한 후보 등록"):
            expires = (date.today() + timedelta(days=int(trend_days))).isoformat()
            items = [{"value": str(r["값"]).strip(), "pool": r["풀"],
                      "source": trend_src.strip(), "expires_at": expires}
                     for _, r in edited.iterrows()
                     if r["등록"] and str(r["값"]).strip() and r["풀"] in TREND_POOL_GUIDE]
            n = db.add_trend_seeds(items)
            st.session_state.pop("trend_cands", None)
            st.toast(f"{n}건 등록" + (f" (중복 {len(items) - n}건 무시)" if len(items) != n else ""))
            st.rerun()

    st.divider()
    st.markdown("**직접 등록** — 추출 없이 값을 바로 추가")
    dc1, dc2, dc3 = st.columns([2, 3, 1])
    with dc1:
        t_val = st.text_input("값", key="trend_val", placeholder="두쫀쿠")
    with dc2:
        t_pool = st.selectbox("풀", list(TREND_POOL_GUIDE), key="trend_pool",
                              format_func=lambda p: f"{p} — {TREND_POOL_GUIDE[p]}")
    with dc3:
        st.write("")
        st.write("")
        if st.button("등록", disabled=not t_val.strip()):
            expires = (date.today() + timedelta(days=int(trend_days))).isoformat()
            n = db.add_trend_seeds([{"value": t_val.strip(), "pool": t_pool,
                                     "source": trend_src.strip(), "expires_at": expires}])
            st.toast("등록했습니다." if n else "이미 등록된 값입니다.")
            st.rerun()

    st.divider()
    st.markdown("**등록된 트렌드 시드**")
    tdf = db.trend_seeds_df()
    if tdf.empty:
        st.caption("아직 등록된 트렌드 시드가 없습니다.")
    else:
        t_event = st.dataframe(tdf, hide_index=True, width="stretch",
                               on_select="rerun", selection_mode="multi-row",
                               column_config={"id": None}, key="trend_table")
        t_rows = [i for i in t_event.selection.rows if i < len(tdf)]
        t_ids = tdf.iloc[t_rows]["id"].tolist()
        if st.button(f"선택 {len(t_ids)}건 삭제", disabled=not t_ids):
            db.delete_trend_seeds(t_ids)
            st.toast(f"{len(t_ids)}건 삭제")
            st.rerun()
