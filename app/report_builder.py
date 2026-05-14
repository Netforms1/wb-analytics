from __future__ import annotations

import re
from io import BytesIO

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from app.decoder import EXPENSE_COLS, INCOME_COLS, ReportTotals

# openpyxl запрещает в ячейках управляющие символы XML 1.0.
_ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Понятные русские заголовки для листа «Экономика по товарам».
SKU_FRIENDLY_COLS: dict[str, str] = {
    "vendor_code": "Артикул",
    "sa_name": "Артикул",
    "nm_id": "nm_id",
    "subject_name": "Категория",
    "brand_name": "Бренд",
    "units_sold": "Продано (нетто), шт",
    "ppvz_for_pay": "К перечислению, ₽",
    "additional_payment": "Доплаты, ₽",
    "delivery_rub": "Логистика, ₽",
    "penalty": "Штрафы, ₽",
    "storage_fee": "Хранение, ₽",
    "deduction": "Прочие удержания, ₽",
    "acceptance": "Платная приёмка, ₽",
    "rebill_logistic_cost": "Перевыставление логистики, ₽",
    "retail_amount": "Выручка по чекам, ₽",
    "payout": "К выплате, ₽",
    "cost": "Себестоимость / ед., ₽",
    "cogs": "Себестоимость общая, ₽",
    "profit": "Прибыль, ₽",
}


def _sanitize_for_xlsx(df: pd.DataFrame) -> pd.DataFrame:
    obj_cols = df.select_dtypes(include=["object"]).columns
    if obj_cols.empty:
        return df
    cleaned = df.copy()
    for col in obj_cols:
        cleaned[col] = cleaned[col].map(
            lambda v: _ILLEGAL_XLSX_CHARS.sub("", v) if isinstance(v, str) else v
        )
    return cleaned


def _per_sku_economics(by_sku: pd.DataFrame) -> pd.DataFrame:
    if by_sku.empty:
        return by_sku
    preferred_order = [
        "sa_name",
        "nm_id",
        "subject_name",
        "brand_name",
        "units_sold",
        "retail_amount",
        "ppvz_for_pay",
        "additional_payment",
        "delivery_rub",
        "penalty",
        "storage_fee",
        "deduction",
        "acceptance",
        "rebill_logistic_cost",
        "payout",
        "cost",
        "cogs",
        "profit",
    ]
    cols = [c for c in preferred_order if c in by_sku.columns]
    df = by_sku[cols].copy()
    df = df.rename(columns={k: v for k, v in SKU_FRIENDLY_COLS.items() if k in df.columns})
    return df


def _comparison_sheet(curr: ReportTotals, prev: ReportTotals | None) -> pd.DataFrame:
    metrics: list[tuple[str, float, bool]] = [
        ("Приходы", curr.income, True),
        ("Удержания", curr.expense, False),
        ("К выплате", curr.payout, True),
        ("Себестоимость", curr.cogs, False),
        ("Прибыль", curr.profit, True),
        ("Удержания по кредитам", curr.loans_deduction, False),
    ]
    rows = []
    for label, value, higher_is_better in metrics:
        prev_val = None
        if prev is not None:
            prev_val = {
                "Приходы": prev.income,
                "Удержания": prev.expense,
                "К выплате": prev.payout,
                "Себестоимость": prev.cogs,
                "Прибыль": prev.profit,
                "Удержания по кредитам": prev.loans_deduction,
            }[label]
        diff = value - prev_val if prev_val is not None else None
        pct = (diff / abs(prev_val) * 100) if (prev_val not in (None, 0)) else None
        direction = ""
        if diff is not None and abs(diff) > 0.005:
            good = (diff > 0) if higher_is_better else (diff < 0)
            direction = "🟢 лучше" if good else "🔴 хуже"
        rows.append(
            {
                "Показатель": label,
                "Текущий период, ₽": round(value, 2),
                "Прошлый период, ₽": round(prev_val, 2) if prev_val is not None else None,
                "Изменение, ₽": round(diff, 2) if diff is not None else None,
                "Изменение, %": round(pct, 1) if pct is not None else None,
                "Оценка": direction,
            }
        )
    return pd.DataFrame(rows)


def build_excel(
    df: pd.DataFrame,
    totals: ReportTotals,
    *,
    previous: ReportTotals | None = None,
) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary = pd.DataFrame(
            {
                "Показатель": [
                    "Сумма приходов",
                    "Сумма удержаний",
                    "В т.ч. удержания по кредитам",
                    "Итого к выплате",
                    "Себестоимость проданных товаров",
                    "Прибыль",
                ],
                "Значение, ₽": [
                    totals.income,
                    totals.expense,
                    totals.loans_deduction,
                    totals.payout,
                    totals.cogs,
                    totals.profit,
                ],
            }
        )
        summary.to_excel(writer, sheet_name="Сводка", index=False)
        _comparison_sheet(totals, previous).to_excel(
            writer, sheet_name="Сравнение", index=False
        )
        _sanitize_for_xlsx(totals.by_money_column).to_excel(
            writer, sheet_name="Расшифровка статей", index=False
        )
        _sanitize_for_xlsx(_per_sku_economics(totals.by_sku)).to_excel(
            writer, sheet_name="Экономика по товарам", index=False
        )
        _sanitize_for_xlsx(totals.by_operation).to_excel(
            writer, sheet_name="По операциям", index=False
        )
        _sanitize_for_xlsx(df).to_excel(writer, sheet_name="Исходные строки", index=False)
    return buf.getvalue()


def build_money_breakdown_chart(totals: ReportTotals) -> bytes:
    data = totals.by_money_column.copy()
    if data.empty:
        return b""
    data = data.assign(signed=lambda d: d.apply(
        lambda r: r["amount"] if r["kind"] == "приход" else -r["amount"], axis=1
    ))
    data = data.sort_values("signed")

    fig, ax = plt.subplots(figsize=(9, max(4, 0.5 * len(data))))
    colors = ["#2ca02c" if k == "приход" else "#d62728" for k in data["kind"]]
    ax.barh(data["label"], data["signed"], color=colors)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_title("Расшифровка выплаты по статьям, ₽")
    ax.set_xlabel("₽")
    fig.tight_layout()
    return _fig_to_png(fig)


def build_top_sku_chart(totals: ReportTotals, top_n: int = 10) -> bytes:
    data = totals.by_sku.copy()
    if data.empty:
        return b""
    metric = "profit" if totals.cogs > 0 and "profit" in data.columns else "payout"
    metric_label = "прибыли" if metric == "profit" else "выплате"
    data = data.head(top_n).iloc[::-1]
    label_col = "sa_name" if "sa_name" in data.columns else data.columns[0]
    labels = data[label_col].astype(str).fillna("—")

    fig, ax = plt.subplots(figsize=(9, max(4, 0.5 * len(data))))
    ax.barh(labels, data[metric], color="#1f77b4")
    ax.set_title(f"Топ-{top_n} товаров по {metric_label}, ₽")
    ax.set_xlabel("₽")
    fig.tight_layout()
    return _fig_to_png(fig)


def _fig_to_png(fig) -> bytes:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue()
