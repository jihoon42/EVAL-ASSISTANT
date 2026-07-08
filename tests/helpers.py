"""테스트 공용 헬퍼."""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
APP_PATH = str(BASE_DIR / "app.py")


def question(qid: str, text: str, created_at: str, **over) -> dict:
    """insert_questions()용 최소 질문 레코드."""
    rec = {
        "id": qid, "domain": "weather", "domain_name": "날씨",
        "intent": "t", "intent_name": "t", "slots": {}, "style": {},
        "question": text, "gen_mode": "template", "model": None,
        "created_at": created_at,
    }
    rec.update(over)
    return rec
