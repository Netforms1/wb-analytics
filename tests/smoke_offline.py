"""Offline smoke test: feeds a fixture into decoder + report builder, no network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.decoder import summarize, to_dataframe
from app.report_builder import build_excel, build_money_breakdown_chart, build_top_sku_chart

OUT_DIR = Path("/tmp/wb-smoke")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = json.loads((ROOT / "tests/fixtures/realization_sample.json").read_text())
    df = to_dataframe(rows)
    totals = summarize(df)

    print(f"rows: {len(df)}")
    print(f"income:  {totals.income:>12.2f} ₽")
    print(f"expense: {totals.expense:>12.2f} ₽")
    print(f"payout:  {totals.payout:>12.2f} ₽")
    print()
    print("by money column:")
    print(totals.by_money_column.to_string(index=False))
    print()
    print("by operation:")
    print(totals.by_operation.to_string(index=False))
    print()
    print("by SKU:")
    print(totals.by_sku.to_string(index=False))

    xlsx = OUT_DIR / "report.xlsx"
    xlsx.write_bytes(build_excel(df, totals))
    money_png = OUT_DIR / "money_breakdown.png"
    money_png.write_bytes(build_money_breakdown_chart(totals))
    sku_png = OUT_DIR / "top_sku.png"
    sku_png.write_bytes(build_top_sku_chart(totals))

    print()
    for p in (xlsx, money_png, sku_png):
        print(f"wrote {p} ({p.stat().st_size} bytes)")

    expected_payout = 3300 - 1100 + 5600 + 250 - (180 + 60 + 240 + 320 + 500 + 870)
    assert abs(totals.payout - expected_payout) < 1e-6, (totals.payout, expected_payout)
    print(f"\nOK: payout matches expected {expected_payout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
