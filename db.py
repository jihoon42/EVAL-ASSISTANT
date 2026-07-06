"""
db.py — 로컬 SQLite 저장소 (검수 도우미/대시보드 공용)

모든 데이터는 로컬 파일(data/eval_assistant.db)에만 저장된다.
외부 전송 없음. 카카오 제출용 xlsx는 여기서 추출한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
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
        """)
        # 구버전 DB 마이그레이션 (컬럼 없으면 추가)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
        for col in ("fail_type", "output_error_type", "output_verdict",
                    "output_comment", "search_id", "phase"):
            if col not in cols:
                conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} TEXT")


def insert_questions(records: list[dict]) -> int:
    """generate.py 레코드를 저장. 중복 id는 무시. 저장 건수 반환."""
    rows = [
        (
            r["id"], r["domain"], r["domain_name"], r["intent"], r["intent_name"],
            json.dumps(r["slots"], ensure_ascii=False),
            json.dumps(r["style"], ensure_ascii=False),
            r["question"], r["gen_mode"], r.get("model"), r["created_at"],
        )
        for r in records
    ]
    with get_conn() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO questions "
            "(id, domain, domain_name, intent, intent_name, slots_json, style_json,"
            " question, gen_mode, model, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return cur.rowcount


def next_pending() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM questions WHERE status='pending' ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


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
                search_id: str, reviewer: str, phase: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
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


def skip_question(question_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE questions SET status='skipped' WHERE id=?", (question_id,))


def reject_question(question_id: str) -> None:
    """질문 자체가 결함(비문·의미 붕괴 등)이라 검수 대상에서 제외."""
    with get_conn() as conn:
        conn.execute("UPDATE questions SET status='rejected' WHERE id=?", (question_id,))


def exemplar_records(limit: int = 6) -> list[dict]:
    """검수를 통과한(검수자가 정상 질문으로 취급한) 질문을 few-shot 예시용으로 샘플링.
    사용이 쌓일수록 예시 풀이 커져 생성 품질이 따라 올라간다."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT domain_name, intent_name, slots_json, style_json, question"
            " FROM questions WHERE status='done'"
            " ORDER BY RANDOM() LIMIT ?",
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
            " q.gen_mode AS 생성모드, q.id AS 질문ID"
            " FROM reviews r JOIN questions q ON q.id = r.question_id"
            " ORDER BY r.reviewed_at",
            conn,
        )
