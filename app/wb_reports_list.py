"""Index of реализационных отчётов: один тяжёлый запрос за 35 дней → группировка по report_id."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from app.cache import get_rows, put_rows
from app.wb_client import fetch_realization_report

INDEX_PATH = Path("data/reports_index.json")
WINDOW_DAYS = 35


@dataclass
class ReportMeta:
    id: str
    date_from: str  # ISO YYYY-MM-DD
    date_to: str
    rows_count: int
    payout: float


def _load_index() -> dict:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_index(idx: dict) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False))


def _row_payout(r: dict) -> float:
    income = (r.get("ppvz_for_pay") or 0) + (r.get("additional_payment") or 0)
    expense = sum(
        r.get(c) or 0
        for c in (
            "delivery_rub",
            "penalty",
            "storage_fee",
            "deduction",
            "acceptance",
            "rebill_logistic_cost",
        )
    )
    return float(income) - float(expense)


def _build_index(rows: list[dict]) -> dict:
    by_id: dict[str, dict] = {}
    for r in rows:
        rid = r.get("realizationreport_id")
        if rid is None:
            continue
        rid = str(rid)
        bucket = by_id.setdefault(
            rid,
            {
                "date_from": (r.get("date_from") or "")[:10],
                "date_to": (r.get("date_to") or "")[:10],
                "rows": [],
            },
        )
        # На случай если у первой строки даты пустые — берём из любой следующей
        if not bucket["date_from"] and r.get("date_from"):
            bucket["date_from"] = r["date_from"][:10]
        if not bucket["date_to"] and r.get("date_to"):
            bucket["date_to"] = r["date_to"][:10]
        bucket["rows"].append(r)
    return by_id


async def refresh_reports_index(
    token: str,
    *,
    today: date | None = None,
    before_wb_call: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    today = today or date.today()
    df_from = today - timedelta(days=WINDOW_DAYS)
    rows = get_rows(df_from, today)
    if rows is None:
        if before_wb_call:
            await before_wb_call()
        rows = await fetch_realization_report(token, df_from, today)
        put_rows(df_from, today, rows)
    idx = _build_index(rows)
    _save_index(idx)
    return idx


def list_reports_sorted() -> list[ReportMeta]:
    idx = _load_index()
    items: list[ReportMeta] = []
    for rid, meta in idx.items():
        rows = meta.get("rows") or []
        items.append(
            ReportMeta(
                id=str(rid),
                date_from=meta.get("date_from") or "",
                date_to=meta.get("date_to") or "",
                rows_count=len(rows),
                payout=sum(_row_payout(r) for r in rows),
            )
        )
    items.sort(key=lambda x: x.date_to, reverse=True)
    return items


def get_report_rows(report_id: str) -> list[dict]:
    return _load_index().get(str(report_id), {}).get("rows", [])
