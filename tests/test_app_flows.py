"""app.py 흐름 검증 (AppTest): 페이지 렌더링·내비 복원·검수 흐름·백그라운드 LLM 작업.
conftest의 autouse 임시 DB 덕에 실사용 data/에는 절대 손대지 않는다."""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

import db
import generate as gen
from helpers import APP_PATH, question

PAGES = ["① 질문 생성", "② 검수 진행", "③ 결과·내보내기", "④ 트렌드 시드"]


def make_app(page: str | None = None) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    if page:
        at.session_state["nav_last"] = page  # 위젯 상태 유실 시 복원 경로와 동일
    return at


def seed_minimal() -> None:
    """4개 페이지가 전부 데이터 있는 경로로 렌더링되도록 최소 데이터 구성."""
    db.insert_questions([
        question("q1", "서울 내일 비 와?", "2026-07-08T09:00:00"),
        question("q2", "부산 모레 눈 온대?", "2026-07-08T09:01:00"),
        question("q3", "강릉 주말 기온 어때?", "2026-07-08T09:02:00"),
    ])
    db.save_review("q3", "응답", "pass", "", "", "pass", "", "", "s1", "t", "cbt")
    db.add_trend_seeds([{"value": "바비", "pool": "typhoon_name", "source": "기사",
                         "expires_at": (date.today() + timedelta(days=2)).isoformat()}])


def test_all_pages_render_and_nav_restores():
    seed_minimal()
    for pg in PAGES:
        at = make_app(pg)
        at.run()
        assert not at.exception, (pg, at.exception)
        assert at.session_state["nav"] == pg  # nav 유실 → nav_last 복원
        assert not at.warning, [w.value for w in at.warning]


def _fill_review(at: AppTest, response: str, sid: str) -> None:
    at.session_state["rv_response"] = response
    at.session_state["rv_search"] = sid


def _save_current(at: AppTest, sid: str) -> None:
    """현재 활성 질문에 대해 검수를 채우고 저장 → 자동으로 다음 질문으로 진행."""
    _fill_review(at, f"응답-{sid}", sid)
    at.run()
    [b for b in at.button if "저장하고 다음" in str(b.label)][0].click().run()


def test_skip_advances_and_restore_returns_to_queue():
    seed_minimal()  # q1·q2 대기(같은 인텐트), q3 완료
    at = make_app("② 검수 진행")
    at.run()
    assert "서울 내일 비 와?" in [c.value for c in at.code]

    [b for b in at.button if "건너뛰기" in str(b.label)][0].click().run()
    assert at.session_state["nav"] == "② 검수 진행"
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]  # 같은 인텐트의 다음

    [b for b in at.button if "건너뛴 항목" in str(b.label)][0].click().run()
    assert not at.exception, at.exception
    assert db.status_counts()["pending"] == 2  # q1이 대기열로 복원


def test_advance_stays_in_intent_then_domain_then_front():
    """저장 후 자동 진행: 같은 인텐트 → 같은 도메인 → 대기열 맨 앞.
    '날씨 인텐트 끝나면 첫 질문으로 튐'(#3)을 없앤다."""
    db.insert_questions([
        question("wr1", "서울 비 와?", "2026-07-08T09:00:00",
                 intent="precip", intent_name="강수"),
        question("wr2", "부산 비 와?", "2026-07-08T09:01:00",
                 intent="precip", intent_name="강수"),
        question("wt1", "대구 기온 어때?", "2026-07-08T09:02:00",
                 intent="temp", intent_name="기온"),
        question("f1", "삼성전자 주가?", "2026-07-08T09:03:00",
                 domain="finance", domain_name="금융", intent="price", intent_name="시세"),
    ])
    at = make_app("② 검수 진행")
    at.run()
    assert "서울 비 와?" in [c.value for c in at.code]  # active = wr1

    _save_current(at, "s1")  # 강수 → 같은 인텐트 다음
    assert "부산 비 와?" in [c.value for c in at.code]

    _save_current(at, "s2")  # 강수 소진 → 같은 도메인(날씨) 다음
    assert "대구 기온 어때?" in [c.value for c in at.code]

    _save_current(at, "s3")  # 날씨 소진 → 대기열 맨 앞
    assert "삼성전자 주가?" in [c.value for c in at.code]


def test_jump_switches_when_form_clean():
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    [s for s in at.selectbox if s.key == "browse_pick"][0].select("q2").run()
    assert not at.exception, at.exception
    assert at.session_state["review_qid"] == "q2"
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]


