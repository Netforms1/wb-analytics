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
    cogs: float
    profit: float
    loans_deduction: float
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


def _net_units_sold(df: pd.DataFrame) -> pd.Series:
    """Net units = sales - returns, indexed by nm_id."""
    if "supplier_oper_name" not in df.columns or "nm_id" not in df.columns:
        return pd.Series(dtype=float)
    sign = df["supplier_oper_name"].map(
        lambda x: 1 if x == "Продажа" else (-1 if x == "Возврат" else 0)
    )
    signed = df["quantity"] * sign
    return signed.groupby(df["nm_id"]).sum()


def _is_loan_row(row: pd.Series) -> bool:
    """WB удерживает по кредитам отдельной операцией; ловим по словам в названиях."""
    for col in ("supplier_oper_name", "bonus_type_name", "doc_type_name"):
        val = row.get(col)
        if isinstance(val, str) and "кредит" in val.lower():
            return True
    return False


def _split_loans(df: pd.DataFrame) -> tuple[float, float]:
    """Returns (loans_deduction, generic_deduction). loans is part of total deduction."""
    if df.empty or "deduction" not in df.columns:
        return 0.0, 0.0
    loan_mask = df.apply(_is_loan_row, axis=1)
    loan_sum = float(df.loc[loan_mask, "deduction"].sum())
    other_sum = float(df.loc[~loan_mask, "deduction"].sum())
    return loan_sum, other_sum


def summarize(df: pd.DataFrame, costs: dict[int, float] | None = None) -> ReportTotals:
    costs = costs or {}
    if df.empty:
        empty = pd.DataFrame()
        return ReportTotals(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, empty, empty, empty)

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

    net_units = _net_units_sold(df)
    if "nm_id" in by_sku.columns:
        by_sku["units_sold"] = by_sku["nm_id"].map(net_units).fillna(0).astype(int)
        by_sku["cost"] = by_sku["nm_id"].map(lambda nm: costs.get(int(nm)) if pd.notna(nm) else None)
        by_sku["cogs"] = (by_sku["units_sold"] * by_sku["cost"].fillna(0)).astype(float)
        by_sku["profit"] = by_sku["payout"] - by_sku["cogs"]
    else:
        by_sku["units_sold"] = 0
        by_sku["cost"] = None
        by_sku["cogs"] = 0.0
        by_sku["profit"] = by_sku["payout"]

    by_sku = by_sku.sort_values("profit" if costs else "payout", ascending=False)

    cogs_total = float(by_sku["cogs"].sum()) if not by_sku.empty else 0.0
    profit_total = payout - cogs_total

    loans_sum, other_deduction = _split_loans(df)

    money_rows = []
    for col in INCOME_COLS + EXPENSE_COLS:
        if col in df.columns:
            amount = float(df[col].sum())
            # «Прочие удержания» делим на «по кредитам» и остальное.
            if col == "deduction":
                money_rows.append(
                    {
                        "column": "deduction_other",
                        "label": "Прочие удержания (без кредитов)",
                        "kind": "удержание",
                        "amount": other_deduction,
                    }
                )
                money_rows.append(
                    {
                        "column": "deduction_loan",
                        "label": "Удержания по кредитам",
                        "kind": "удержание",
                        "amount": loans_sum,
                    }
                )
            else:
                money_rows.append(
                    {
                        "column": col,
                        "label": COLUMN_LABELS_RU.get(col, col),
                        "kind": "приход" if col in INCOME_COLS else "удержание",
                        "amount": amount,
                    }
                )
    if costs and cogs_total > 0:
        money_rows.append(
            {
                "column": "cogs",
                "label": "Себестоимость проданных товаров",
                "kind": "удержание",
                "amount": cogs_total,
            }
        )
    by_money_column = pd.DataFrame(money_rows).sort_values("amount", ascending=False)

    return ReportTotals(
        income=income,
        expense=expense,
        payout=payout,
        cogs=cogs_total,
        profit=profit_total,
        loans_deduction=loans_sum,
        by_operation=by_operation,
        by_sku=by_sku,
        by_money_column=by_money_column,
    )
