from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Awaitable, Callable

from app.cache import get_rows, put_rows
from app.costs import load_costs
from app.decoder import ReportTotals, summarize, to_dataframe
from app.report_builder import build_excel, build_money_breakdown_chart, build_top_sku_chart
from app.wb_client import fetch_realization_report

async def _noop_progress(_msg: str) -> None:
    return None


async def _noop_gate() -> None:
    return None


PROGRESS_NOOP: Callable[[str], Awaitable[None]] = _noop_progress
GATE_NOOP: Callable[[], Awaitable[None]] = _noop_gate


@dataclass
class ReportBundle:
    rows_count: int
    totals: ReportTotals
    previous: ReportTotals | None
    period: tuple[date, date]
    previous_period: tuple[date, date] | None
    excel: bytes
    chart_breakdown_png: bytes
    chart_top_sku_png: bytes
    has_costs: bool


async def _get_period_rows(
    token: str,
    date_from: date,
    date_to: date,
    *,
    on_progress: Callable[[str], Awaitable[None]],
    before_fetch: Callable[[], Awaitable[None]],
    fetch_label: str,
) -> list[dict]:
    cached = get_rows(date_from, date_to)
    if cached is not None:
        return cached
    await on_progress(fetch_label)
    await before_fetch()
    rows = await fetch_realization_report(token, date_from, date_to)
    put_rows(date_from, date_to, rows)
    return rows


async def build_report(
    token: str,
    date_from: date,
    date_to: date,
    *,
    on_progress: Callable[[str], Awaitable[None]] = PROGRESS_NOOP,
    before_wb_call: Callable[[], Awaitable[None]] = GATE_NOOP,
) -> ReportBundle:
    costs = load_costs()

    rows = await _get_period_rows(
        token, date_from, date_to,
        on_progress=on_progress,
        before_fetch=before_wb_call,
        fetch_label="Скачиваю текущий период из WB…",
    )
    df = to_dataframe(rows)
    totals = summarize(df, costs=costs)

    period_len = (date_to - date_from).days or 7
    prev_to = date_from
    prev_from = prev_to - timedelta(days=period_len)
    prev_totals: ReportTotals | None = None
    prev_period: tuple[date, date] | None = (prev_from, prev_to)
    try:
        prev_rows = await _get_period_rows(
            token, prev_from, prev_to,
            on_progress=on_progress,
            before_fetch=before_wb_call,
            fetch_label="Тяну прошлый период (учитываю лимит WB ~1 req/min)…",
        )
        prev_df = to_dataframe(prev_rows)
        prev_totals = summarize(prev_df, costs=costs)
    except Exception:  # noqa: BLE001
        prev_period = None

    return ReportBundle(
        rows_count=len(df),
        totals=totals,
        previous=prev_totals,
        period=(date_from, date_to),
        previous_period=prev_period,
        excel=build_excel(df, totals, previous=prev_totals),
        chart_breakdown_png=build_money_breakdown_chart(totals),
        chart_top_sku_png=build_top_sku_chart(totals),
        has_costs=bool(costs),
    )


def build_bundle_from_rows(
    rows: list[dict],
    *,
    period: tuple[date, date],
    previous_rows: list[dict] | None = None,
    previous_period: tuple[date, date] | None = None,
) -> ReportBundle:
    """Готовый бандл без запросов в WB — для уже скачанных данных (по report_id)."""
    costs = load_costs()
    df = to_dataframe(rows)
    totals = summarize(df, costs=costs)
    prev_totals = (
        summarize(to_dataframe(previous_rows), costs=costs) if previous_rows else None
    )
    return ReportBundle(
        rows_count=len(df),
        totals=totals,
        previous=prev_totals,
        period=period,
        previous_period=previous_period if prev_totals else None,
        excel=build_excel(df, totals, previous=prev_totals),
        chart_breakdown_png=build_money_breakdown_chart(totals),
        chart_top_sku_png=build_top_sku_chart(totals),
        has_costs=bool(costs),
    )


def _fmt_delta(curr: float, prev: float | None, *, higher_is_better: bool) -> str:
    if prev is None:
        return ""
    diff = curr - prev
    if abs(diff) < 0.005:
        return "  ⚪ без изменений"
    pct = (diff / abs(prev) * 100) if prev else 0.0
    is_good = (diff > 0) if higher_is_better else (diff < 0)
    emoji = "🟢" if is_good else "🔴"
    sign = "+" if diff > 0 else "−"
    pct_str = f" ({sign}{abs(pct):.1f}%)" if prev else ""
    return f"  {emoji} {sign}{abs(diff):,.2f} ₽{pct_str}".replace(",", " ")


def format_summary_text(period_from: date, period_to: date, bundle: ReportBundle) -> str:
    t = bundle.totals
    p = bundle.previous

    def d(curr: float, attr: str, *, higher_is_better: bool) -> str:
        return _fmt_delta(curr, getattr(p, attr) if p else None, higher_is_better=higher_is_better)

    lines = [
        "<b>Отчёт WB о реализации</b>",
        f"Период: {period_from.isoformat()} — {period_to.isoformat()}",
        f"Строк в отчёте: {bundle.rows_count}",
    ]
    if bundle.previous_period:
        pf, pt = bundle.previous_period
        lines.append(f"Сравнение с: {pf.isoformat()} — {pt.isoformat()}")
    lines.append("")
    lines.append(f"💰 Приходы: <b>{t.income:,.2f} ₽</b>{d(t.income, 'income', higher_is_better=True)}")
    lines.append(f"➖ Удержания: <b>{t.expense:,.2f} ₽</b>{d(t.expense, 'expense', higher_is_better=False)}")
    if t.loans_deduction or (p and p.loans_deduction):
        lines.append(
            f"💳 По кредитам: <b>{t.loans_deduction:,.2f} ₽</b>"
            f"{d(t.loans_deduction, 'loans_deduction', higher_is_better=False)}"
        )
    lines.append(f"📦 К выплате: <b>{t.payout:,.2f} ₽</b>{d(t.payout, 'payout', higher_is_better=True)}")
    if bundle.has_costs:
        lines.append(
            f"🏷 Себестоимость: <b>{t.cogs:,.2f} ₽</b>"
            f"{d(t.cogs, 'cogs', higher_is_better=False)}"
        )
        lines.append(
            f"📈 Прибыль: <b>{t.profit:,.2f} ₽</b>"
            f"{d(t.profit, 'profit', higher_is_better=True)}"
        )
    else:
        lines.append("")
        lines.append("ℹ️ Загрузите себестоимость через меню — будет считаться прибыль.")

    lines.append("")
    lines.append("<b>Расшифровка по статьям:</b>")
    if not t.by_money_column.empty:
        for _, row in t.by_money_column.iterrows():
            sign = "+" if row["kind"] == "приход" else "−"
            lines.append(f"  {sign} {row['label']}: {row['amount']:,.2f} ₽")
    else:
        lines.append("  (пусто)")
    return "\n".join(lines).replace(",", " ")
