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

Optional:

- `DATABASE_URL` defaults to `postgresql+asyncpg://michelangelo:michelangelo@postgres:5432/michelangelo`
- `MAX_API_BASE_URL` defaults to `https://platform-api.max.ru`
- `MAX_POLL_TIMEOUT_SECONDS` defaults to `30`

`MINIAPP_URL` remains a fallback. If `TELEGRAM_MINIAPP_URL` or `MAX_MINIAPP_URL`
is set, that platform uses its own miniapp URL.

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

## ReadyScript module (site ↔ admin panel)

The boxed storefront at `https://michelangelo-lab.ru` talks to this panel at
`https://admin.michelangelo-lab.ru` through the PHP module in
[`readyscript-module/`](readyscript-module/README.md). It captures the Telegram/MAX
identity when the shop is opened inside a miniapp, stamps it onto the order, and
pushes webhooks here.

Set the shared signing secret on both sides:

```bash
RS_MODULE_SECRET=<same value as in the ReadyScript module settings>
```

Endpoints exposed for the module (all HMAC-signed, see `webhook_security.py`):

```text
POST /api/rs/orders     order created or changed
POST /api/rs/events     batch of storefront actions
POST /api/rs/identity   verify initData, return the trusted user id
```

Headers: `X-ML-Timestamp` and `X-ML-Signature: sha256=<hex>`, where the signature
covers `"{timestamp}.{raw_body}"`. Requests older than
`RS_SIGNATURE_TOLERANCE_SECONDS` (default 300) are rejected.

Telegram `initData` is verified **here**, with the bot token — an id claimed by the
site without a valid signature is stored as `bind_source="manual"` and never
overrides a verified one.

Storefront statistics live under **Витрина** in the admin panel; order bindings are
shown in the **Привязка** column on the orders page.

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
