# Michelangelo Bots

Telegram and MAX bots with the same menu/content logic.

## Environment

Copy `.env.example` to `.env` and fill tokens:

```bash
cp .env.example .env
```

Required variables:

- `TELEGRAM_BOT_TOKEN`
- `MAX_BOT_TOKEN`
- `MINIAPP_URL`
- `TELEGRAM_MINIAPP_URL`
- `MAX_MINIAPP_URL`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_BASE_URL` — публичный HTTPS-адрес админки для универсальных кнопок
  «Ответить клиенту» в Telegram и MAX

Optional:

- `DATABASE_URL` defaults to `postgresql+asyncpg://michelangelo:michelangelo@postgres:5432/michelangelo`
- `MAX_BOT_USERNAME` — username MAX-бота без `@`; если не указан, загружается через `/me`
- `MAX_API_BASE_URL` defaults to `https://platform-api2.max.ru`
- `MAX_POLL_TIMEOUT_SECONDS` defaults to `30`
- `ORDER_NOTIFICATION_TELEGRAM_CHAT_IDS` — Telegram chat ID администраторов через запятую
- `ORDER_NOTIFICATION_MAX_USER_IDS` — MAX user ID администраторов через запятую
- `ORDER_NOTIFICATION_MAX_CHAT_IDS` — MAX chat ID административных групп через запятую
- `NOTIFICATION_ALERT_AFTER_MINUTES` — возраст массового сбоя до тревоги (по умолчанию 180)
- `NOTIFICATION_ALERT_MIN_FAILURES` — сколько одновременных ошибок считать массовым сбоем (3)
- `NOTIFICATION_ALERT_ISOLATED_AFTER_MINUTES` — тревога об одном зависшем сообщении (1440)
- `NOTIFICATION_ALERT_REMINDER_MINUTES` — интервал повторного сигнала о продолжающемся сбое (360)

Docker-образ устанавливает официальный корневой сертификат Минцифры из
`certs/russian_trusted_root_ca.crt`: он требуется домену `platform-api2.max.ru`.
Проверка TLS не отключается. При плановой замене сертификата сверяйте SHA-256
отпечаток файла с публикацией Госуслуг перед обновлением образа.

`MINIAPP_URL` remains a fallback. If `TELEGRAM_MINIAPP_URL` or `MAX_MINIAPP_URL`
is set, that platform uses its own miniapp URL.

После первого baseline-запуска ReadyScript polling отправляет администраторам
уведомление о каждом новом заказе из Telegram/MAX. В сообщение входят номер,
сумма, состав заказа, имя, телефон, email, messenger user ID и username, если
он известен боту. Успешные доставки сохраняются в БД и не дублируются на
следующем цикле polling.

Уведомление о Telegram-заказе содержит кнопку «Написать пользователю»: она
ведёт на `https://t.me/<username>`, а при отсутствии username — на
`tg://user?id=<telegram_user_id>`.

Чтобы узнать собственный MAX user ID для
`ORDER_NOTIFICATION_MAX_USER_IDS`, отправьте MAX-боту команду `/getmyid` в
личном чате. Бот вернёт ID и готовую строку для `.env`.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
michelangelo-telegram-bot
michelangelo-max-bot
```

## Docker

Run both bots:

```bash
docker compose up --build
```

Admin panel:

```text
http://localhost:8000
```

The admin panel uses HTTP Basic Auth with `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
It shows total users, Telegram/MAX split, total actions, popular actions, recent events,
user search/filtering, user profiles, raw platform profile data, and interaction history.

### Перенос пользователей из старой админки

В разделах `/clients` и `/users` доступна кнопка «Перенести пользователей». Мастер
принимает CSV/TSV (UTF-8 или Windows-1251) и XLSX до 15 МБ/50 000 строк, показывает
предварительную проверку и отчёт по ошибкам, а затем создаёт новых пользователей или
дополняет, перезаписывает либо пропускает существующих. Дубли определяются по паре
«мессенджер + User ID». Идентификаторы импортируются как текст, поэтому ведущие нули
из текстовых ячеек Excel не теряются. На странице доступен CSV-шаблон.

