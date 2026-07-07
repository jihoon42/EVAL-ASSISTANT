"""
app.py — E2E 검수 도우미 (Streamlit 로컬 앱)

흐름: ① 질문 생성 → ② 복사해서 카나나 앱에 붙여넣기 → 응답/판정 기록 → ③ 집계·xlsx 추출
실행: streamlit run app.py
모든 데이터는 로컬(data/eval_assistant.db)에만 저장된다.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
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

tab_gen, tab_review, tab_result = st.tabs(["① 질문 생성", "② 검수 진행", "③ 결과·내보내기"])

# ---------------------------------------------------------------
# ① 질문 생성
# ---------------------------------------------------------------
with tab_gen:
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
                on_progress=on_progress,
            )
            inserted = db.insert_questions(records)
            progress.progress(1.0, text="완료")
            st.success(f"{inserted}건 생성·저장 완료 (모드: {records[0]['gen_mode'] if records else mode})")
            st.dataframe(
                pd.DataFrame(
                    [{"도메인": r["domain_name"], "인텐트": r["intent_name"],
                      "질문": r["question"], "생성모드": r["gen_mode"]} for r in records]
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
            "체크박스로 질문을 선택하세요 (헤더 체크박스 = 전체 선택). "
            "**결함 제외**는 기록이 남아 생성 품질 신호로 쓰이고, **영구 삭제**는 DB에서 흔적 없이 지웁니다."
        )
        event = st.dataframe(
            curation, width="stretch", hide_index=True,
            on_select="rerun", selection_mode="multi-row",
            column_config={"id": None},
            key="curation_table",
        )
        sel_rows = [i for i in event.selection.rows if i < len(curation)]
        sel_ids = curation.iloc[sel_rows]["id"].tolist()
        c1, c2, c3 = st.columns(3)
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
with tab_review:
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

    current = db.next_pending()
    if current is None:
        st.info("대기 중인 질문이 없습니다. ① 탭에서 질문을 생성하세요.")
    else:
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
with tab_result:
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

        st.markdown("**검수 결과 (제출양식 뷰)** — 셀을 드래그로 선택해 복사(Ctrl+C)한 뒤 "
                    "카카오 시트에 그대로 붙여넣으세요.")
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
                return SUBMIT_DERIVED[name](results)
            if name in results.columns:
                return results[name]
            return pd.Series("", index=results.index)  # 카카오 측 기입란 → 제목만 있는 빈 컬럼

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
