"""app.py 흐름 검증 (AppTest): 페이지 렌더링·내비 복원·검수 흐름·백그라운드 생성.
conftest의 autouse 임시 DB 덕에 실사용 data/에는 절대 손대지 않는다."""
from __future__ import annotations

import time
from datetime import date, timedelta

from streamlit.testing.v1 import AppTest

import db
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
    assert any("생성 진행 중" in str(b.label) for b in at.button)
