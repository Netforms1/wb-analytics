"""Parse WB реализационный отчёт downloaded as XLSX from the seller portal."""
from __future__ import annotations

import re
from io import BytesIO

import pandas as pd

# Правила распознавания шапок (lower-cased substrings — ВСЕ должны быть в названии колонки).
# Порядок важен: сначала более конкретные сопоставления.
COLUMN_RULES: list[tuple[str, list[str]]] = [
    ("realizationreport_id", ["номер отчёт"]),
    ("realizationreport_id", ["номер отчет"]),
    ("date_from", ["дата начала", "период"]),
    ("date_from", ["начало отчётного периода"]),
    ("date_from", ["начало отчетного периода"]),
    ("date_from", ["начало периода"]),
    ("date_from", ["дата с"]),
    ("date_to", ["дата конца", "период"]),
    ("date_to", ["конец отчётного периода"]),
    ("date_to", ["конец отчетного периода"]),
    ("date_to", ["конец периода"]),
    ("date_to", ["дата по"]),
    ("supplier_oper_name", ["обоснование"]),
    ("bonus_type_name", ["виды логистики"]),
    ("doc_type_name", ["тип документа"]),
    ("nm_id", ["код номенклатуры"]),
    ("nm_id", ["артикул wb"]),
    ("sa_name", ["артикул поставщика"]),
    ("subject_name", ["предмет"]),
    ("brand_name", ["бренд"]),
    ("quantity", ["кол-во"]),
    ("retail_amount", ["сумма продаж"]),
    ("retail_amount", ["сумма (возвратов)"]),
    ("ppvz_for_pay", ["к перечислению продавцу"]),
    ("ppvz_for_pay", ["к перечислению"]),
    ("delivery_rub", ["услуги по доставке"]),
    ("delivery_rub", ["стоимость логистики"]),
    ("penalty", ["сумма штрафов"]),
    ("penalty", ["штраф"]),
    ("storage_fee", ["хранени"]),
    ("acceptance", ["приёмк"]),
    ("acceptance", ["приемк"]),
    ("deduction", ["прочие удержания"]),
    ("additional_payment", ["доплат"]),
    ("rebill_logistic_cost", ["перевыставлени"]),
    ("rrd_id", ["rrd_id"]),
    ("rrd_id", ["№ строки"]),
]

NUMERIC_COLS = {
    "quantity",
    "retail_amount",
    "ppvz_for_pay",
    "delivery_rub",
    "penalty",
    "storage_fee",
    "deduction",
    "acceptance",
    "rebill_logistic_cost",
    "additional_payment",
    "nm_id",
    "rrd_id",
    "realizationreport_id",
}


def _normalize(s) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _to_num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        try:
            if pd.isna(v):
                return 0.0
        except Exception:  # noqa: BLE001
            pass
        return float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _detect_header_row(raw: pd.DataFrame, look_rows: int = 15) -> int:
    """WB иногда добавляет 1–2 заголовочные строки сверху. Ищем настоящий header."""
    for i in range(min(look_rows, len(raw))):
        row = raw.iloc[i]
        string_cells = sum(1 for x in row if isinstance(x, str) and len(str(x).strip()) > 2)
        if string_cells >= 5:
            return i
    raise ValueError("Не нашёл шапку таблицы в Excel.")


FNAME_REPORT_RE = re.compile(r"№?\s*(\d{6,12})_(\d{3,12})")


def _report_id_from_filename(filename: str | None) -> int | None:
    if not filename:
        return None
    m = FNAME_REPORT_RE.search(filename)
    if m:
        return int(m.group(1))
    return None


def parse_wb_excel(content: bytes, filename: str | None = None) -> list[dict]:
    bio = BytesIO(content)
    raw = pd.read_excel(bio, engine="openpyxl", header=None)
    header_row = _detect_header_row(raw)

    bio.seek(0)
    df = pd.read_excel(bio, engine="openpyxl", header=header_row)
    headers_norm = {col: _normalize(col) for col in df.columns}

    field_to_col: dict[str, str] = {}
    for api_name, keywords in COLUMN_RULES:
        if api_name in field_to_col:
            continue
        for col, norm in headers_norm.items():
            if all(kw in norm for kw in keywords):
                field_to_col[api_name] = col
                break

    if "ppvz_for_pay" not in field_to_col and "retail_amount" not in field_to_col:
        raise ValueError(
            "Не нашёл финансовых колонок (К перечислению, Сумма продаж). "
            "Похоже, это не реализационный отчёт WB."
        )

    out: list[dict] = []
    for _, r in df.iterrows():
        row_dict: dict = {}
        any_value = False
        for api_name, src_col in field_to_col.items():
            val = r[src_col]
            try:
                if pd.isna(val):
                    val = None
            except Exception:  # noqa: BLE001
                pass
            if api_name in NUMERIC_COLS:
                num = _to_num(val)
                row_dict[api_name] = num
                if num != 0:
                    any_value = True
            else:
                if val is None:
                    row_dict[api_name] = None
                else:
                    row_dict[api_name] = str(val)
                    if str(val).strip():
                        any_value = True
        if any_value:
            # даты приводим к ISO "YYYY-MM-DD"
            for k in ("date_from", "date_to"):
                v = row_dict.get(k)
                if isinstance(v, str) and len(v) >= 10:
                    try:
                        row_dict[k] = pd.to_datetime(v, dayfirst=True).date().isoformat()
                    except Exception:  # noqa: BLE001
                        row_dict[k] = v[:10]
            out.append(row_dict)

    # Если внутри файла не оказалось номера отчёта — вытащим его из имени файла
    # вида "Еженедельный детализированный отчет №713836250_1344375 - 1.xlsx".
    has_id = any(r.get("realizationreport_id") for r in out)
    if not has_id:
        rid = _report_id_from_filename(filename)
        if rid is not None:
            for r in out:
                r["realizationreport_id"] = rid
    return out
