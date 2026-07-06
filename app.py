"""
app.py — E2E 검수 도우미 (Streamlit 로컬 앱)

흐름: ① 질문 생성 → ② 복사해서 카나나 앱에 붙여넣기 → 응답/판정 기록 → ③ 집계·xlsx 추출
실행: streamlit run app.py
모든 데이터는 로컬(data/eval_assistant.db)에만 저장된다.
"""

from __future__ import annotations

import json
from datetime import date
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
# 제출 양식 컬럼 순서 ("기획 검수"는 카카오 측 기입란 — 비워서 내보냄)
SUBMIT_COLUMNS = ["단계", "날짜", "테스터", "search ID", "검색 키워드", "1) 정확도",
                  "N 사유", "정확도 코멘트", "기획 검수", "2) LLM 출력", "오류 유형",
                  "출력 오류 코멘트", "기획 검수"]

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
                st.caption(f"검수 통과 질문 {len(exemplars)}건을 few-shot 예시로 사용합니다.")
            records = gen.generate(
                count=int(count), domains=picked, mode=mode, model=model,
                batch=int(batch), exemplars=exemplars, on_progress=on_progress,
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

    with st.expander("전체 질문 목록 보기"):
        st.dataframe(db.questions_df(), width="stretch", hide_index=True)

# ---------------------------------------------------------------
# ② 검수 진행
# ---------------------------------------------------------------
with tab_review:
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
            with st.form("review_form", clear_on_submit=True):
                response = st.text_area(
                    "카나나 앱 응답 (전문 붙여넣기 — 내부 분석용)", height=160,
                    placeholder="앱에서 받은 응답을 그대로 붙여넣으세요.",
                )
                search_id = st.text_input("search ID (앱에서 확인 가능하면 입력)")

                st.markdown("**1) 정확도**")
                acc_verdict = st.radio("정확도 판정", VERDICTS, horizontal=True,
                                       label_visibility="collapsed")
                fail_types = st.multiselect("N 사유 (fail 시 선택)", FAIL_TYPES)
                acc_comment = st.text_input("정확도 코멘트")

                st.markdown("**2) LLM 출력**")
                out_verdict = st.radio("LLM 출력 판정", VERDICTS, horizontal=True,
                                       label_visibility="collapsed")
                output_errors = st.multiselect("오류 유형 (fail 시 선택)", OUTPUT_ERROR_TYPES)
                out_comment = st.text_input("출력 오류 코멘트")

                submitted = st.form_submit_button("저장하고 다음 →", type="primary")

            if submitted:
                if not response.strip():
                    st.warning("앱 응답이 비어 있습니다. 응답 전문을 붙여넣어 주세요.")
                elif acc_verdict == "fail" and not fail_types and not acc_comment.strip():
                    st.warning("정확도 fail에는 N 사유를 선택하거나 코멘트를 적어주세요.")
                elif out_verdict == "fail" and not output_errors and not out_comment.strip():
                    st.warning("LLM 출력 fail에는 오류 유형을 선택하거나 코멘트를 적어주세요.")
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
                    st.toast("저장 완료")
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

        st.markdown("**검수 결과 전체**")
        st.dataframe(results, width="stretch", hide_index=True)

        # ---- xlsx 추출: '제출양식' 시트는 E2E 품질평가 양식 컬럼과 동일 ----
        submit = pd.DataFrame({
            "단계": results["단계"],
            "날짜": pd.to_datetime(results["검수일시"]).dt.strftime("%Y. %m. %d"),
            "테스터": results["테스터"],
            "search ID": results["search ID"],
            "검색 키워드": results["검색 키워드"],
            "1) 정확도": results["1) 정확도"],
            "N 사유": results["N 사유"],
            "정확도 코멘트": results["정확도 코멘트"],
            "기획 검수": "",
            "2) LLM 출력": results["2) LLM 출력"],
            "오류 유형": results["오류 유형"],
            "출력 오류 코멘트": results["출력 오류 코멘트"],
            "기획 검수 ": "",  # 양식상 동일 이름 컬럼 2개
        })
        submit.columns = SUBMIT_COLUMNS

        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            submit.to_excel(writer, sheet_name="제출양식", index=False)
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
