"""Parse WB реализационный отчёт downloaded as XLSX from the seller portal."""
from __future__ import annotations

import re
from io import BytesIO

import pandas as pd

# ── PRIMARY: точный маппинг колонок текущего экспорта WB (lower-cased) ───────
EXACT_HEADER_MAP: dict[str, str] = {
    "№": "rrd_id",
    "код номенклатуры": "nm_id",
    "артикул поставщика": "sa_name",
    "предмет": "subject_name",
    "бренд": "brand_name",
    "название": "product_name",
    "тип документа": "doc_type_name",
    "обоснование для оплаты": "supplier_oper_name",
    "виды логистики, штрафов и корректировок вв": "bonus_type_name",
    "кол-во": "quantity",
    "цена розничная": "retail_price_unit",
    "вайлдберриз реализовал товар (пр)": "retail_amount",
    "к перечислению продавцу за реализованный товар": "ppvz_for_pay",
    "услуги по доставке товара покупателю": "delivery_rub",
    "общая сумма штрафов": "penalty",
    "корректировка вознаграждения вайлдберриз (вв)": "additional_payment",
    "хранение": "storage_fee",
    "удержания": "deduction",
    "операции на приемке": "acceptance",
    "возмещение издержек по перевозке/по складским операциям с товаром": "rebill_logistic_cost",
    "дата заказа покупателем": "order_dt",
    "дата продажи": "sale_dt",
    "srid": "srid",
    "номер отчёта": "realizationreport_id",
    "номер отчета": "realizationreport_id",
    "дата начала отчётного периода": "date_from",
    "дата начала отчетного периода": "date_from",
    "дата конца отчётного периода": "date_to",
    "дата конца отчетного периода": "date_to",
}

# ── FALLBACK: fuzzy substring rules (для старых/иных версий экспорта) ────────
FUZZY_RULES: list[tuple[str, list[str]]] = [
    ("realizationreport_id", ["номер отчёт"]),
    ("realizationreport_id", ["номер отчет"]),
    ("date_from", ["начало отчётного периода"]),
    ("date_from", ["начало отчетного периода"]),
    ("date_from", ["начало периода"]),
    ("date_to", ["конец отчётного периода"]),
    ("date_to", ["конец отчетного периода"]),
    ("date_to", ["конец периода"]),
    ("supplier_oper_name", ["обоснование"]),
    ("doc_type_name", ["тип документа"]),
    ("nm_id", ["код номенклатуры"]),
    ("nm_id", ["артикул wb"]),
    ("sa_name", ["артикул поставщика"]),
    ("subject_name", ["предмет"]),
    ("brand_name", ["бренд"]),
    ("bonus_type_name", ["виды логистики"]),
    ("quantity", ["кол-во"]),
    ("retail_amount", ["сумма продаж"]),
    ("ppvz_for_pay", ["к перечислению"]),
    ("delivery_rub", ["услуги по доставке"]),
    ("delivery_rub", ["стоимость логистики"]),
    ("penalty", ["штраф"]),
    ("storage_fee", ["хранени"]),
    ("acceptance", ["приёмк"]),
    ("acceptance", ["приемк"]),
    ("deduction", ["прочие удержания"]),
    ("additional_payment", ["доплат"]),
    ("rebill_logistic_cost", ["возмещение издержек"]),
    ("rebill_logistic_cost", ["перевыставлени"]),
    ("sale_dt", ["дата продажи"]),
    ("order_dt", ["дата заказа"]),
]

NUMERIC_COLS = {
    "quantity",
    "retail_amount",
    "retail_price_unit",
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

DATE_FIELDS = {"date_from", "date_to", "sale_dt", "order_dt"}

FNAME_REPORT_RE = re.compile(r"№?\s*(\d{6,12})_(\d{3,12})")


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


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _to_iso_date(v) -> str | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:  # noqa: BLE001
        pass
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # ISO «YYYY-MM-DD» — dayfirst здесь ломает порядок.
        dayfirst = not _ISO_DATE_RE.match(s)
        try:
            return pd.to_datetime(s, dayfirst=dayfirst).date().isoformat()
        except Exception:  # noqa: BLE001
            return s[:10]
    try:
        return pd.to_datetime(v).date().isoformat()
    except Exception:  # noqa: BLE001
        return None


def _detect_header_row(raw: pd.DataFrame, look_rows: int = 15) -> int:
    """WB иногда добавляет 1–2 заголовочные строки сверху. Ищем настоящий header."""
    for i in range(min(look_rows, len(raw))):
        row = raw.iloc[i]
        string_cells = sum(1 for x in row if isinstance(x, str) and len(str(x).strip()) > 2)
        if string_cells >= 5:
            return i
    raise ValueError("Не нашёл шапку таблицы в Excel.")


def _report_id_from_filename(filename: str | None) -> int | None:
    if not filename:
        return None
    m = FNAME_REPORT_RE.search(filename)
    if m:
        return int(m.group(1))
    return None


def _map_columns(headers: dict[str, str]) -> dict[str, str]:
    """headers: {col_name: normalized}. Returns {api_field: col_name}."""
    field_to_col: dict[str, str] = {}
    # 1) Exact match
    for col, norm in headers.items():
        api = EXACT_HEADER_MAP.get(norm)
        if api and api not in field_to_col:
            field_to_col[api] = col
    # 2) Fuzzy fallback
    for api, keywords in FUZZY_RULES:
        if api in field_to_col:
            continue
        for col, norm in headers.items():
            if all(kw in norm for kw in keywords):
                field_to_col[api] = col
                break
    return field_to_col


def parse_wb_excel(content: bytes, filename: str | None = None) -> list[dict]:
    bio = BytesIO(content)
    raw = pd.read_excel(bio, engine="openpyxl", header=None)
    header_row = _detect_header_row(raw)

    bio.seek(0)
    df = pd.read_excel(bio, engine="openpyxl", header=header_row)
    headers_norm = {col: _normalize(col) for col in df.columns}

    field_to_col = _map_columns(headers_norm)

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
            if api_name in DATE_FIELDS:
                iso = _to_iso_date(val)
                row_dict[api_name] = iso
                if iso:
                    any_value = True
            elif api_name in NUMERIC_COLS:
                num = _to_num(val)
                row_dict[api_name] = num
                if num != 0:
                    any_value = True
            else:
                if val is None:
                    row_dict[api_name] = None
                else:
                    s = str(val)
                    row_dict[api_name] = s
                    if s.strip():
                        any_value = True
        if any_value:
            out.append(row_dict)

    # ── Заполняем недостающие метаданные ────────────────────────────────
    # 1) realizationreport_id из имени файла
    if not any(r.get("realizationreport_id") for r in out):
        rid = _report_id_from_filename(filename)
        if rid is not None:
            for r in out:
                r["realizationreport_id"] = rid

    # 2) Период (date_from/date_to) — если в файле нет, считаем по «Дата продажи»
    # только для рядов продаж/возвратов: логистические корректировки могут тянуться
    # из старых заказов, они период недели не описывают.
    has_period = any(r.get("date_from") and r.get("date_to") for r in out)
    if not has_period:
        sale_dates = sorted(
            {
                r["sale_dt"]
                for r in out
                if r.get("sale_dt")
                and r.get("supplier_oper_name") in ("Продажа", "Возврат")
            }
        )
        if not sale_dates:
            sale_dates = sorted({r["sale_dt"] for r in out if r.get("sale_dt")})
        if sale_dates:
            df_from, df_to = sale_dates[0], sale_dates[-1]
            for r in out:
                r.setdefault("date_from", df_from)
                r.setdefault("date_to", df_to)

    return out
