from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from app.config import settings
from app.service import build_report
from app.wb_client import WBApiError

app = FastAPI(title="WB Realization Report Decoder", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/report/summary")
async def report_summary(
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
) -> dict:
    bundle = await _build(date_from, date_to)
    t = bundle.totals
    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "rows": bundle.rows_count,
        "income": t.income,
        "expense": t.expense,
        "payout": t.payout,
        "breakdown": t.by_money_column.to_dict(orient="records"),
        "by_operation": t.by_operation.to_dict(orient="records"),
        "by_sku": t.by_sku.to_dict(orient="records"),
    }


@app.get("/report/excel")
async def report_excel(
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
) -> Response:
    bundle = await _build(date_from, date_to)
    filename = f"wb_report_{date_from.isoformat()}_{date_to.isoformat()}.xlsx"
    return Response(
        content=bundle.excel,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _build(date_from: date, date_to: date):
    try:
        return await build_report(settings.wb_api_token, date_from, date_to)
    except WBApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
