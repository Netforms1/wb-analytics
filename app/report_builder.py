from __future__ import annotations

from io import BytesIO

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from app.decoder import ReportTotals


def build_excel(df: pd.DataFrame, totals: ReportTotals) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary = pd.DataFrame(
            {
                "Показатель": [
                    "Сумма приходов",
                    "Сумма удержаний",
                    "Итого к выплате",
                ],
                "Значение, ₽": [totals.income, totals.expense, totals.payout],
            }
        )
        summary.to_excel(writer, sheet_name="Сводка", index=False)
        totals.by_money_column.to_excel(writer, sheet_name="Расшифровка статей", index=False)
        totals.by_operation.to_excel(writer, sheet_name="По операциям", index=False)
        totals.by_sku.to_excel(writer, sheet_name="По товарам", index=False)
        df.to_excel(writer, sheet_name="Исходные строки", index=False)
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
    data = data.head(top_n).iloc[::-1]
    label_col = "sa_name" if "sa_name" in data.columns else data.columns[0]
    labels = data[label_col].astype(str).fillna("—")

    fig, ax = plt.subplots(figsize=(9, max(4, 0.5 * len(data))))
    ax.barh(labels, data["payout"], color="#1f77b4")
    ax.set_title(f"Топ-{top_n} товаров по выплате, ₽")
    ax.set_xlabel("₽")
    fig.tight_layout()
    return _fig_to_png(fig)


def _fig_to_png(fig) -> bytes:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue()
