from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

# Numeric columns that represent money flows in the реализация report.
# Positive — приход поставщику, negative — расход (комиссии/удержания).
INCOME_COLS = ["ppvz_for_pay", "additional_payment"]
EXPENSE_COLS = [
    "delivery_rub",
    "penalty",
    "storage_fee",
    "deduction",
    "acceptance",
    "rebill_logistic_cost",
]

# Human-readable labels for the breakdown.
COLUMN_LABELS_RU: dict[str, str] = {
    "ppvz_for_pay": "К перечислению за товар",
    "additional_payment": "Доплаты",
    "delivery_rub": "Логистика",
    "penalty": "Штрафы",
    "storage_fee": "Хранение",
    "deduction": "Прочие удержания",
    "acceptance": "Платная приёмка",
    "rebill_logistic_cost": "Перевыставление логистики",
}


@dataclass
class ReportTotals:
    income: float
    expense: float
    payout: float
    by_operation: pd.DataFrame
    by_sku: pd.DataFrame
    by_money_column: pd.DataFrame


def to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in INCOME_COLS + EXPENSE_COLS + ["quantity", "retail_amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


def summarize(df: pd.DataFrame) -> ReportTotals:
    if df.empty:
        empty = pd.DataFrame()
        return ReportTotals(0.0, 0.0, 0.0, empty, empty, empty)

    income = float(sum(df[c].sum() for c in INCOME_COLS if c in df.columns))
    expense = float(sum(df[c].sum() for c in EXPENSE_COLS if c in df.columns))
    payout = income - expense

    by_operation = (
        df.groupby("supplier_oper_name", dropna=False)
        .agg(
            quantity=("quantity", "sum"),
            retail_amount=("retail_amount", "sum"),
            ppvz_for_pay=("ppvz_for_pay", "sum"),
        )
        .reset_index()
        .sort_values("ppvz_for_pay", ascending=False)
    )

    sku_key_cols = [c for c in ("nm_id", "sa_name", "subject_name", "brand_name") if c in df.columns]
    agg_cols = {c: (c, "sum") for c in INCOME_COLS + EXPENSE_COLS if c in df.columns}
    agg_cols["quantity"] = ("quantity", "sum")
    agg_cols["retail_amount"] = ("retail_amount", "sum")
    by_sku = df.groupby(sku_key_cols, dropna=False).agg(**agg_cols).reset_index()
    by_sku["payout"] = sum(by_sku[c] for c in INCOME_COLS if c in by_sku.columns) - sum(
        by_sku[c] for c in EXPENSE_COLS if c in by_sku.columns
    )
    by_sku = by_sku.sort_values("payout", ascending=False)

    money_rows = []
    for col in INCOME_COLS + EXPENSE_COLS:
        if col in df.columns:
            money_rows.append(
                {
                    "column": col,
                    "label": COLUMN_LABELS_RU.get(col, col),
                    "kind": "приход" if col in INCOME_COLS else "удержание",
                    "amount": float(df[col].sum()),
                }
            )
    by_money_column = pd.DataFrame(money_rows).sort_values("amount", ascending=False)

    return ReportTotals(
        income=income,
        expense=expense,
        payout=payout,
        by_operation=by_operation,
        by_sku=by_sku,
        by_money_column=by_money_column,
    )
