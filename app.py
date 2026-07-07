"""
app.py — E2E 검수 도우미 (Streamlit 로컬 앱)

흐름: ① 질문 생성 → ② 복사해서 카나나 앱에 붙여넣기 → 응답/판정 기록 → ③ 집계·xlsx 추출
실행: streamlit run app.py
모든 데이터는 로컬(data/eval_assistant.db)에만 저장된다.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from io import BytesIO

import pandas as pd
import requests
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
    ollama_ok = gen.check_ollama(gen.DEFAULT_HOST)
    st.caption(f"Ollama: {'🟢 연결됨' if ollama_ok else '⚪ 미연결 (템플릿 모드 사용 가능)'}")
    st.caption("데이터는 이 PC의 data/ 폴더에만 저장됩니다.")

# st.tabs는 위젯 변경 rerun 시 활성 탭이 첫 탭으로 튕기는 문제가 있어(예: ② 탭에서
# 검수할 질문을 고르면 ① 화면으로 이동) 세션 상태에 고정되는 페이지 방식을 쓴다.
PAGES = ["① 질문 생성", "② 검수 진행", "③ 결과·내보내기", "④ 트렌드 시드"]
_pick = st.segmented_control("페이지 이동", PAGES, key="nav", default=PAGES[0],
                             label_visibility="collapsed")
page = _pick or st.session_state.get("nav_last", PAGES[0])  # 재클릭 해제 시 현재 페이지 유지
st.session_state["nav_last"] = page

# 페이지를 오가도 작성 중이던 입력이 날아가지 않도록 위젯 상태를 고정한다.
# (렌더링되지 않은 위젯의 상태는 Streamlit이 정리해 버리므로 재대입으로 보존 표시)
_PRESERVE_KEYS = ("rv_response", "rv_search", "rv_acc_comment", "rv_out_comment",
                  "rv_fail", "rv_err", "mq_text", "mq_entities", "submit_layout",
                  "trend_src", "trend_raw", "trend_val")
for _k in _PRESERVE_KEYS:
    if _k in st.session_state:
        st.session_state[_k] = st.session_state[_k]

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

    if st.button("생성 시작", type="primary", disabled=not picked):
        progress = st.progress(0.0, text="생성 중...")

        def on_progress(done: int, total_: int) -> None:
            progress.progress(done / total_, text=f"생성 중... {done}/{total_}")

        try:
            exemplars = db.exemplar_records(6)
            if exemplars:
                st.caption(f"검수 통과 질문 {len(exemplars)}건을 few-shot 예시로 사용합니다 (도메인 안배).")
            records = gen.generate(
                count=int(count), domains=picked, mode=mode, model=model,
                batch=int(batch), exemplars=exemplars,
                existing_questions=db.all_question_texts(),  # 과거 생성분과 준중복 방지
                trend_pools=trend_pools, trend_ratio=float(trend_ratio),
                on_progress=on_progress,
            )
            inserted = db.insert_questions(records)
            progress.progress(1.0, text="완료")
            n_trend_q = sum(1 for r in records if r.get("seed_origin") == "trend")
            st.success(f"{inserted}건 생성·저장 완료 (모드: {records[0]['gen_mode'] if records else mode}"
                       + (f", 트렌드 시드 질문 {n_trend_q}건" if n_trend_q else "") + ")")
            st.dataframe(
                pd.DataFrame(
                    [{"도메인": r["domain_name"], "인텐트": r["intent_name"],
                      "질문": r["question"], "생성모드": r["gen_mode"],
                      "시드출처": r.get("seed_origin", "base")} for r in records]
                ),
                width="stretch", hide_index=True,
            )
        except Exception as e:  # noqa: BLE001
            st.error(f"생성 실패: {e}")

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
# ② 검수 진행
# ---------------------------------------------------------------
if page == "② 검수 진행":
    with st.expander("💡 직접 떠올린 질문 추가 — 저장하면 바로 다음 검수 차례가 됩니다"):
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
                    db.insert_questions([{
                        "id": uuid.uuid4().hex[:12],
                        "domain": mq_domain,
                        "domain_name": DOMAIN_LABELS.get(mq_domain, "기타"),
                        "intent": "manual", "intent_name": "직접 작성",
                        "slots": slots, "style": {},
                        "question": q_text, "gen_mode": "manual", "model": None,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }])
                    st.session_state["mq_clear"] = True
                    st.toast("추가했습니다. 바로 아래에서 검수하세요.")
                    st.rerun()

    queue = db.pending_queue()
    if not queue:
        current = None
        st.info("대기 중인 질문이 없습니다. ① 탭에서 질문을 생성하세요.")
    else:
        # 선택 검수: 기본은 대기열 맨 앞, 드롭다운에 타이핑하면 검색됨 (예: "바비").
        # 선택한 질문을 저장/건너뛰기/제외하면 대기열에서 빠지므로 자동으로 맨 앞으로 복귀.
        queue_ids = [q["id"] for q in queue]
        queue_labels = {
            q["id"]: f"{i + 1}. [{q['domain_name']}/{q['intent_name']}] {q['question'][:60]}"
            for i, q in enumerate(queue)
        }
        if st.session_state.get("review_pick") not in queue_ids:
            st.session_state.pop("review_pick", None)
        pick = st.selectbox(
            "검수할 질문 — 기본은 대기열 맨 앞, 입력해서 검색·선택하면 그 질문을 바로 검수",
            queue_ids, format_func=lambda i: queue_labels[i], key="review_pick",
        )
        current = next(q for q in queue if q["id"] == pick)

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
                        st.rerun()

            b1, b2 = st.columns(2)
            if b1.button("이 질문 건너뛰기"):
                db.skip_question(current["id"])
                st.rerun()
            if b2.button("질문 결함 → 제외",
                         help="비문·의미 붕괴 등 질문 자체가 잘못된 경우. "
                              "검수 대상에서 제외되고, 검수 통과 질문만 few-shot 예시로 재사용됩니다."):
                db.reject_question(current["id"])
                st.toast("결함 질문으로 제외했습니다.")
                st.rerun()

        with right:
            st.subheader("검수 결과 기록")
            # 저장 성공 직후에만 폼을 비운다 — 경고에 걸렸을 땐 입력(긴 응답 붙여넣기)을 보존
            if st.session_state.pop("review_clear", False):
                st.session_state.update({
                    "rv_response": "", "rv_search": "",
                    "rv_acc": VERDICTS[0], "rv_fail": [], "rv_acc_comment": "",
                    "rv_out": VERDICTS[0], "rv_err": [], "rv_out_comment": "",
                    "rv_allow_dup": False,
                })
            with st.form("review_form"):
                response = st.text_area(
                    "카나나 앱 응답 (전문 붙여넣기 — 내부 분석용)", height=160, key="rv_response",
                    placeholder="앱에서 받은 응답을 그대로 붙여넣으세요.",
                )
                search_id = st.text_input(
                    "search ID (응답을 받으면 앱에 반드시 함께 생성됩니다)", key="rv_search")

                st.markdown("**1) 정확도**")
                acc_verdict = st.radio("정확도 판정", VERDICTS, horizontal=True,
                                       label_visibility="collapsed", key="rv_acc")
                fail_types = st.multiselect("N 사유 (fail 시 선택)", FAIL_TYPES, key="rv_fail")
                acc_comment = st.text_input("정확도 코멘트", key="rv_acc_comment")

                st.markdown("**2) LLM 출력**")
                out_verdict = st.radio("LLM 출력 판정", VERDICTS, horizontal=True,
                                       label_visibility="collapsed", key="rv_out")
                output_errors = st.multiselect("오류 유형 (fail 시 선택)", OUTPUT_ERROR_TYPES,
                                               key="rv_err")
                out_comment = st.text_input("출력 오류 코멘트", key="rv_out_comment")

                allow_dup = st.checkbox(
                    "이전 검수와 같은 search ID/앱 응답이어도 저장", key="rv_allow_dup",
                    help="서로 다른 질문에 동일한 폴백 응답이 온 경우처럼 드문 상황에서만 켜세요.")
                submitted = st.form_submit_button("저장하고 다음 →", type="primary")

            if submitted:
                dup_fields = db.duplicate_review_fields(
                    current["id"], search_id.strip(), response.strip())
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
                        current["id"], response.strip(),
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
                    st.session_state["last_reviewed"] = current["id"]
                    st.session_state["review_clear"] = True
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

        def submit_series(name: str) -> pd.Series:
            if name in SUBMIT_DERIVED:
                return SUBMIT_DERIVED[name](view_results)
            if name in view_results.columns:
                return view_results[name]
            return pd.Series("", index=view_results.index)  # 카카오 측 기입란 → 빈 컬럼

        def unique_columns(names: list[str]) -> list[str]:
            """화면 표시용 중복 해소 — 같은 이름 뒤에 보이지 않는 공백을 붙인다."""
            seen: dict[str, int] = {}
            out: list[str] = []
            for n in names:
                k = seen.get(n, 0)
                out.append(n + " " * k)
                seen[n] = k + 1
            return out

        submit = pd.concat([submit_series(n) for n in layout], axis=1)
        submit.columns = unique_columns(layout)
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
                    e_acc_c = st.text_input("정확도 코멘트", value=detail["reason"] or "",
                                            key=f"ed_accc_{target}")
                    e_out = st.radio(
                        "2) LLM 출력", VERDICTS, horizontal=True, key=f"ed_out_{target}",
                        index=VERDICTS.index(detail["output_verdict"]) if detail["output_verdict"] in VERDICTS else 0)
                    stored_err = [v for v in (detail["output_error_type"] or "").split(", ") if v]
                    err_opts = OUTPUT_ERROR_TYPES + [v for v in stored_err if v not in OUTPUT_ERROR_TYPES]
                    e_err = st.multiselect("오류 유형 (fail 시)", err_opts, default=stored_err,
                                           key=f"ed_err_{target}")
                    e_out_c = st.text_input("출력 오류 코멘트", value=detail["output_comment"] or "",
                                            key=f"ed_outc_{target}")
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

        # ---- xlsx 추출: '제출양식' 시트는 위 표와 동일, 헤더는 양식 원래 이름 그대로 ----
        submit_x = submit.copy()
        submit_x.columns = layout  # 동일 이름 컬럼(기획 검수 2개)도 그대로 유지
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            submit_x.to_excel(writer, sheet_name="제출양식", index=False)
            pivot.reset_index().to_excel(writer, sheet_name="요약", index=False)
            results.to_excel(writer, sheet_name="내부분석", index=False)
        st.download_button(
            "📥 xlsx 다운로드 (제출양식 + 요약 + 내부분석)",
            data=buf.getvalue(),
            file_name=f"E2E_품질평가_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        st.caption("※ '내부분석' 시트에는 앱 응답 전문·도메인·인텐트가 포함됩니다. "
                   "카카오 제출 시 '제출양식' 시트만 복사해 쓰세요.")

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
    tc1, tc2 = st.columns([1, 3])
    with tc1:
        trend_days = st.number_input("유효기간(일)", min_value=1, max_value=365, value=14,
                                     help="만료되면 생성에 더 이상 쓰이지 않습니다 (기록은 남음).")
    with tc2:
        st.write("")
        st.write("")
        extract_clicked = st.button("후보 추출", type="primary",
                                    disabled=not trend_raw.strip() or not ollama_ok)
    if not ollama_ok:
        st.info("Ollama 미연결 — 자동 추출은 불가하지만 아래 '직접 등록'은 사용할 수 있습니다.")

    if extract_clicked:
        with st.spinner("본문에서 후보 추출 중..."):
            try:
                cands = gen.extract_trend_candidates(trend_raw, TREND_POOL_GUIDE)
                # 본문에 그대로 등장하는지 표시 (환각 방지 확인용)
                st.session_state["trend_cands"] = [
                    {**c, "verbatim": c["value"] in trend_raw} for c in cands]
                if not cands:
                    st.warning("후보를 찾지 못했습니다. 본문을 더 넣거나 직접 등록해 보세요.")
            except requests.RequestException as e:
                st.error(f"추출 실패: {e}")

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