Если в старой выгрузке колонка называется просто `ID`, её нужно явно подтвердить как
Telegram/MAX User ID и выбрать платформу. Это защищает от случайного импорта внутреннего
номера записи старой админки. При смене токена бота мессенджер может запретить отправку
перенесённому пользователю, пока тот не запустит нового бота самостоятельно.

### Bot settings and customer notifications

`/bot-settings` manages administrator order/inbox templates, customer order confirmations,
CDEK status messages and manager links. The message menu is managed in `/bot-builder`.
The ReadyScript synchronization worker sends a confirmation for each new linked order and
then sends one customer notification for every newly observed delivery status. Existing orders
are baselined during the first run, so deploying this version does not generate an old-order burst.

Неуспешная клиентская доставка повторяется в каждом цикле синхронизации и учитывается в
отдельном журнале попыток. Самопроверка отправляет администраторам один агрегированный сигнал,
если минимум три сообщения не доставляются три часа, либо одно сообщение зависло на сутки.
Продолжающийся сбой напоминается не чаще раза в шесть часов; после очистки очереди приходит
одно сообщение о восстановлении. Тем же способом контролируется длительный сбой получения
заказов и статусов ReadyScript/СДЭК. Пороги задаются переменными `NOTIFICATION_ALERT_*`, а тексты
сигнала и восстановления редактируются в `/bot-settings`.

Website questions can be forwarded into both administrator channels with:

```text
POST /api/site/messages
X-ReadyScript-Secret: <READYSCRIPT_WEBHOOK_SECRET>
```

The JSON body accepts `client_id`, `text`, `photo_urls`, customer contact fields and an optional
`reply_url`. The latter is used by the administrator-channel reply button for website clients.

### Визуальный конструктор бота

`/bot-builder` — схема сообщений и переходов, отдельно для Telegram и MAX.
Каждый блок содержит текст, одно необязательное фото (JPEG/PNG до 10 МБ)
и инлайн-кнопки. Кнопка ведёт к другому блоку или открывает HTTPS-ссылку,
в том числе мини-приложение. Блоки можно перемещать, менять стартовое сообщение,
соединять кружок кнопки с блоком или выбирать переход в списке справа.
«Проверить сценарий» позволяет пройти ветки без отправки сообщений клиентам.

При первом запуске обновлённой админки текущее приветствие, активные кнопки,
настроенные ответы и стандартные разделы автоматически переносятся в схему
каждого мессенджера. Существующие настройки остаются в базе; повторный запуск
не перезаписывает схемы. Старые callback-кнопки продолжают работать.
Уведомления о заказах, отмена заказов и переписка с менеджером работают отдельно.

«Сохранить черновик» сохраняет работу без изменения действующего бота.
«Опубликовать» применяет схему к следующим сообщениям без перезапуска ботов.
Сохранение проверяет переходы и наличие фото; конфликт изменений из двух вкладок
возвращает ошибку, вместо того чтобы затереть чужую работу. Для возврата к прежнему
сценарию можно восстановить записи `flow_<platform>_live` и `flow_<platform>_draft`
из резервной копии `bot_settings`.

Для установки обновите код и пересоберите три сервиса:

```bash
docker compose up -d --build admin telegram-bot max-bot
```

Все три сервиса должны использовать один `UPLOADS_DIR`: в compose подключён
общий том `uploads-data`, у ботов — только для чтения. Загруженные фото доступны
в браузере только после входа в админку. Для Telegram текст длиннее 1024 символов
отправляется после фото отдельным сообщением с кнопками.

### Chat with clients

`/chats` — список диалогов, `/chats/{user_id}` — переписка с клиентом и форма
отправки сообщения. Входящие реплики берутся из `bot_events`, исходящие
сохраняются в `chat_messages` вместе с результатом доставки: неотправленное
сообщение остаётся в ленте с пометкой «не доставлено» и текстом ошибки.
Отправка идёт тем же ботом, что и рассылки — Telegram `sendMessage` или MAX
`/messages`. Написать первым можно только клиенту с известным `chat_id`.

