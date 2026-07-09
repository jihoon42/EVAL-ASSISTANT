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


def test_skip_then_restore_returns_previous_question_on_page2():
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    assert "서울 내일 비 와?" in [c.value for c in at.code]

    skip = [b for b in at.button if "건너뛰기" in str(b.label)][0]
    skip.click().run()
    assert at.session_state["nav"] == "② 검수 진행"
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]

    restore = [b for b in at.button if "건너뛴 항목" in str(b.label)][0]
    restore.click().run()
    assert not at.exception, at.exception
    assert at.session_state["nav"] == "② 검수 진행", "페이지 튕김"
    assert "서울 내일 비 와?" in [c.value for c in at.code], "이전 질문 미복귀"


def test_group_review_filter_narrows_queue():
    db.insert_questions([
        question("a1", "삼성전자 오늘 주가 얼마야?", "2026-07-08T09:00:00",
                 domain="finance", domain_name="금융", intent="stock_price",
                 intent_name="시세 조회", slots={"stock": "삼성전자"}),
        question("a2", "서울 내일 비 와?", "2026-07-08T09:01:00",
                 intent="precip", intent_name="강수"),
        question("a3", "카카오 오늘 주가 알려줘", "2026-07-08T09:02:00",
                 domain="finance", domain_name="금융", intent="stock_price",
                 intent_name="시세 조회", slots={"stock": "카카오"}),
    ])
    at = make_app("② 검수 진행")
    at.run()
    group = [s for s in at.selectbox if s.key == "review_group"][0]
    assert group.value == "전체"
    group.select("금융/시세 조회").run()
    assert not at.exception, at.exception
    pick = [s for s in at.selectbox if s.key == "review_pick"][0]
    assert list(pick.options) == [
        "1. [금융/시세 조회] 삼성전자 오늘 주가 얼마야?",
        "2. [금융/시세 조회] 카카오 오늘 주가 알려줘",
    ]
    assert "삼성전자 오늘 주가 얼마야?" in [c.value for c in at.code]


def test_pick_question_stays_on_page2():
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    pick = [s for s in at.selectbox if s.key == "review_pick"][0]
    pick.select("q2").run()
    assert not at.exception, at.exception
    assert at.session_state["nav"] == "② 검수 진행"
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]


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


def test_edit_question_keeps_current_selection():
    """질문 다듬기로 문구를 고쳐도 보던 질문이 유지되어야 한다
    (라벨 변경 → 드롭다운 위젯 재생성 → 선택 초기화로 맨 앞 질문으로 튀던 버그)."""
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    pick = [s for s in at.selectbox if s.key == "review_pick"][0]
    pick.select("q2").run()
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]

    at.session_state["edit_q_q2"] = "부산 모레 눈 소식 있어?"
    [b for b in at.button if str(b.label) == "수정 저장"][0].click().run()
    assert not at.exception, at.exception
    assert at.session_state["review_pick"] == "q2", "수정 후 다른 질문으로 튐"
    assert "부산 모레 눈 소식 있어?" in [c.value for c in at.code], "수정된 질문이 화면에 없음"


def test_force_pick_rescues_lost_selection():
    """드롭다운 위젯 상태가 유실돼도(브라우저 desync 등) 직전 동작이 지정한 질문을
    서버 측에서 강제 유지한다 — 질문 다듬기 후 맨 앞 질문으로 튀던 실사용 버그의 방어."""
    seed_minimal()
    at = make_app("② 검수 진행")
    at.session_state["review_force_pick"] = "q2"  # 위젯 상태(review_pick)는 없음 = 유실 상황
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["review_pick"] == "q2"
    assert "부산 모레 눈 온대?" in [c.value for c in at.code]
    assert "review_force_pick" not in at.session_state  # 1회성으로 소비됨


def test_group_filter_survives_queue_changes():
    """묶음 필터가 건수(라벨) 변화에도 풀리지 않아야 한다."""
    db.insert_questions([
        question("a1", "삼성전자 오늘 주가 얼마야?", "2026-07-09T09:00:00",
                 domain="finance", domain_name="금융", intent="stock_price",
                 intent_name="시세 조회", slots={"stock": "삼성전자"}),
        question("a2", "서울 내일 비 와?", "2026-07-09T09:01:00",
                 intent="precip", intent_name="강수"),
        question("a3", "카카오 오늘 주가 알려줘", "2026-07-09T09:02:00",
                 domain="finance", domain_name="금융", intent="stock_price",
                 intent_name="시세 조회", slots={"stock": "카카오"}),
    ])
    at = make_app("② 검수 진행")
    at.run()
    group = [s for s in at.selectbox if s.key == "review_group"][0]
    group.select("금융/시세 조회").run()
    assert "삼성전자 오늘 주가 얼마야?" in [c.value for c in at.code]

    # 건너뛰기 → 대기열·건수 변화. 필터는 유지되고 같은 묶음의 다음 질문이 나와야 한다.
    [b for b in at.button if "건너뛰기" in str(b.label)][0].click().run()
    assert not at.exception, at.exception
    group = [s for s in at.selectbox if s.key == "review_group"][0]
    assert group.value == "금융/시세 조회", "묶음 필터가 풀림"
    assert "카카오 오늘 주가 알려줘" in [c.value for c in at.code]


def _fill_review_form(at: AppTest) -> None:
    at.session_state["rv_response"] = "카나나 앱 응답 전문"
    at.session_state["rv_search"] = "sid-guard-1"


def test_review_saves_to_displayed_question():
    """정상 흐름: 현재 질문에 대해 폼을 채우고 저장하면 그 질문에 붙는다."""
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    assert "서울 내일 비 와?" in [c.value for c in at.code]  # current = q1
    _fill_review_form(at)
    [b for b in at.button if "저장하고 다음" in str(b.label)][0].click().run()
    assert not at.exception, at.exception
    detail = db.review_detail("q1")
    assert detail is not None and detail["search_id"] == "sid-guard-1"


def test_review_save_blocked_once_when_question_changes_under_filled_form():
    """오매칭 방지: 입력이 남은 채 질문이 바뀌면 저장을 1회 차단하고,
    확인(재클릭) 후에는 화면의 질문으로 저장한다."""
    seed_minimal()
    at = make_app("② 검수 진행")
    at.run()
    _fill_review_form(at)  # q1을 보며 입력해 둔 상황
    at.run()               # 입력이 q1에 결속됨 (review_form_target=q1)

    # 드롭다운으로 질문 변경 → current가 q2로 바뀌지만 폼 입력은 유지
    pick = [s for s in at.selectbox if s.key == "review_pick"][0]
    pick.select("q2").run()
    assert any("질문이 바뀌었습니다" in str(w.value) for w in at.warning)

    # 저장 시도 1: 차단되어 아무 데도 저장되지 않아야 함
    [b for b in at.button if "저장하고 다음" in str(b.label)][0].click().run()
    assert db.review_detail("q1") is None and db.review_detail("q2") is None
    assert any("저장을 한 번 막았습니다" in str(w.value) for w in at.warning)

    # 저장 시도 2(확인 후): 화면에 보이는 q2로 저장
    [b for b in at.button if "저장하고 다음" in str(b.label)][0].click().run()
    assert db.review_detail("q1") is None
    detail = db.review_detail("q2")
    assert detail is not None and detail["search_id"] == "sid-guard-1"


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
