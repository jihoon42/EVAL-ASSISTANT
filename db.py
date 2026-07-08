"""
db.py — 로컬 SQLite 저장소 (검수 도우미/대시보드 공용)

모든 데이터는 로컬 파일(data/eval_assistant.db)에만 저장된다.
외부 전송 없음. 카카오 제출용 xlsx는 여기서 추출한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "eval_assistant.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS questions (
            id          TEXT PRIMARY KEY,
            domain      TEXT NOT NULL,
            domain_name TEXT NOT NULL,
            intent      TEXT NOT NULL,
            intent_name TEXT NOT NULL,
            slots_json  TEXT NOT NULL,
            style_json  TEXT NOT NULL,
            question    TEXT NOT NULL,
            gen_mode    TEXT NOT NULL,
            model       TEXT,
            created_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending'  -- pending | done | skipped
        );
        CREATE TABLE IF NOT EXISTS reviews (
            question_id       TEXT PRIMARY KEY REFERENCES questions(id),
            response          TEXT NOT NULL,   -- 앱 응답 전문 (내부 분석용)
            verdict           TEXT NOT NULL,   -- 1) 정확도: pass | fail
            fail_type         TEXT,            -- N 사유 (쉼표 구분 복수 가능)
            reason            TEXT,            -- 정확도 코멘트
            output_verdict    TEXT,            -- 2) LLM 출력: pass | fail
            output_error_type TEXT,            -- 오류 유형 (쉼표 구분 복수 가능)
            output_comment    TEXT,            -- 출력 오류 코멘트
            search_id         TEXT,            -- 카나나 앱 search ID
            phase             TEXT,            -- 검수 단계 라벨 (예: cbt)
            reviewer          TEXT,            -- 테스터
            reviewed_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trend_seeds (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            value      TEXT NOT NULL,           -- 시드 값 (예: 두쫀쿠)
            pool       TEXT NOT NULL,           -- 주입할 슬롯 풀 이름 (예: food_category)
            source     TEXT,                    -- 출처 메모 (기사 제목·URL)
            added_at   TEXT NOT NULL,
            expires_at TEXT,                    -- YYYY-MM-DD, NULL이면 무기한
            active     INTEGER NOT NULL DEFAULT 1,
            UNIQUE(value, pool)
        );
        """)
        # 구버전 DB 마이그레이션 (컬럼 없으면 추가)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
        for col in ("fail_type", "output_error_type", "output_verdict",
                    "output_comment", "search_id", "phase"):
            if col not in cols:
                conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} TEXT")
        qcols = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        if "seed_origin" not in qcols:
            # base=기본 시드, trend=트렌드 시드 사용 질문 (최신성 분석용)
            conn.execute(
                "ALTER TABLE questions ADD COLUMN seed_origin TEXT NOT NULL DEFAULT 'base'")
        if "priority" not in qcols:
            # 1이면 검수 대기열 맨 앞 ("먼저 검수" 지정)
            conn.execute(
                "ALTER TABLE questions ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")


def insert_questions(records: list[dict]) -> int:
    """generate.py 레코드를 저장. 중복 id는 무시. 저장 건수 반환."""
    rows = [
        (
            r["id"], r["domain"], r["domain_name"], r["intent"], r["intent_name"],
            json.dumps(r["slots"], ensure_ascii=False),
            json.dumps(r["style"], ensure_ascii=False),
            r["question"], r["gen_mode"], r.get("model"), r["created_at"],
            r.get("seed_origin", "base"),
        )
        for r in records
    ]
    with get_conn() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO questions "
            "(id, domain, domain_name, intent, intent_name, slots_json, style_json,"
            " question, gen_mode, model, created_at, seed_origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return cur.rowcount


def next_pending() -> dict | None:
    """다음 검수 대상. '먼저 검수' 지정(priority=1) → 직접 추가 질문(manual) → 생성일시 순."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM questions WHERE status='pending'"
            " ORDER BY priority DESC, gen_mode != 'manual', created_at, rowid LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def pending_queue() -> list[dict]:
    """검수 대기열 전체를 실제 검수 순서로 반환 (② 탭 선택 검수용)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM questions WHERE status='pending'"
            " ORDER BY priority DESC, gen_mode != 'manual', created_at, rowid"
        ).fetchall()
        return [dict(r) for r in rows]


def prioritize_questions(ids: list[str]) -> int:
    """선택한 질문을 검수 대기열 맨 앞으로. 건너뜀/결함 제외 상태였다면 대기로 복원한다."""
    with get_conn() as conn:
        cur = conn.executemany(
            "UPDATE questions SET priority=1, status='pending' WHERE id=?",
            [(i,) for i in ids])
        return cur.rowcount


