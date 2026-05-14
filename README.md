# wb-analytics

Сервис расшифровки финансового отчёта Wildberries «отчёт о реализации»
(`/api/v5/supplier/reportDetailByPeriod`). Достаёт данные через WB API,
группирует их по статьям удержаний и по товарам, отдаёт результат:

- Telegram-боту — краткой сводкой, Excel-файлом с детализацией и графиками;
- через REST API (FastAPI) — JSON-сводкой или Excel-файлом.

Хранение данных пока не используется: всё считается на лету.

## Запуск

1. Создать виртуальное окружение и установить зависимости:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Скопировать `.env.example` в `.env` и заполнить:

   - `WB_API_TOKEN` — JWT-токен из личного кабинета WB (раздел «Профиль →
     Доступ к API», категория «Статистика»);
   - `TELEGRAM_BOT_TOKEN` — токен бота от @BotFather;
   - `TELEGRAM_ALLOWED_USER_IDS` — список Telegram user id через запятую
     (если пусто — отвечаем всем, но в single-tenant сценарии лучше указать
     свой id).

3. Запустить Telegram-бота:

   ```bash
   python -m app.bot
   ```

4. Запустить REST API (опционально, отдельный процесс):

   ```bash
   uvicorn app.main:app --reload
   ```

## Команды бота

- `/report 2025-01-01 2025-01-07` — отчёт за период;
- `/last_week` — отчёт за последние 7 дней;
- `/help`.

## REST API

- `GET /report/summary?from=YYYY-MM-DD&to=YYYY-MM-DD` — JSON со сводкой,
  расшифровкой по статьям, разбивкой по операциям и SKU.
- `GET /report/excel?from=YYYY-MM-DD&to=YYYY-MM-DD` — XLSX-файл.

## Структура

```
app/
  config.py          # настройки из .env
  wb_client.py       # асинхронный клиент WB Statistics API + пагинация
  decoder.py         # расшифровка отчёта (по статьям / операциям / SKU)
  report_builder.py  # генерация Excel и графиков (matplotlib)
  service.py         # связка fetch → decode → build
  bot.py             # aiogram-бот (long polling)
  main.py            # FastAPI-приложение
```
