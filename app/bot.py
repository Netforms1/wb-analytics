from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

from app.config import settings
from app.service import build_report, format_summary_text
from app.wb_client import WBApiError

logger = logging.getLogger(__name__)

# WB лимитит reportDetailByPeriod ~1 req/min на токен и продлевает кулдаун при нарушениях.
# Не пускаем запрос чаще, чем раз в WB_MIN_INTERVAL_SEC, чтобы не накапливать штраф.
WB_MIN_INTERVAL_SEC = 70.0
_last_wb_call_at: float = 0.0
_wb_lock = asyncio.Lock()

HELP = (
    "Бот расшифровывает финансовый отчёт WB «реализация».\n\n"
    "Команды:\n"
    "/report ГГГГ-ММ-ДД ГГГГ-ММ-ДД — отчёт за период\n"
    "/last_week — отчёт за последние 7 дней"
)


def _parse_dates(args: str | None) -> tuple[date, date] | None:
    if not args:
        return None
    parts = args.split()
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


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        if not _is_allowed(message):
            return
        await message.answer(HELP)

    @dp.message(Command("last_week"))
    async def cmd_last_week(message: Message) -> None:
        if not _is_allowed(message):
            return
        date_to = date.today()
        date_from = date_to - timedelta(days=7)
        await _send_report(message, date_from, date_to)

    @dp.message(Command("report"))
    async def cmd_report(message: Message, command: Command.CommandObject = None) -> None:
        if not _is_allowed(message):
            return
        parsed = _parse_dates(command.args if command else None)
        if not parsed:
            await message.answer(
                "Использование: /report 2025-01-01 2025-01-07",
                parse_mode=None,
            )
            return
        date_from, date_to = parsed
        await _send_report(message, date_from, date_to)

    @dp.message(F.text)
    async def fallback(message: Message) -> None:
        if not _is_allowed(message):
            return
        await message.answer(HELP)

    return dp


async def _send_report(message: Message, date_from: date, date_to: date) -> None:
    global _last_wb_call_at
    async with _wb_lock:
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
            caption="Топ товаров по выплате",
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