def test_save_targets_active_not_jumped_question():
    """오매칭 원천 차단: q1을 보며 입력해 둔 뒤 q2로 점프 시도해도, 입력이 남아 있으면
    활성 질문은 q1로 고정되고 저장은 q1에 붙는다 (q2는 손대지 않음)."""
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    _fill_review(at, "q1에 대한 응답", "sid-q1")
    at.run()  # 입력이 q1(활성)에 결속

    [s for s in at.selectbox if s.key == "browse_pick"][0].select("q2").run()
    assert at.session_state["review_qid"] == "q1", "입력이 남았는데 활성 질문이 바뀜"
    assert "서울 내일 비 와?" in [c.value for c in at.code]  # 화면 여전히 q1
    assert any("입력이 사라집니다" in str(w.value) for w in at.warning)

    [b for b in at.button if "저장하고 다음" in str(b.label)][0].click().run()
    assert not at.exception, at.exception
    d1 = db.review_detail("q1")
    assert d1 is not None and d1["search_id"] == "sid-q1"
    assert db.review_detail("q2") is None, "엉뚱한 질문에 저장됨"


def test_confirm_jump_discards_input_and_switches():
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    _fill_review(at, "버릴 입력", "sid-x")
    at.run()
    [s for s in at.selectbox if s.key == "browse_pick"][0].select("q2").run()
    [b for b in at.button if str(b.label) == "버리고 이동"][0].click().run()
    assert not at.exception, at.exception
    assert at.session_state["review_qid"] == "q2"
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]
    assert db.review_detail("q1") is None
    assert at.session_state["rv_response"] == ""  # 폼 비워짐


def test_cancel_jump_keeps_active_and_input():
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    _fill_review(at, "지켜야 할 입력", "sid-y")
    at.run()
    [s for s in at.selectbox if s.key == "browse_pick"][0].select("q2").run()
    [b for b in at.button if str(b.label) == "현재 유지"][0].click().run()
    assert not at.exception, at.exception
    assert at.session_state["review_qid"] == "q1"
    assert at.session_state["browse_pick"] == "q1"  # 드롭다운 원위치
    assert at.session_state["rv_response"] == "지켜야 할 입력"  # 입력 보존
    assert "서울 내일 비 와?" in [c.value for c in at.code]


def test_review_saves_to_displayed_question():
    """정상 흐름: 현재 질문에 대해 폼을 채우고 저장하면 그 질문에 붙는다."""
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    assert "서울 내일 비 와?" in [c.value for c in at.code]  # active = q1
    _fill_review(at, "카나나 앱 응답 전문", "sid-guard-1")
    at.run()
    [b for b in at.button if "저장하고 다음" in str(b.label)][0].click().run()
    assert not at.exception, at.exception
    detail = db.review_detail("q1")
    assert detail is not None and detail["search_id"] == "sid-guard-1"


def test_edit_question_keeps_active_question():
    """질문 다듬기로 문구를 고쳐도 보던 질문이 유지되어야 한다 (#1 계열)."""
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    [s for s in at.selectbox if s.key == "browse_pick"][0].select("q2").run()
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]

    at.session_state["edit_q_q2"] = "부산 모레 눈 소식 있어?"
    [b for b in at.button if str(b.label) == "수정 저장"][0].click().run()
    assert not at.exception, at.exception
    assert at.session_state["review_qid"] == "q2", "수정 후 다른 질문으로 튐"
    assert "부산 모레 눈 소식 있어?" in [c.value for c in at.code]


def test_submit_view_deduplicates_same_name_columns():
    """기본 제출양식: '기획 검수' 2개가 보이지 않는 공백으로 구분되어 표시되고,
    같은 렌더에서 xlsx(원래 이름 그대로 중복 헤더)까지 예외 없이 빌드된다."""
    seed_minimal()
    at = make_app("③ 결과·내보내기")
    at.run()
    assert not at.exception, at.exception
    subs = [d.value for d in at.dataframe
            if "검색 키워드" in d.value.columns and "기획 검수" in d.value.columns]
    assert subs, "제출양식 뷰를 찾지 못함"
    cols = list(subs[0].columns)
    assert cols.count("기획 검수") == 1 and "기획 검수 " in cols