def update_question_text(question_id: str, new_text: str) -> None:
    """질문 문구 수정 (검수 전 다듬기용). 고친 질문이 검수를 통과하면 few-shot 예시가 된다."""
    with get_conn() as conn:
        conn.execute("UPDATE questions SET question=? WHERE id=?", (new_text, question_id))


def status_counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM questions GROUP BY status").fetchall()
    counts = {"pending": 0, "done": 0, "skipped": 0, "rejected": 0}
    for r in rows:
        counts[r["status"]] = r["n"]
    return counts


def save_review(question_id: str, response: str,
                accuracy_verdict: str, fail_type: str, accuracy_comment: str,
                output_verdict: str, output_error_type: str, output_comment: str,
                search_id: str, reviewer: str, phase: str,
                reviewed_at: str | None = None) -> None:
    """reviewed_at을 주면 그 값을 유지한다 (기존 검수 수정 시 검수일시 보존용)."""
    now = reviewed_at or datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reviews (question_id, response, verdict, fail_type, reason,"
            " output_verdict, output_error_type, output_comment, search_id, phase,"
            " reviewer, reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (question_id, response, accuracy_verdict, fail_type, accuracy_comment,
             output_verdict, output_error_type, output_comment, search_id, phase,
             reviewer, now),
        )
        conn.execute("UPDATE questions SET status='done' WHERE id=?", (question_id,))


def review_detail(question_id: str) -> dict | None:
    """검수 1건 + 질문 정보 (수정 폼 프리필용)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.*, q.question, q.domain_name, q.intent_name"
            " FROM reviews r JOIN questions q ON q.id = r.question_id"
            " WHERE r.question_id = ?", (question_id,)).fetchone()
        return dict(row) if row else None


def reopen_review(question_id: str) -> None:
    """검수 기록을 지우고 질문을 재검수 대기로 되돌린다 (저장 실수 복구용)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM reviews WHERE question_id=?", (question_id,))
        conn.execute("UPDATE questions SET status='pending' WHERE id=?", (question_id,))


def duplicate_review_fields(question_id: str, search_id: str, response: str) -> list[str]:
    """다른 검수 기록과 값이 똑같은 필드 목록 (직전 답변 붙여넣기 실수 감지용)."""
    dups: list[str] = []
    with get_conn() as conn:
        if search_id and conn.execute(
                "SELECT 1 FROM reviews WHERE search_id=? AND question_id!=? LIMIT 1",
                (search_id, question_id)).fetchone():
            dups.append("search ID")
        if response and conn.execute(
                "SELECT 1 FROM reviews WHERE response=? AND question_id!=? LIMIT 1",
                (response, question_id)).fetchone():
            dups.append("앱 응답")
    return dups


