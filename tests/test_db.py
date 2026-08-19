"""db.py 검증: 대기열 순서, 정리, 검수 수정/되돌리기, 트렌드 시드, few-shot 안배."""
from __future__ import annotations

from datetime import date, timedelta

import db
from helpers import question


def seed_three_domains(n_done: int = 9) -> None:
    """도메인 3개 × 4건(12건) 저장, 앞 n_done건은 검수 완료 처리."""
    recs = []
    for i in range(12):
        dom = ["weather", "local", "finance"][i % 3]
        recs.append(question(
            f"q{i:03d}", f"도메인 {dom} 전용 테스트 질문 {i}번입니다",
            f"2026-07-06T10:{i:02d}:00", domain=dom, domain_name=dom))
    assert db.insert_questions(recs) == 12
    for i in range(n_done):
        db.save_review(f"q{i:03d}", "응답", "pass", "", "", "pass", "", "",
                       f"s{i}", "tester", "cbt")


# ---------------------------------------------------------------
# few-shot 예시 풀
# ---------------------------------------------------------------

def test_exemplar_records_domain_stratified():
    seed_three_domains()
    for _ in range(20):
        ex = db.exemplar_records(6)
        assert len(ex) == 6
        per_domain = {}
        for e in ex:
            per_domain[e["domain_name"]] = per_domain.get(e["domain_name"], 0) + 1
        assert max(per_domain.values()) == 2, f"도메인 쏠림: {per_domain}"


# ---------------------------------------------------------------
# 대기열 순서: 먼저 검수 > 직접 추가 > 생성일시
# ---------------------------------------------------------------

def test_queue_order_and_prioritize():
    db.insert_questions([
        question("g1", "서울 내일 비 와?", "2026-07-06T09:00:00"),
        question("g2", "부산 모레 기온 어때?", "2026-07-06T09:01:00"),
        question("m1", "태풍 바비 어디쯤이야?", "2026-07-07T10:00:00", gen_mode="manual"),
        question("g3", "강릉 주말 눈 온대?", "2026-07-07T11:00:00"),
    ])
    db.skip_question("g3")

    assert [q["id"] for q in db.pending_queue()] == ["m1", "g1", "g2"]
    assert db.next_pending()["id"] == "m1"

    # '먼저 검수'는 건너뜀도 대기로 복원하며 manual보다 앞선다
    assert db.prioritize_questions(["g3"]) == 1
    assert [q["id"] for q in db.pending_queue()] == ["g3", "m1", "g1", "g2"]

    # 정리 표: 검수 순서 정렬 + 대기 건 순번
    df = db.curation_df()
    assert df["id"].tolist() == ["g3", "m1", "g1", "g2"]
    assert df["순번"].tolist() == ["1", "2", "3", "4"]


def test_curation_df_skipped_at_bottom_without_number():
    db.insert_questions([
        question("g1", "서울 내일 비 와?", "2026-07-06T09:00:00"),
        question("g2", "부산 모레 기온 어때?", "2026-07-06T09:01:00"),
    ])
    db.skip_question("g2")
    df = db.curation_df()
    assert df["id"].tolist() == ["g1", "g2"]
    assert df["순번"].tolist() == ["1", ""]
    assert df.iloc[-1]["상태"] == "skipped"


# ---------------------------------------------------------------
# 질문 정리 / 다듬기
# ---------------------------------------------------------------

def test_reject_delete_and_edit_text():
    db.insert_questions([
        question("g1", "서울 내일 비 와?", "2026-07-06T09:00:00"),
        question("g2", "부산 모레 기온 어때?", "2026-07-06T09:01:00"),
        question("g3", "강릉 주말 눈 온대?", "2026-07-06T09:02:00"),
    ])
    db.reject_questions(["g2"])
    assert db.status_counts()["rejected"] == 1

    # 질문 다듬기 → 검수 통과 시 고친 문구가 few-shot 예시로
    db.update_question_text("g1", "서울 내일 비 올까?")
    assert db.next_pending()["question"] == "서울 내일 비 올까?"
    db.save_review("g1", "응답", "pass", "", "", "pass", "", "", "s1", "t", "cbt")
    assert "서울 내일 비 올까?" in {e["question"] for e in db.exemplar_records(6)}

    # 영구 삭제: 검수 기록 딸린 질문도 흔적 없이
    assert db.delete_questions(["g1", "g3"]) == 2
    assert db.review_detail("g1") is None
    assert db.all_question_texts() == ["부산 모레 기온 어때?"]


