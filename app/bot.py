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
from app.wb_orders import (
    OrdersApiError,
    fetch_orders,
    fetch_sales,
    format_orders_text,
    format_sales_text,
    summarize_orders,
    summarize_sales,
)
from app.wb_token import NoWBTokenError, get_wb_token, has_user_token, save_wb_token

logger = logging.getLogger(__name__)

# WB лимитит reportDetailByPeriod ~1 req/min на токен и продлевает кулдаун при нарушениях.
WB_MIN_INTERVAL_SEC = 70.0
# Отдельные таймштампы и локи на endpoint, лимиты у них независимые.
_last_call_at: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(endpoint: str) -> asyncio.Lock:
    if endpoint not in _locks:
        _locks[endpoint] = asyncio.Lock()
    return _locks[endpoint]


async def _wait_for_window(endpoint: str) -> None:
    async with _get_lock(endpoint):
        last = _last_call_at.get(endpoint, 0.0)
        wait_left = WB_MIN_INTERVAL_SEC - (time.monotonic() - last)
        if wait_left > 0:
            await asyncio.sleep(wait_left)
        _last_call_at[endpoint] = time.monotonic()


# ── UI: bottom reply keyboard ────────────────────────────────────────────────
BTN_WEEK = "📊 Реализация за неделю"
BTN_PERIOD = "📅 Реализация за период"
BTN_ORDERS = "📦 Заказы за неделю"
BTN_SALES = "✅ Выкупы за неделю"
BTN_CATALOG = "🛒 Каталог + себестоимость"
BTN_COSTS = "💰 Мои себестоимости"
BTN_TOKEN = "🔑 WB-токен"
BTN_HELP = "ℹ️ Справка"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_PERIOD)],
        [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_SALES)],
        [KeyboardButton(text=BTN_CATALOG), KeyboardButton(text=BTN_COSTS)],
        [KeyboardButton(text=BTN_TOKEN), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

HELP = (
    "Бот расшифровывает финансовые данные WB и считает прибыль "
    "с учётом вашей себестоимости.\n\n"
    "Меню снизу:\n"
    "📊 Реализация за неделю — недельный отчёт о реализации\n"
    "📅 Реализация за период — указать даты вручную\n"
    "📦 Заказы за неделю — все заказы (включая отменённые)\n"
    "✅ Выкупы за неделю — фактические выкупы и возвраты\n"
    "🛒 Каталог + себестоимость — Excel-шаблон с товарами; заполните колонку «Себестоимость» и отправьте обратно\n"
    "💰 Мои себестоимости — показать текущий список\n"
    "🔑 WB-токен — сохранить ваш JWT WB API (сообщение удаляется автоматически)\n\n"
    "Лимит WB: ~1 запрос в минуту на каждый эндпоинт."
)


class ReportStates(StatesGroup):
    waiting_period = State()
    waiting_token = State()


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

    @dp.message(F.text == BTN_ORDERS)
    @dp.message(Command("orders"))
    async def on_orders(message: Message) -> None:
        if not _is_allowed(message):
            return
        try:
            token = get_wb_token()
        except NoWBTokenError as exc:
            await message.answer(str(exc))
            return
        date_to = date.today()
        date_from = date_to - timedelta(days=7)
        status = await message.answer("Тяну заказы из WB (учитываю лимит ~1 req/min)…")
        await _wait_for_window("orders")
        try:
            items = await fetch_orders(token, date_from)
        except OrdersApiError as exc:
            text = str(exc)
            if "429" in text:
                await status.edit_text(
                    "WB ограничивает /orders до 1 запроса в минуту. Подождите ~1 мин и повторите."
                )
            else:
                await status.edit_text(f"Ошибка WB API: {exc}")
            return
        # API возвращает заказы по dateFrom без верхней границы — обрежем сами.
        items = [
            i for i in items
            if (i.get("date") or "")[:10] <= date_to.isoformat()
        ]
        summary = summarize_orders(items)
        await status.edit_text(format_orders_text(date_from, date_to, summary))

    @dp.message(F.text == BTN_SALES)
    @dp.message(Command("sales"))
    async def on_sales(message: Message) -> None:
        if not _is_allowed(message):
            return
        try:
            token = get_wb_token()
        except NoWBTokenError as exc:
            await message.answer(str(exc))
            return
        date_to = date.today()
        date_from = date_to - timedelta(days=7)
        status = await message.answer("Тяну выкупы из WB (учитываю лимит ~1 req/min)…")
        await _wait_for_window("sales")
        try:
            items = await fetch_sales(token, date_from)
        except OrdersApiError as exc:
            text = str(exc)
            if "429" in text:
                await status.edit_text(
                    "WB ограничивает /sales до 1 запроса в минуту. Подождите ~1 мин и повторите."
                )
            else:
                await status.edit_text(f"Ошибка WB API: {exc}")
            return
        items = [
            i for i in items
            if (i.get("date") or "")[:10] <= date_to.isoformat()
        ]
        summary = summarize_sales(items)
        await status.edit_text(format_sales_text(date_from, date_to, summary))

    @dp.message(F.text == BTN_TOKEN)
    @dp.message(Command("set_token"))
    async def on_token_prompt(message: Message, state: FSMContext) -> None:
        if not _is_allowed(message):
            return
        await state.set_state(ReportStates.waiting_token)
        status = "сохранён" if has_user_token() else "ещё не задан"
        await message.answer(
            f"Текущий WB-токен: <b>{status}</b>.\n\n"
            "Пришлите новый JWT-токен следующим сообщением.\n"
            "Я сохраню его и сразу удалю ваше сообщение, чтобы токен не остался в истории чата."
        )

    @dp.message(ReportStates.waiting_token, F.text)
    async def on_token_received(message: Message, state: FSMContext, bot: Bot) -> None:
        if not _is_allowed(message):
            return
        await state.clear()
        raw = (message.text or "").strip()
        # быстрая sanity-проверка: WB-токен — JWT, всегда три части через точку
        if raw.count(".") != 2 or len(raw) < 50:
            await message.answer("Это не похоже на WB JWT-токен. Сообщение оставлю в чате.")
            return

        try:
            save_wb_token(raw)
        except Exception as exc:  # noqa: BLE001
            logger.exception("save token failed")
            await message.answer(f"Не смог сохранить токен: {exc}")
            return

        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:  # noqa: BLE001
            await message.answer(
                "Токен сохранил, но удалить ваше сообщение не получилось. "
                "Удалите его, пожалуйста, вручную."
            )
            return

        await message.answer("✅ WB-токен сохранён. Сообщение с токеном удалил.")

    @dp.message(F.text == BTN_CATALOG)
    @dp.message(Command("catalog"))
    async def on_catalog(message: Message) -> None:
        if not _is_allowed(message):
            return
        try:
            token = get_wb_token()
        except NoWBTokenError as exc:
            await message.answer(str(exc))
            return
        status = await message.answer("Скачиваю каталог из WB…")
        try:
            catalog = await fetch_catalog(token)
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


async def _wait_for_wb_window() -> None:
    await _wait_for_window("realization")


async def _send_report(message: Message, date_from: date, date_to: date) -> None:
    try:
        token = get_wb_token()
    except NoWBTokenError as exc:
        await message.answer(str(exc))
        return

    status = await message.answer("Готовлю отчёт…")

    async def on_progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except Exception:  # noqa: BLE001
            pass

    try:
        bundle = await build_report(
            token, date_from, date_to,
            on_progress=on_progress,
            before_wb_call=_wait_for_wb_window,
        )
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