def test_submit_layout_editor_removes_and_adds_columns():
    """컬럼 구성 편집: 줄 삭제 = 컬럼 제거, 데이터에 없는 이름(비고) = 빈 컬럼,
    '날짜' 대신 '검수일시' 같은 데이터 컬럼 치환."""
    seed_minimal()
    at = make_app("③ 결과·내보내기")
    at.session_state["submit_layout"] = "\n".join(["검수일시", "테스터", "검색 키워드", "비고"])
    at.run()
    assert not at.exception, at.exception
    target = [d.value for d in at.dataframe
              if list(d.value.columns) == ["검수일시", "테스터", "검색 키워드", "비고"]]
    assert target, [list(d.value.columns) for d in at.dataframe]
    view = target[0]
    assert (view["비고"] == "").all()
    assert view.iloc[0]["검색 키워드"] == "강릉 주말 기온 어때?"


def test_results_domain_filter_shows_single_domain():
    """③ 도메인 필터: 고른 도메인 행만 제출양식 뷰에 남는다 (#5 도메인별 제출)."""
    db.insert_questions([
        question("w1", "서울 비 와?", "2026-07-08T09:00:00"),
        question("f1", "삼성전자 주가?", "2026-07-08T09:01:00",
                 domain="finance", domain_name="금융", intent="price", intent_name="시세"),
    ])
    db.save_review("w1", "resp1", "pass", "", "", "pass", "", "", "sid-w", "t", "cbt")
    db.save_review("f1", "resp2", "pass", "", "", "pass", "", "", "sid-f", "t", "cbt")
    at = make_app("③ 결과·내보내기")
    at.run()
    dom = [s for s in at.selectbox if s.key == "submit_domain"][0]
    assert "날씨" in dom.options and "금융" in dom.options
    dom.select("금융").run()
    assert not at.exception, at.exception
    subs = [d.value for d in at.dataframe if "기획 검수" in d.value.columns]
    assert subs, "제출양식 뷰를 찾지 못함"
    assert subs[0]["검색 키워드"].tolist() == ["삼성전자 주가?"]


def test_background_generation_e2e_and_lock():
    at = make_app()
    at.run()
    assert not at.exception, at.exception

    mode = [s for s in at.selectbox if list(s.options) == ["auto", "ollama", "template"]][0]
    mode.select("template")
    at.number_input[0].set_value(8)  # 생성 개수
    [b for b in at.button if "생성 시작" in str(b.label)][0].click().run()
    assert not at.exception, at.exception

    job, thread = at.session_state["gen_job"], at.session_state["gen_thread"]
    for _ in range(200):
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done" and job["saved"] == 8
    assert len(db.all_question_texts()) == 8
    assert not thread.is_alive()

    at.run()  # 완료 후: 사이드바 완료 표시 + 버튼 복구(잠금 해제 검증)
    assert any("생성 완료: 8건 저장" in str(s.value) for s in at.success)
    assert any("생성 시작" in str(b.label) for b in at.button)


def test_generation_button_locked_while_running():
    class FakeThread:
        def is_alive(self):
            return True

    at = make_app()
    at.session_state["gen_thread"] = FakeThread()
    at.session_state["gen_job"] = {"total": 10, "done": 3, "saved": 3, "trend": 0,
                                   "status": "running", "error": None, "mode": "template"}
    at.run()
    assert not at.exception, at.exception
    assert any("LLM 작업 진행 중" in str(b.label) for b in at.button)


def test_extraction_thread_crash_guard():
    """추출 스레드가 비정상 종료돼도 ④가 에러를 표시하고 상태를 정리한다."""
    at = make_app("④ 트렌드 시드")
    at.session_state["ext_job"] = {"status": "running", "cands": None, "error": None}
    at.session_state["ext_thread"] = None
    at.run()
    assert not at.exception, at.exception
    assert any("추출 실패" in str(e.value) for e in at.error)
    assert "ext_job" not in at.session_state


@pytest.mark.skipif(not gen.check_ollama(gen.DEFAULT_HOST), reason="Ollama 미연결")
def test_background_extraction_e2e():
    """추출 백그라운드 실행: 시작 → 스레드 완료 → 후보 인계 → 잠금 해제(버튼 복구)."""
    at = make_app("④ 트렌드 시드")
    at.session_state["trend_raw"] = ("성수동 베이커리마다 두쫀쿠를 사려는 줄이 이어졌고, "
                                     "연남동에서는 버터떡 가게가 늘고 있다.")
    at.run()
    [b for b in at.button if "후보 추출" in str(b.label)][0].click().run()
    assert not at.exception, at.exception

    job = at.session_state["ext_job"]
    for _ in range(600):  # 최대 5분 (CPU 추론 감안)
        if job["status"] != "running":
            break
        time.sleep(0.5)
    assert job["status"] == "done", job

    at.run()  # 완료 인계: 후보가 검토 단계로 넘어가고 잡 상태는 정리됨
    assert not at.exception, at.exception
    assert "ext_job" not in at.session_state
    assert "trend_cands" in at.session_state
    assert any(str(b.label) == "후보 추출" for b in at.button), "잠금이 해제되지 않음"


