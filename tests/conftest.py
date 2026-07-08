"""pytest 공용 설정.

- 저장소 루트를 임포트 경로에 추가 (db / generate / app.py 상대 참조)
- 모든 테스트를 테스트별 임시 DB로 격리 — 실사용 data/eval_assistant.db는
  어떤 테스트도 절대 건드리지 않는다 (autouse).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import db  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """테스트마다 빈 임시 DB를 쓰도록 db.DB_PATH를 바꿔치기한다."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "eval_assistant_test.db")
    db.init_db()
    yield
