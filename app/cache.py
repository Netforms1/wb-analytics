"""Tiny on-disk cache for raw WB realization rows by period."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

CACHE_PATH = Path("data/period_cache.json")


def _key(date_from: date, date_to: date) -> str:
    return f"{date_from.isoformat()}..{date_to.isoformat()}"


def _load_all() -> dict[str, list[dict[str, Any]]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def get_rows(date_from: date, date_to: date) -> list[dict[str, Any]] | None:
    return _load_all().get(_key(date_from, date_to))


def put_rows(date_from: date, date_to: date, rows: list[dict[str, Any]]) -> None:
    data = _load_all()
    data[_key(date_from, date_to)] = rows
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False))
