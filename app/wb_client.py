from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import httpx

WB_STATISTICS_BASE = "https://statistics-api.wildberries.ru"
REPORT_DETAIL_PATH = "/api/v5/supplier/reportDetailByPeriod"

PAGE_LIMIT = 100_000
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class WBApiError(RuntimeError):
    pass


def _format_date(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.date().isoformat()
    return d.isoformat()


async def fetch_realization_report(
    token: str,
    date_from: date | datetime,
    date_to: date | datetime,
    *,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Fetch the full реализация report for the period, following rrdid pagination."""
    headers = {"Authorization": token}
    params_base = {
        "dateFrom": _format_date(date_from),
        "dateTo": _format_date(date_to),
        "limit": PAGE_LIMIT,
    }

    rows: list[dict[str, Any]] = []
    rrdid = 0

    async with httpx.AsyncClient(base_url=WB_STATISTICS_BASE, timeout=timeout) as client:
        while True:
            params = {**params_base, "rrdid": rrdid}
            page = await _get_with_retries(client, REPORT_DETAIL_PATH, headers=headers, params=params)
            if not page:
                break
            rows.extend(page)
            last_rrd = page[-1].get("rrd_id")
            if last_rrd is None or last_rrd == rrdid or len(page) < PAGE_LIMIT:
                break
            rrdid = last_rrd

    return rows


async def _get_with_retries(
    client: httpx.AsyncClient,
    path: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any],
    max_attempts: int = 5,
) -> list[dict[str, Any]]:
    backoff = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.get(path, headers=headers, params=params)
        except httpx.HTTPError as exc:
            if attempt == max_attempts:
                raise WBApiError(f"network error: {exc}") from exc
            await asyncio.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
        if resp.status_code in RETRYABLE_STATUSES and attempt < max_attempts:
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        raise WBApiError(f"WB API {resp.status_code}: {resp.text[:300]}")

    return []
