from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from io import BytesIO

import pandas as pd
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.config import settings
from app.costs import load_costs, update_costs
from app.service import build_report, format_summary_text
from app.wb_catalog import CatalogApiError, fetch_catalog
from app.wb_client import WBApiError

logger = logging.getLogger(__name__)

# WB лимитит reportDetailByPeriod ~1 req/min на токен и продлевает кулдаун при нарушениях.
WB_MIN_INTERVAL_SEC = 70.0
_last_wb_call_at: float = 0.0
_wb_lock: asyncio.Lock | None = None


def _get_wb_lock() -> asyncio.Lock:
    global _wb_lock
    if _wb_lock is None:
        _wb_lock = asyncio.Lock()
    return _wb_lock


# ── UI: bottom reply keyboard ────────────────────────────────────────────────
BTN_WEEK = "📊 За неделю"
BTN_PERIOD = "📅 За период"
BTN_CATALOG = "🛒 Каталог + себестоимость"
BTN_COSTS = "💰 Мои себестоимости"
BTN_HELP = "ℹ️ Справка"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_PERIOD)],
        [KeyboardButton(text=BTN_CATALOG), KeyboardButton(text=BTN_COSTS)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

HELP = (
    "Бот расшифровывает финансовый отчёт WB «реализация» "
    "и считает прибыль с учётом вашей себестоимости.\n\n"
    "Меню снизу:\n"
    "📊 За неделю — отчёт за последние 7 дней\n"
    "📅 За период — указать даты вручную\n"
    "🛒 Каталог + себестоимость — выгрузить Excel-шаблон с товарами; "
    "заполните колонку «Себестоимость» и отправьте файл обратно\n"
    "💰 Мои себестоимости — показать текущий список\n\n"
    "Лимит WB: 1 запрос в минуту."
)


class ReportStates(StatesGroup):
    waiting_period = State()


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_dates(text: str | None) -> tuple[date, date] | None:
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        return datetime.fromisoformat(parts[0]).date(), datetime.fromisoformat(parts[1]).date()
    except ValueError:
        return None


def _is_allowed(message: Message) -> bool:
    allowed = settings.allowed_user_ids
    if not allowed:
        return True
    return message.from_user is not None and message.from_user.id in allowed


def _catalog_to_excel(catalog: list[dict], existing_costs: dict[int, float]) -> bytes:
    df = pd.DataFrame(catalog)
    if df.empty:
        df = pd.DataFrame(columns=["nm_id", "vendor_code", "title", "subject", "brand", "price"])
    df["Себестоимость"] = df["nm_id"].map(existing_costs).astype("object")
    df = df.rename(
        columns={
            "nm_id": "nm_id",
            "vendor_code": "Артикул",
            "title": "Название",
            "subject": "Категория",
            "brand": "Бренд",
            "price": "Цена WB, ₽",
        }
    )
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Каталог", index=False)
    return buf.getvalue()


def _parse_costs_excel(content: bytes) -> dict[int, float]:
    """Read Excel/CSV and return {nm_id: cost}. Skips rows with empty cost."""
    bio = BytesIO(content)
    try:
        df = pd.read_excel(bio, engine="openpyxl")
    except Exception:
        bio.seek(0)
        df = pd.read_csv(bio)

    nm_col = next(
        (c for c in df.columns if str(c).strip().lower() in {"nm_id", "nmid", "nm id"}),
        None,
    )
    cost_col = next(
        (c for c in df.columns if "себестоим" in str(c).lower() or str(c).strip().lower() == "cost"),
        None,
    )
    if nm_col is None or cost_col is None:
        raise ValueError(
            "Не нашёл колонок nm_id и «Себестоимость» в файле. "
            "Используйте шаблон из «🛒 Каталог + себестоимость»."
        )

    result: dict[int, float] = {}
    for _, row in df.iterrows():
        try:
            nm = int(row[nm_col])
        except (TypeError, ValueError):
            continue
        raw = row[cost_col]
        if pd.isna(raw):
            continue
        try:
            cost = float(str(raw).replace(",", ".").replace(" ", ""))
        except (TypeError, ValueError):
            continue
        if cost < 0:
            continue
        result[nm] = cost
    return result


# ── dispatcher ───────────────────────────────────────────────────────────────
def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    @dp.message(Command("help"))
    @dp.message(F.text == BTN_HELP)
    async def on_help(message: Message, state: FSMContext) -> None:
        if not _is_allowed(message):
            return
        await state.clear()
        await message.answer(HELP, reply_markup=MAIN_KB)

    @dp.message(F.text == BTN_WEEK)
    @dp.message(Command("last_week"))
    async def on_week(message: Message, state: FSMContext) -> None:
        if not _is_allowed(message):
            return
        await state.clear()
        date_to = date.today()
        date_from = date_to - timedelta(days=7)
        await _send_report(message, date_from, date_to)

    @dp.message(F.text == BTN_PERIOD)
    async def on_period_prompt(message: Message, state: FSMContext) -> None:
        if not _is_allowed(message):
            return
        await state.set_state(ReportStates.waiting_period)
        await message.answer(
            "Пришлите две даты через пробел в формате ГГГГ-ММ-ДД ГГГГ-ММ-ДД.\n"
            "Например: <code>2025-04-28 2025-05-04</code>"
        )

    @dp.message(ReportStates.waiting_period, F.text)
    async def on_period_received(message: Message, state: FSMContext) -> None:
        if not _is_allowed(message):
            return
        parsed = _parse_dates(message.text)
        if not parsed:
            await message.answer("Не понял даты. Пример: 2025-04-28 2025-05-04")
            return
        await state.clear()
        await _send_report(message, *parsed)

    @dp.message(Command("report"))
    async def on_report_command(
        message: Message, command: Command.CommandObject = None
    ) -> None:
        if not _is_allowed(message):
            return
        parsed = _parse_dates(command.args if command else None)
        if not parsed:
            await message.answer("Использование: /report 2025-01-01 2025-01-07")
            return
        await _send_report(message, *parsed)

    @dp.message(F.text == BTN_CATALOG)
    @dp.message(Command("catalog"))
    async def on_catalog(message: Message) -> None:
        if not _is_allowed(message):
            return
        status = await message.answer("Скачиваю каталог из WB…")
        try:
            catalog = await fetch_catalog(settings.wb_api_token)
        except CatalogApiError as exc:
            await status.edit_text(f"Ошибка WB API: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("catalog failed")
            await status.edit_text(f"Внутренняя ошибка: {exc}")
            return

        if not catalog:
            await status.edit_text("WB вернул пустой каталог.")
            return

        xlsx = _catalog_to_excel(catalog, load_costs())
        await status.edit_text(f"Карточек: {len(catalog)}. Шлю шаблон.")
        await message.answer_document(
            BufferedInputFile(xlsx, filename="catalog_template.xlsx"),
            caption=(
                "Заполните колонку <b>Себестоимость</b> и отправьте файл обратно "
                "сюда же. Я обновлю мэппинг и буду считать прибыль в отчётах."
            ),
        )

    @dp.message(F.text == BTN_COSTS)
    @dp.message(Command("costs"))
    async def on_costs(message: Message) -> None:
        if not _is_allowed(message):
            return
        costs = load_costs()
        if not costs:
            await message.answer(
                "Себестоимости пока не загружены. Нажмите «🛒 Каталог + себестоимость»."
            )
            return
        df = pd.DataFrame(
            [{"nm_id": nm, "Себестоимость, ₽": v} for nm, v in sorted(costs.items())]
        )
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Себестоимости", index=False)
        await message.answer_document(
            BufferedInputFile(buf.getvalue(), filename="costs.xlsx"),
            caption=f"Сейчас сохранено: <b>{len(costs)}</b> позиций.",
        )

    @dp.message(F.document)
    async def on_document(message: Message, bot: Bot) -> None:
        if not _is_allowed(message):
            return
        doc = message.document
        name = (doc.file_name or "").lower()
        if not (name.endswith(".xlsx") or name.endswith(".csv")):
            await message.answer("Жду Excel (.xlsx) или CSV с колонками nm_id и «Себестоимость».")
            return

        buf = BytesIO()
        await bot.download(doc, destination=buf)
        try:
            updates = _parse_costs_excel(buf.getvalue())
        except ValueError as exc:
            await message.answer(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("costs upload failed")
            await message.answer(f"Не смог разобрать файл: {exc}")
            return

        if not updates:
            await message.answer("В файле не нашёл ни одной строки с заполненной себестоимостью.")
            return

        added, changed = update_costs(updates)
        await message.answer(
            f"Готово. Загружено позиций: <b>{len(updates)}</b> "
            f"(новых: {added}, изменено: {changed})."
        )

    @dp.message(F.text)
    async def fallback(message: Message, state: FSMContext) -> None:
        if not _is_allowed(message):
            return
        await state.clear()
        await message.answer(HELP, reply_markup=MAIN_KB)

    return dp


async def _send_report(message: Message, date_from: date, date_to: date) -> None:
    global _last_wb_call_at
    async with _get_wb_lock():
        wait_left = WB_MIN_INTERVAL_SEC - (time.monotonic() - _last_wb_call_at)
        if wait_left > 0:
            await message.answer(
                f"Подождите ещё {int(wait_left) + 1} сек — у WB лимит 1 запрос в минуту."
            )
            return
        _last_wb_call_at = time.monotonic()

    status = await message.answer("Запрашиваю отчёт у WB, подождите…")
    try:
        bundle = await build_report(settings.wb_api_token, date_from, date_to)
    except WBApiError as exc:
        text = str(exc)
        if "429" in text:
            await status.edit_text(
                "WB API ограничивает этот отчёт до 1 запроса в минуту. "
                "Подождите ~1 минуту и повторите."
            )
        else:
            await status.edit_text(f"Ошибка WB API: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("report failed")
        await status.edit_text(f"Внутренняя ошибка: {exc}")
        return

    if bundle.rows_count == 0:
        await status.edit_text("За указанный период отчёт пуст.")
        return

    await status.edit_text(format_summary_text(date_from, date_to, bundle))

    name_period = f"{date_from.isoformat()}_{date_to.isoformat()}"
    await message.answer_document(
        BufferedInputFile(bundle.excel, filename=f"wb_report_{name_period}.xlsx"),
        caption="Полный отчёт с детализацией",
    )
    if bundle.chart_breakdown_png:
        await message.answer_photo(
            BufferedInputFile(bundle.chart_breakdown_png, filename="breakdown.png"),
            caption="Расшифровка по статьям",
        )
    if bundle.chart_top_sku_png:
        await message.answer_photo(
            BufferedInputFile(bundle.chart_top_sku_png, filename="top_sku.png"),
            caption="Топ товаров",
        )


async def run_polling() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_polling())