def skip_question(question_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE questions SET status='skipped' WHERE id=?", (question_id,))


def reject_question(question_id: str) -> None:
    """질문 자체가 결함(비문·의미 붕괴 등)이라 검수 대상에서 제외."""
    with get_conn() as conn:
        conn.execute("UPDATE questions SET status='rejected' WHERE id=?", (question_id,))


def exemplar_records(limit: int = 6) -> list[dict]:
    """검수를 통과한(검수자가 정상 질문으로 취급한) 질문을 few-shot 예시용으로 샘플링.
    도메인별로 돌아가며 뽑아(라운드 로빈) 특정 도메인 쏠림을 막는다.
    사용이 쌓일수록 예시 풀이 커져 생성 품질이 따라 올라간다."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT domain_name, intent_name, slots_json, style_json, question FROM ("
            "  SELECT *, ROW_NUMBER() OVER (PARTITION BY domain ORDER BY RANDOM()) AS rn"
            "  FROM questions WHERE status='done'"
            ") ORDER BY rn LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "domain_name": r["domain_name"],
            "intent_name": r["intent_name"],
            "slots": json.loads(r["slots_json"]),
            "style": json.loads(r["style_json"]),
            "question": r["question"],
        }
        for r in rows
    ]


def restore_skipped() -> int:
    with get_conn() as conn:
        cur = conn.execute("UPDATE questions SET status='pending' WHERE status='skipped'")
        return cur.rowcount


def all_question_texts() -> list[str]:
    """지금까지 생성된 모든 질문 텍스트 (상태 무관).
    생성 시 넘겨주면 과거 실행분과의 완전/준중복을 막는다."""
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT question FROM questions")]


def all_question_combo_pairs() -> list[tuple[str, dict]]:
    """지금까지 다룬 (인텐트, 슬롯) 쌍 전체 — 같은 조합을 문구만 바꿔 또 묻지 않도록
    생성 시 회피 목록으로 쓴다."""
    with get_conn() as conn:
        rows = conn.execute("SELECT intent, slots_json FROM questions").fetchall()
    return [(r["intent"], json.loads(r["slots_json"])) for r in rows]


def reject_questions(ids: list[str]) -> int:
    """여러 질문을 한꺼번에 결함 제외. 기록은 남고 few-shot 예시에서 배제된다."""
    with get_conn() as conn:
        cur = conn.executemany(
            "UPDATE questions SET status='rejected' WHERE id=?", [(i,) for i in ids])
        return cur.rowcount


def delete_questions(ids: list[str]) -> int:
    """질문을 DB에서 완전히 제거 (흔적 없음). 딸린 검수 기록이 있으면 함께 지운다."""
    with get_conn() as conn:
        conn.executemany("DELETE FROM reviews WHERE question_id=?", [(i,) for i in ids])
        cur = conn.executemany("DELETE FROM questions WHERE id=?", [(i,) for i in ids])
        return cur.rowcount


def curation_df() -> pd.DataFrame:
    """검수 전 정리 대상 질문 목록 (검수 완료 건 제외).
    실제 검수 순서로 정렬하고 대기 건에 순번을 붙인다 — '먼저 검수'의 효과가 눈에 보이게."""
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT id, status AS 상태, priority AS 우선, domain_name AS 도메인,"
            " intent_name AS 인텐트, question AS 질의, gen_mode AS 생성모드,"
            " created_at AS 생성일시"
            " FROM questions WHERE status != 'done'"
            " ORDER BY status != 'pending', priority DESC, gen_mode != 'manual',"
            " created_at, rowid",
            conn,
        )
    if not df.empty:
        df.insert(1, "순번", "")
        pend = df["상태"] == "pending"
        df.loc[pend, "순번"] = [str(i) for i in range(1, int(pend.sum()) + 1)]
    return df


# ---------------------------------------------------------------
# 트렌드 시드 (최신 기사·리뷰에서 추출한 엔티티 — 생성 시 슬롯에 혼입)
# ---------------------------------------------------------------

def add_trend_seeds(items: list[dict]) -> int:
    """트렌드 시드 등록. (값, 풀) 중복은 무시. items: {value, pool, source, expires_at}"""
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO trend_seeds (value, pool, source, added_at, expires_at)"
            " VALUES (?,?,?,?,?)",
            [(i["value"], i["pool"], i.get("source", ""), now, i.get("expires_at"))
             for i in items],
        )
        return cur.rowcount


def active_trend_seeds() -> dict[str, list[str]]:
    """유효한(활성 + 미만료) 트렌드 시드를 {풀: [값, ...]} 형태로 반환 (생성 주입용)."""
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pool, value FROM trend_seeds WHERE active=1"
            " AND (expires_at IS NULL OR expires_at >= ?)",
            (today,),
        ).fetchall()
    pools: dict[str, list[str]] = {}
    for r in rows:
        pools.setdefault(r["pool"], []).append(r["value"])
    return pools


def trend_seeds_df() -> pd.DataFrame:
    """트렌드 시드 전체 목록 (관리 UI용, 만료 여부 표시)."""
    today = date.today().isoformat()
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT id, value AS 값, pool AS 풀, source AS 출처,"
            " added_at AS 등록일시, expires_at AS 만료일, active"
            " FROM trend_seeds ORDER BY added_at DESC, id DESC",
            conn,
        )
    if not df.empty:
        df["상태"] = ["활성" if a == 1 and (e == "" or e >= today) else "만료/중지"
                      for a, e in zip(df["active"], df["만료일"].fillna(""))]
        df = df.drop(columns=["active"])
    return df


def delete_trend_seeds(ids: list[int]) -> int:
    with get_conn() as conn:
        cur = conn.executemany(
            "DELETE FROM trend_seeds WHERE id=?", [(int(i),) for i in ids])
        return cur.rowcount


def questions_df() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT id, domain_name AS 도메인, intent_name AS 인텐트, question AS 질의,"
            " gen_mode AS 생성모드, status AS 상태, created_at AS 생성일시 FROM questions"
            " ORDER BY created_at DESC, rowid DESC",
            conn,
        )


def results_df() -> pd.DataFrame:
    """검수 완료 건 조인 뷰 (xlsx 추출/대시보드용). 컬럼명은 제출 양식 기준."""
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT r.phase AS 단계, r.reviewed_at AS 검수일시, r.reviewer AS 테스터,"
            " r.search_id AS 'search ID', q.question AS '검색 키워드',"
            " r.verdict AS '1) 정확도', r.fail_type AS 'N 사유', r.reason AS '정확도 코멘트',"
            " r.output_verdict AS '2) LLM 출력', r.output_error_type AS '오류 유형',"
            " r.output_comment AS '출력 오류 코멘트',"
            " q.domain_name AS 도메인, q.intent_name AS 인텐트, r.response AS '앱 응답',"
            " q.gen_mode AS 생성모드, q.seed_origin AS 시드출처, q.id AS 질문ID"
            " FROM reviews r JOIN questions q ON q.id = r.question_id"
            " ORDER BY r.reviewed_at",
            conn,
        )
