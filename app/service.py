from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.decoder import ReportTotals, summarize, to_dataframe
from app.report_builder import build_excel, build_money_breakdown_chart, build_top_sku_chart
from app.wb_client import fetch_realization_report


@dataclass
class ReportBundle:
    rows_count: int
    totals: ReportTotals
    excel: bytes
    chart_breakdown_png: bytes
    chart_top_sku_png: bytes


async def build_report(token: str, date_from: date, date_to: date) -> ReportBundle:
    rows = await fetch_realization_report(token, date_from, date_to)
    df = to_dataframe(rows)
    totals = summarize(df)
    return ReportBundle(
        rows_count=len(df),
        totals=totals,
        excel=build_excel(df, totals),
        chart_breakdown_png=build_money_breakdown_chart(totals),
        chart_top_sku_png=build_top_sku_chart(totals),
    )


def format_summary_text(period_from: date, period_to: date, bundle: ReportBundle) -> str:
    t = bundle.totals
    lines = [
        f"<b>Отчёт WB о реализации</b>",
        f"Период: {period_from.isoformat()} — {period_to.isoformat()}",
        f"Строк в отчёте: {bundle.rows_count}",
        "",
        f"💰 Приходы: <b>{t.income:,.2f} ₽</b>",
        f"➖ Удержания: <b>{t.expense:,.2f} ₽</b>",
        f"📦 К выплате: <b>{t.payout:,.2f} ₽</b>",
        "",
        "<b>Расшифровка по статьям:</b>",
    ]
    if not t.by_money_column.empty:
        for _, row in t.by_money_column.iterrows():
            sign = "+" if row["kind"] == "приход" else "−"
            lines.append(f"  {sign} {row['label']}: {row['amount']:,.2f} ₽")
    else:
        lines.append("  (пусто)")
    return "\n".join(lines).replace(",", " ")