# ---------------------------------------------------------------
# 검수 저장/수정/되돌리기, 붙여넣기 실수 감지
# ---------------------------------------------------------------

def test_review_edit_reopen_and_duplicate_fields():
    db.insert_questions([
        question("q1", "서울 내일 비 와?", "2026-07-06T09:00:00"),
        question("q2", "부산 주말 날씨 어때?", "2026-07-06T09:01:00"),
    ])
    db.save_review("q1", "응답A", "pass", "", "", "pass", "", "", "sid-001", "김", "cbt")

    # 다른 질문에 같은 search ID/응답을 붙여넣으면 감지, 자기 자신은 제외
    assert db.duplicate_review_fields("q2", "sid-001", "새 응답") == ["search ID"]
    assert db.duplicate_review_fields("q2", "sid-002", "응답A") == ["앱 응답"]
    assert db.duplicate_review_fields("q1", "sid-001", "응답A") == []
    assert db.duplicate_review_fields("q2", "", "") == []

    # 수정 시 검수일시 보존 (INSERT OR REPLACE)
    orig_ts = db.review_detail("q1")["reviewed_at"]
    db.save_review("q1", "응답A(수정)", "fail", "최신성", "날짜 틀림", "pass", "", "",
                   "sid-001", "김", "cbt", reviewed_at=orig_ts)
    after = db.review_detail("q1")
    assert after["reviewed_at"] == orig_ts and after["verdict"] == "fail"
    assert db.status_counts()["done"] == 1

    # 재검수 되돌리기: 기록 삭제 + 대기 복귀(가장 오래된 건이라 맨 앞)
    db.reopen_review("q1")
    assert db.review_detail("q1") is None
    assert db.next_pending()["id"] == "q1"


# ---------------------------------------------------------------
# 트렌드 시드
# ---------------------------------------------------------------

def test_trend_seeds_crud_expiry_and_seed_origin():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    n = db.add_trend_seeds([
        {"value": "두쫀쿠", "pool": "food_category", "source": "A", "expires_at": tomorrow},
        {"value": "두쫀쿠", "pool": "food_category", "source": "B", "expires_at": tomorrow},  # 중복
        {"value": "울릉도", "pool": "region", "source": "C", "expires_at": None},  # 무기한
        {"value": "옛유행", "pool": "food_category", "source": "D", "expires_at": yesterday},  # 만료
    ])
    assert n == 3

    active = db.active_trend_seeds()
    assert active["food_category"] == ["두쫀쿠"] and active["region"] == ["울릉도"]

    tdf = db.trend_seeds_df()
    status = dict(zip(tdf["값"], tdf["상태"]))
    assert status["두쫀쿠"] == "활성" and status["옛유행"] == "만료/중지"

    gone = int(tdf.loc[tdf["값"] == "옛유행", "id"].iloc[0])
    assert db.delete_trend_seeds([gone]) == 1

    # seed_origin: 값 없는 구형 레코드는 base로 저장
    db.insert_questions([question("old1", "구버전 질문", "2026-07-01T00:00:00")])
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT seed_origin FROM questions WHERE id='old1'").fetchone()[0] == "base"


def test_all_question_combo_pairs():
    db.insert_questions([question(
        "q1", "디딤돌대출 조건이 어떻게 돼?", "2026-07-06T09:00:00",
        intent="condition", slots={"fin_condition_term": "디딤돌대출"})])
    pairs = db.all_question_combo_pairs()
    assert pairs == [("condition", {"fin_condition_term": "디딤돌대출"})]


# ---------------------------------------------------------------
# 시드 출처 → 질문 배경 연결 (② 탭 '이 질문의 배경')
# ---------------------------------------------------------------

def trend_question(qid: str, slots: dict, trend_slots: list[str], **over) -> dict:
    return question(qid, "테스트 질문입니다", "2026-08-19T09:00:00",
                    slots=slots, trend_slots=trend_slots, seed_origin="trend", **over)


