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

It fetches orders through `readyscript-orders/script.py` every 60 seconds. RS credentials
can be stored in the project `.env` or in `readyscript-orders/.env`.

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
