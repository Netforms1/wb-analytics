"""WB Statistics: /orders and /sales — operational data with their own rate limits."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import httpx

WB_STATISTICS_BASE = "https://statistics-api.wildberries.ru"
ORDERS_PATH = "/api/v1/supplier/orders"
SALES_PATH = "/api/v1/supplier/sales"

PAGE_SOFT_LIMIT = 80_000  # WB max per page
RETRYABLE = {500, 502, 503, 504}


class OrdersApiError(RuntimeError):
    pass


def _rfc3339(d: date | datetime) -> str:
    if isinstance(d, datetime):
        dt = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    else:
        dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


async def _get(client: httpx.AsyncClient, path: str, headers, params, max_attempts=4) -> list[dict]:
    backoff = 1.5
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.get(path, headers=headers, params=params)
        except httpx.HTTPError as exc:
            if attempt == max_attempts:
                raise OrdersApiError(f"network error: {exc}") from exc
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
        if resp.status_code in RETRYABLE and attempt < max_attempts:
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        raise OrdersApiError(f"WB {path} {resp.status_code}: {resp.text[:300]}")
    return []


async def _fetch_paginated(token: str, path: str, date_from: date | datetime) -> list[dict]:
    headers = {"Authorization": token}
    cursor = _rfc3339(date_from)
    out: list[dict] = []
    seen_ids: set[str] = set()
    async with httpx.AsyncClient(base_url=WB_STATISTICS_BASE, timeout=120.0) as client:
        while True:
            page = await _get(client, path, headers, {"dateFrom": cursor, "flag": 0})
            if not page:
                break
            new_items = [
                p for p in page
                if (p.get("srid") or p.get("saleID") or p.get("gNumber") or "") not in seen_ids
            ]
            if not new_items:
                break
            for p in new_items:
                key = p.get("srid") or p.get("saleID") or p.get("gNumber") or ""
                if key:
                    seen_ids.add(key)
            out.extend(new_items)
            if len(page) < PAGE_SOFT_LIMIT:
                break
            cursor = max(p.get("lastChangeDate") or p.get("date") or cursor for p in page)
            await asyncio.sleep(0.5)
    return out


async def fetch_orders(token: str, date_from: date | datetime) -> list[dict]:
    return await _fetch_paginated(token, ORDERS_PATH, date_from)


async def fetch_sales(token: str, date_from: date | datetime) -> list[dict]:
    return await _fetch_paginated(token, SALES_PATH, date_from)


# ── summaries ────────────────────────────────────────────────────────────────
@dataclass
class OrdersSummary:
    total_orders: int
    cancelled: int
    total_amount: float
    cancelled_amount: float
    top_skus: list[tuple[str, int, float]]  # (sa_name, qty, amount)


@dataclass
class SalesSummary:
    sales_count: int
    returns_count: int
    sales_amount: float
    returns_amount: float
    buyout_rate_pct: float
    top_skus: list[tuple[str, int, float]]


def _amount(o: dict) -> float:
    val = o.get("priceWithDisc")
    if val is None:
        val = o.get("finishedPrice") or o.get("totalPrice") or 0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _sa(o: dict) -> str:
    return o.get("supplierArticle") or str(o.get("nmId") or "—")


def _top_skus(items: list[dict], *, limit: int = 10) -> list[tuple[str, int, float]]:
    agg: dict[str, dict[str, float]] = {}
    for it in items:
        key = _sa(it)
        bucket = agg.setdefault(key, {"qty": 0, "amount": 0.0})
        bucket["qty"] += 1
        bucket["amount"] += _amount(it)
    rows = [(k, int(v["qty"]), float(v["amount"])) for k, v in agg.items()]
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:limit]


def summarize_orders(orders: list[dict]) -> OrdersSummary:
    cancelled = [o for o in orders if o.get("isCancel")]
    return OrdersSummary(
        total_orders=len(orders),
        cancelled=len(cancelled),
        total_amount=sum(_amount(o) for o in orders),
        cancelled_amount=sum(_amount(o) for o in cancelled),
        top_skus=_top_skus(orders),
    )


def summarize_sales(items: list[dict]) -> SalesSummary:
    sales = [s for s in items if str(s.get("saleID", "")).startswith("S")]
    returns = [s for s in items if str(s.get("saleID", "")).startswith("R")]
    sales_amt = sum(_amount(s) for s in sales)
    returns_amt = sum(abs(_amount(s)) for s in returns)
    total = len(sales) + len(returns)
    rate = (len(sales) / total * 100.0) if total else 0.0
    return SalesSummary(
        sales_count=len(sales),
        returns_count=len(returns),
        sales_amount=sales_amt,
        returns_amount=returns_amt,
        buyout_rate_pct=rate,
        top_skus=_top_skus(sales),
    )


def format_orders_text(period_from: date, period_to: date, s: OrdersSummary) -> str:
    lines = [
        "<b>Заказы WB</b>",
        f"Период: {period_from.isoformat()} — {period_to.isoformat()}",
        "",
        f"📦 Заказов: <b>{s.total_orders}</b>",
        f"❌ Отмен: <b>{s.cancelled}</b> ({_pct(s.cancelled, s.total_orders):.1f}%)",
        f"💰 Сумма заказов: <b>{s.total_amount:,.2f} ₽</b>",
        f"   из них отменённых: {s.cancelled_amount:,.2f} ₽",
    ]
    if s.top_skus:
        lines.append("")
        lines.append("<b>Топ-10 артикулов:</b>")
        for sa, qty, amt in s.top_skus:
            lines.append(f"  • {sa}: {qty} шт / {amt:,.2f} ₽")
    return "\n".join(lines).replace(",", " ")


def format_sales_text(period_from: date, period_to: date, s: SalesSummary) -> str:
    lines = [
        "<b>Выкупы и возвраты WB</b>",
        f"Период: {period_from.isoformat()} — {period_to.isoformat()}",
        "",
        f"✅ Выкупов: <b>{s.sales_count}</b> на <b>{s.sales_amount:,.2f} ₽</b>",
        f"↩️ Возвратов: <b>{s.returns_count}</b> на <b>{s.returns_amount:,.2f} ₽</b>",
        f"📊 % выкупа: <b>{s.buyout_rate_pct:.1f}%</b>",
    ]
    if s.top_skus:
        lines.append("")
        lines.append("<b>Топ-10 артикулов по выкупам:</b>")
        for sa, qty, amt in s.top_skus:
            lines.append(f"  • {sa}: {qty} шт / {amt:,.2f} ₽")
    return "\n".join(lines).replace(",", " ")


def _pct(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole else 0.0