def test_question_sources_links_question_to_article():
    db.add_trend_seeds([{
        "value": "두쫀쿠", "pool": "hot_item", "source": "편의점 디저트 1위",
        "source_url": "https://example.com/a", "source_date": "2026-08-18",
        "evidence": "두쫀쿠가 매출 1위에 올랐다.", "expires_at": None}])
    db.insert_questions([trend_question("q1", {"hot_item": "두쫀쿠"}, ["hot_item"])])

    srcs = db.question_sources("q1")
    assert len(srcs) == 1
    assert srcs[0]["source_url"] == "https://example.com/a"
    assert srcs[0]["source_date"] == "2026-08-18"
    assert "매출 1위" in srcs[0]["evidence"]


def test_question_sources_only_returns_trend_sourced_slots():
    """기본 시드에서 온 슬롯은 출처가 없다 — 없는 근거를 지어내지 않는지."""
    db.add_trend_seeds([{"value": "두쫀쿠", "pool": "hot_item", "source": "기사",
                         "expires_at": None}])
    db.insert_questions([trend_question(
        "q1", {"hot_item": "두쫀쿠", "anchor": "가평역"}, ["hot_item"])])
    srcs = db.question_sources("q1")
    assert [s["value"] for s in srcs] == ["두쫀쿠"]


def test_question_sources_empty_for_base_and_deleted_seed():
    db.insert_questions([question("base1", "기본 시드 질문", "2026-08-19T09:00:00")])
    assert db.question_sources("base1") == []
    assert db.question_sources("없는id") == []

    db.add_trend_seeds([{"value": "바비", "pool": "typhoon_name", "source": "기사",
                         "expires_at": None}])
    db.insert_questions([trend_question("q2", {"typhoon_name": "바비"}, ["typhoon_name"])])
    assert len(db.question_sources("q2")) == 1
    sid = int(db.trend_seeds_df().iloc[0]["id"])
    db.delete_trend_seeds([sid])
    # 시드를 지우면 배경은 사라지되 질문·검수 기록은 멀쩡해야 한다
    assert db.question_sources("q2") == []
    assert db.next_pending() is not None


def test_results_df_carries_seed_evidence():
    db.add_trend_seeds([{
        "value": "두쫀쿠", "pool": "hot_item", "source": "편의점 디저트 1위",
        "source_url": "https://example.com/a", "source_date": "2026-08-18",
        "expires_at": None}])
    db.insert_questions([
        trend_question("q1", {"hot_item": "두쫀쿠"}, ["hot_item"]),
        question("q2", "기본 시드 질문", "2026-08-19T09:00:00"),
    ])
    for qid in ("q1", "q2"):
        db.save_review(qid, "응답", "fail", "최신성", "", "pass", "", "",
                       f"sid-{qid}", "tester", "cbt")

    res = db.results_df().set_index("질문ID")
    assert "https://example.com/a" in res.loc["q1", "시드근거"]
    assert "2026-08-18" in res.loc["q1", "시드근거"]
    assert res.loc["q2", "시드근거"] == ""


def test_migration_keeps_old_rows_and_adds_columns():
    """출처 기능 이전 스키마의 DB를 열어도 데이터가 살아 있고 컬럼만 붙는지."""
    with db.get_conn() as conn:
        for col in ("trend_slots",):
            conn.execute(f"ALTER TABLE questions DROP COLUMN {col}")
        for col in ("source_url", "source_date", "evidence"):
            conn.execute(f"ALTER TABLE trend_seeds DROP COLUMN {col}")
        conn.execute(
            "INSERT INTO questions (id, domain, domain_name, intent, intent_name,"
            " slots_json, style_json, question, gen_mode, created_at, seed_origin)"
            " VALUES ('old1','weather','날씨','t','t','{}','{}','구버전 질문',"
            "'template','2026-07-01T00:00:00','trend')")
        conn.execute(
            "INSERT INTO trend_seeds (value, pool, source, added_at)"
            " VALUES ('옛시드','hot_item','옛 메모','2026-07-01T00:00:00')")

    db.init_db()   # 마이그레이션

    with db.get_conn() as conn:
        qcols = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
        scols = {r[1] for r in conn.execute("PRAGMA table_info(trend_seeds)")}
    assert "trend_slots" in qcols
    assert {"source_url", "source_date", "evidence"} <= scols

    assert db.trend_seeds_df().iloc[0]["출처"] == "옛 메모"
    # 구버전 질문은 배경이 없을 뿐 조회는 정상 (② 탭이 안내 문구로 처리)
    assert db.question_sources("old1") == []