В ленте показываются фото и видео с обеих сторон. Входящие медиа Telegram
отдаёт прокси-эндпоинт админки `/media/telegram/{file_id}` (прямая ссылка
Telegram содержит токен бота, наружу она не уходит), входящие медиа MAX берутся
по ссылкам из апдейта. Форма ответа принимает несколько файлов
(`image/*`, `video/*`): текст можно оставить пустым, если приложено вложение.
Telegram получает `sendPhoto`/`sendVideo`, а несколько файлов — `sendMediaGroup`
(текст длиннее 1024 символов уходит отдельным сообщением, потому что не влезает
в подпись). Для MAX файл сначала загружается через `/uploads`, а отправка
повторяется, пока MAX дообрабатывает видео (`attachment.not.ready`).
Отправленные файлы лежат в `UPLOADS_DIR/chats` и показываются в ленте через
`/media/chat/{message_id}/{index}`.

### Orders

`/orders/{order_id}` — карточка заказа с кнопкой «Перейти в чат с клиентом»
(`/chats/{user_id}?order={order_id}`, чат открывается с контекстом заказа, и
отправленное сообщение сохраняется со ссылкой на него). В таблицах заказов,
клиентов и пользователей кликабельна вся строка, а не только ссылка в первой
колонке.

## ReadyScript orders

Set `READYSCRIPT_WEBHOOK_SECRET` in `.env`.

ReadyScript should send created/updated orders to:

```text
POST /api/readyscript/orders
X-ReadyScript-Secret: <READYSCRIPT_WEBHOOK_SECRET>
Content-Type: application/json
```

Recommended payload:

```json
{
  "order_id": 123,
  "order_num": "A-123",
  "status": "new",
  "total": "5900",
  "currency": "RUB",
  "customer_name": "Иван Иванов",
  "customer_phone": "+79000000000",
  "customer_email": "ivan@example.com",
  "telegram_init_data": "window.Telegram.WebApp.initData"
}
```

If the ReadyScript backend can only pass an already saved Telegram id, send
`telegram_user_id` instead. This is accepted only through the protected server-to-server
endpoint above.

ReadyScript polling runs automatically in the `readyscript-sync` compose service:

```bash
docker compose up -d readyscript-sync
```

It fetches orders from the ReadyScript API every 60 seconds through the client in
`readyscript-orders/script.py`. RS credentials can be stored in the project `.env` or in
`readyscript-orders/.env`.

To run a single sync manually:

```bash
michelangelo-readyscript-sync
```

## ReadyScript module (polling architecture)

The PHP module in [`readyscript-module/`](readyscript-module/README.md) captures
signed Telegram/MAX mini-app identity, verifies it inside ReadyScript, and writes
`telegram_user_id` or `max_user_id` directly to each order. Both fields are marked
`appVisible`, so the `readyscript-sync` service reads them through the standard
ReadyScript API. No outgoing ReadyScript webhook or module cron is required for
order synchronization.

Order notifications in Telegram and MAX also include a configurable cancel
button. A confirmed request is ownership-checked, de-duplicated, then sent to
the module's protected `michelangelo.cancelOrder` API method. ReadyScript uses
the delivery type's native deletion method, so a CDEK 2.0 delivery order is
deleted from CDEK before the ReadyScript order is marked cancelled. Enable this
method and its cancellation right for the polling OAuth application after
installing module version 2.4.0.0.

PostgreSQL runs in the same compose stack and stores data in the `postgres-data` Docker volume.
Tables are created automatically on service startup:

- `bot_users`: platform, platform user id, chat id, username, names, language, raw profile,
  first/last seen timestamps, last action, last message, total actions.
- `bot_events`: every message/callback with action, event type, text/payload, raw update,
  timestamp, and link to the user.

Run tests:

```bash
pytest
```