# ---------------------------------------------------------------
# 시드 출처 표시 (② 배경 패널 / ③ 제출본 유출 방지 / ④ 입력)
# ---------------------------------------------------------------

def _trend_seed_with_source() -> None:
    db.add_trend_seeds([{
        "value": "두쫀쿠", "pool": "hot_item", "source": "편의점 디저트 1위",
        "source_url": "https://example.com/a", "source_date": "2026-08-18",
        "evidence": "두쫀쿠가 매출 1위에 올랐다.",
        "expires_at": (date.today() + timedelta(days=7)).isoformat()}])


def test_review_shows_seed_background_for_trend_question():
    _trend_seed_with_source()
    db.insert_questions([question(
        "t1", "두쫀쿠 어디서 살 수 있어?", "2026-08-19T09:00:00",
        slots={"hot_item": "두쫀쿠"}, trend_slots=["hot_item"], seed_origin="trend")])

    at = make_app("② 검수 진행")
    at.run()
    assert not at.exception, at.exception
    labels = [e.label for e in at.expander]
    assert any("이 질문의 배경" in lb for lb in labels), labels
    body = " ".join(str(m.value) for m in at.markdown)
    assert "https://example.com/a" in body
    assert "배경 참고" in " ".join(str(c.value) for c in at.caption)


def test_review_has_no_background_panel_for_base_question():
    """기본 시드 질문에는 배경 패널이 뜨지 않아야 한다 (없는 근거를 암시하지 않게)."""
    db.insert_questions([question("b1", "서울 내일 비 와?", "2026-08-19T09:00:00")])
    at = make_app("② 검수 진행")
    at.run()
    assert not at.exception, at.exception
    assert not any("이 질문의 배경" in e.label for e in at.expander)


def test_seed_evidence_never_reaches_submit_sheet():
    """'시드근거'는 내부분석 전용 — 카카오 제출양식 뷰에 절대 나가면 안 된다."""
    _trend_seed_with_source()
    db.insert_questions([question(
        "t1", "두쫀쿠 어디서 살 수 있어?", "2026-08-19T09:00:00",
        slots={"hot_item": "두쫀쿠"}, trend_slots=["hot_item"], seed_origin="trend")])
    db.save_review("t1", "응답", "fail", "최신성", "", "pass", "", "", "s9", "t", "cbt")

    at = make_app("③ 결과·내보내기")
    at.run()
    assert not at.exception, at.exception
    assert "시드근거" in db.results_df().columns
    # 제출양식 뷰 = '기획 검수'(카카오 기입란)가 있는 프레임. 내부분석 뷰와 구분된다.
    subs = [d.value for d in at.dataframe
            if "검색 키워드" in d.value.columns and "기획 검수" in d.value.columns]
    assert subs, "제출양식 뷰를 찾지 못함"
    assert all("시드근거" not in list(s.columns) for s in subs)
    # 내부분석 뷰에는 반대로 있어야 한다 (최신성 fail 되짚기용)
    internal = [d.value for d in at.dataframe if "앱 응답" in d.value.columns]
    assert internal and "시드근거" in list(internal[0].columns)


def test_trend_tab_registers_source_fields():
    at = make_app("④ 트렌드 시드")
    at.run()
    assert not at.exception, at.exception
    keys = {i.key for i in at.text_input}
    assert {"trend_src", "trend_src_url", "trend_src_date"} <= keys, keys

    at.text_input(key="trend_src").set_value("편의점 디저트 1위")
    at.text_input(key="trend_src_url").set_value("https://example.com/a")
    at.text_input(key="trend_src_date").set_value("2026-08-18")
    at.text_area(key="trend_evidence").set_value("두쫀쿠가 매출 1위에 올랐다.")
    at.text_input(key="trend_val").set_value("두쫀쿠")
    at.run()
    at.button[-1].click().run()
    assert not at.exception, at.exception

    row = db.trend_seeds_df().iloc[0]
    assert row["값"] == "두쫀쿠"
    assert row["출처URL"] == "https://example.com/a"
    assert row["기사일자"] == "2026-08-18"


def test_trend_tab_warns_on_malformed_article_date():
    at = make_app("④ 트렌드 시드")
    at.run()
    at.text_input(key="trend_src_date").set_value("8월 18일")
    at.run()
    assert not at.exception, at.exception
    assert any("YYYY-MM-DD" in str(w.value) for w in at.warning), [w.value for w in at.warning]
