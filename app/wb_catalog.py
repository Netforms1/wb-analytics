"""Fetch catalog (cards) and current prices from WB API."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

CONTENT_BASE = "https://content-api.wildberries.ru"
PRICES_BASE = "https://discounts-prices-api.wildberries.ru"

CARDS_LIST_PATH = "/content/v2/get/cards/list"
GOODS_FILTER_PATH = "/api/v2/list/goods/filter"

CARDS_PAGE_LIMIT = 100
GOODS_PAGE_LIMIT = 1000


class CatalogApiError(RuntimeError):
    pass


async def fetch_cards(token: str, *, timeout: float = 60.0) -> list[dict[str, Any]]:
    """Iterate cursor pagination of cards/list. Returns minimal fields per card."""
    headers = {"Authorization": token, "Content-Type": "application/json"}
    cards: list[dict[str, Any]] = []
    cursor: dict[str, Any] = {"limit": CARDS_PAGE_LIMIT}

    async with httpx.AsyncClient(base_url=CONTENT_BASE, timeout=timeout) as client:
        while True:
            body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
            resp = await client.post(CARDS_LIST_PATH, headers=headers, json=body)
            if resp.status_code != 200:
                raise CatalogApiError(f"cards/list {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            batch = data.get("cards") or []
            cards.extend(batch)
            cursor_info = data.get("cursor") or {}
            total = cursor_info.get("total", 0)
            if total < CARDS_PAGE_LIMIT or not batch:
                break
            last = batch[-1]
            cursor = {
                "limit": CARDS_PAGE_LIMIT,
                "updatedAt": cursor_info.get("updatedAt") or last.get("updatedAt"),
                "nmID": cursor_info.get("nmID") or last.get("nmID"),
            }
            await asyncio.sleep(0.3)  # be gentle

    return cards


async def fetch_prices(token: str, *, timeout: float = 60.0) -> dict[int, float]:
    """Returns nm_id -> minimum discounted price across sizes (or list price)."""
    headers = {"Authorization": token}
    prices: dict[int, float] = {}
    offset = 0
    async with httpx.AsyncClient(base_url=PRICES_BASE, timeout=timeout) as client:
        while True:
            params = {"limit": GOODS_PAGE_LIMIT, "offset": offset}
            resp = await client.get(GOODS_FILTER_PATH, headers=headers, params=params)
            if resp.status_code != 200:
                raise CatalogApiError(f"goods/filter {resp.status_code}: {resp.text[:300]}")
            payload = resp.json().get("data") or {}
            items = payload.get("listGoods") or []
            if not items:
                break
            for it in items:
                nm = it.get("nmID")
                if nm is None:
                    continue
                sizes = it.get("sizes") or []
                size_prices = [
                    s.get("discountedPrice") or s.get("price")
                    for s in sizes
                    if (s.get("discountedPrice") or s.get("price"))
                ]
                if size_prices:
                    prices[int(nm)] = float(min(size_prices))
            if len(items) < GOODS_PAGE_LIMIT:
                break
            offset += GOODS_PAGE_LIMIT
            await asyncio.sleep(0.3)
    return prices


async def fetch_catalog(token: str) -> list[dict[str, Any]]:
    """Merge cards + prices into a flat list: nm_id, vendor_code, title, price."""
    cards = await fetch_cards(token)
    try:
        prices = await fetch_prices(token)
    except CatalogApiError:
        prices = {}

    result: list[dict[str, Any]] = []
    for c in cards:
        nm = c.get("nmID")
        if nm is None:
            continue
        result.append(
            {
                "nm_id": int(nm),
                "vendor_code": c.get("vendorCode") or "",
                "title": c.get("title") or "",
                "subject": (c.get("subjectName") or c.get("subject") or ""),
                "brand": c.get("brand") or "",
                "price": prices.get(int(nm)),
            }
        )
    result.sort(key=lambda r: (r["vendor_code"], r["nm_id"]))
    return result
