from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.config import Settings, get_settings
from michelangelo_bots.db import BotOrder, BotUser, datetime_now, get_session_factory, init_db
from michelangelo_bots.telegram_webapp import verify_telegram_init_data

logger = logging.getLogger(__name__)


class ReadyScriptSyncError(RuntimeError):
    pass


async def sync_readyscript_orders(
    *,
    session: AsyncSession,
    settings: Settings,
) -> dict[str, int]:
    orders = fetch_orders(settings)
    imported = 0
    linked = 0

    for raw_order in orders:
        order = await upsert_order(session, settings, raw_order)
        imported += 1
        if order.bot_user_id is not None:
            linked += 1

    await session.commit()
    return {"imported": imported, "linked": linked}


def fetch_orders(settings: Settings) -> list[dict[str, Any]]:
    rs_settings = readyscript_settings(settings)
    validate_api_settings(rs_settings)
    script = load_readyscript_script(settings.readyscript_script_path)
    configure_script(script, rs_settings)

    client = script.ReadyScriptClient(
        api_base=rs_settings["api_base"].rstrip("/"),
        client_id=rs_settings["client_id"],
        client_secret=rs_settings["client_secret"],
        username=rs_settings["username"],
        password=rs_settings["password"],
    )
    try:
        orders = client.get_all_orders()
        enriched_orders = script.enrich_orders(client=client, orders=orders)
        return [order for order in enriched_orders if isinstance(order, dict)]
    finally:
        client.close()


def load_readyscript_script(path: Path) -> ModuleType:
    if not path.exists():
        raise ReadyScriptSyncError(f"ReadyScript script not found: {path}")

    spec = importlib.util.spec_from_file_location("readyscript_orders_script", path)
    if spec is None or spec.loader is None:
        raise ReadyScriptSyncError(f"Could not load ReadyScript script: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_script(script: ModuleType, rs_settings: dict[str, Any]) -> None:
    script.PAGE_SIZE = rs_settings["page_size"]
    script.FETCH_ORDER_DETAILS = rs_settings["fetch_order_details"]
    script.FETCH_USERS = rs_settings["fetch_users"]
    script.REQUEST_DELAY_SECONDS = rs_settings["request_delay_seconds"]
    script.REQUEST_TIMEOUT = rs_settings["request_timeout"]


def readyscript_settings(settings: Settings) -> dict[str, Any]:
    env = load_env_file(settings.readyscript_script_path.parent / ".env")
    return {
        "api_base": first_value(env.get("RS_API_BASE"), settings.readyscript_api_base),
        "client_id": first_value(env.get("RS_CLIENT_ID"), settings.readyscript_client_id),
        "client_secret": first_value(
            env.get("RS_CLIENT_SECRET"),
            settings.readyscript_client_secret,
        ),
        "username": first_value(env.get("RS_USERNAME"), settings.readyscript_username),
        "password": first_value(env.get("RS_PASSWORD"), settings.readyscript_password),
        "page_size": parse_int(env.get("RS_PAGE_SIZE"), settings.readyscript_page_size),
        "fetch_order_details": parse_bool(
            env.get("RS_FETCH_ORDER_DETAILS"),
            settings.readyscript_fetch_order_details,
        ),
        "fetch_users": parse_bool(env.get("RS_FETCH_USERS"), settings.readyscript_fetch_users),
        "request_delay_seconds": parse_float(
            env.get("RS_REQUEST_DELAY_SECONDS"),
            settings.readyscript_request_delay_seconds,
        ),
        "request_timeout": parse_int(
            env.get("RS_REQUEST_TIMEOUT"),
            settings.readyscript_request_timeout,
        ),
        "interval_seconds": parse_int(
            env.get("RS_INTERVAL_SECONDS"),
            settings.readyscript_interval_seconds,
        ),
    }


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def validate_api_settings(rs_settings: dict[str, Any]) -> None:
    missing = [
        name
        for name, value in {
            "RS_CLIENT_ID": rs_settings["client_id"],
            "RS_CLIENT_SECRET": rs_settings["client_secret"],
            "RS_USERNAME": rs_settings["username"],
            "RS_PASSWORD": rs_settings["password"],
        }.items()
        if not value
    ]
    if missing:
        raise ReadyScriptSyncError("Missing ReadyScript settings: " + ", ".join(missing))


async def upsert_order(
    session: AsyncSession,
    settings: Settings,
    raw_order: dict[str, Any],
) -> BotOrder:
    external_order_id = first_value(raw_order.get("id"), raw_order.get("order_id"))
    if external_order_id is None:
        raise ReadyScriptSyncError(f"ReadyScript order has no id: {raw_order!r}")

    identity = extract_customer_identity(raw_order, settings)
    bot_user = await find_or_create_bot_user(session, identity, raw_order)
    now = datetime_now()

    result = await session.execute(
        select(BotOrder).where(
            BotOrder.external_source == "readyscript",
            BotOrder.external_order_id == external_order_id,
        )
    )
    order = result.scalar_one_or_none()
    if order is None:
        order = BotOrder(
            external_source="readyscript",
            external_order_id=external_order_id,
            created_at=now,
        )
        session.add(order)

    order.external_order_number = first_value(raw_order.get("order_num"), raw_order.get("number"))
    order.platform = identity.get("platform")
    order.platform_user_id = identity.get("platform_user_id")
    order.telegram_user_id = (
        identity.get("platform_user_id") if identity.get("platform") == "telegram" else None
    )
    order.bind_source = first_value(raw_order.get("ml_bind_source"), "readyscript_polling")
    order.bot_user_id = bot_user.id if bot_user else None
    order.status = first_value(raw_order.get("status"), raw_order.get("status_title"))
    order.total_amount = first_value(
        raw_order.get("totalcost"),
        raw_order.get("total"),
        raw_order.get("amount"),
    )
    order.currency = first_value(raw_order.get("currency"), raw_order.get("currency_stitle"))
    order.customer_name = identity.get("full_name")
    order.customer_phone = identity.get("phone")
    order.customer_email = identity.get("email")
    order.raw_payload = raw_order
    order.updated_at = now

    if bot_user:
        bot_user.phone = order.customer_phone or bot_user.phone
        bot_user.email = order.customer_email or bot_user.email
        bot_user.full_name = order.customer_name or bot_user.full_name
        bot_user.raw_profile = {**(bot_user.raw_profile or {}), "readyscript_order": raw_order}

    return order


def extract_customer_identity(
    raw_order: dict[str, Any],
    settings: Settings,
) -> dict[str, str | None]:
    user = raw_order.get("readyscript_user")
    if not isinstance(user, dict):
        user = {}

    platform_user_id = extract_platform_user_id(raw_order, settings, "telegram")
    platform = "telegram" if platform_user_id else None

    if platform_user_id is None:
        platform_user_id = extract_platform_user_id(raw_order, settings, "max")
        platform = "max" if platform_user_id else None

    return {
        "platform": platform,
        "platform_user_id": platform_user_id,
        "full_name": first_value(
            raw_order.get("customer_name"),
            raw_order.get("contact_person"),
            raw_order.get("user_fio"),
            user.get("full_name"),
            " ".join(
                part
                for part in [
                    str(user.get("surname") or "").strip(),
                    str(user.get("name") or "").strip(),
                    str(user.get("midname") or "").strip(),
                ]
                if part
            ),
        ),
        "phone": normalize_phone(
            first_value(
                raw_order.get("customer_phone"),
                raw_order.get("user_phone"),
                user.get("phone"),
            )
        ),
        "email": normalize_email(
            first_value(
                raw_order.get("customer_email"),
                raw_order.get("user_email"),
                user.get("e_mail"),
                user.get("email"),
                user.get("login"),
            )
        ),
    }


def extract_platform_user_id(
    raw_order: dict[str, Any],
    settings: Settings,
    platform: str,
) -> str | None:
    init_data_key = f"{platform}_init_data"
    user_id_keys = (
        f"{platform}_user_id",
        f"{platform}_id",
        f"{platform}_uid",
    )

    # Нормализованный контракт модуля: эти поля находятся прямо в заказе.
    # Старые форматы ниже оставлены, чтобы polling пережил поэтапный rollout.

    if platform == "telegram" and raw_order.get(init_data_key):
        init_data = verify_telegram_init_data(
            str(raw_order[init_data_key]),
            settings.telegram_bot_token,
        )
        user = init_data.get("user") or {}
        if user.get("id") is not None:
            return str(user["id"])

    for key in user_id_keys:
        direct_value = raw_order.get(key)
        if direct_value is not None and str(direct_value).strip():
            return str(direct_value).strip()

    if (
        raw_order.get("ml_platform") == platform
        and scalar_string(raw_order.get("ml_platform_user_id"))
    ):
        return scalar_string(raw_order.get("ml_platform_user_id"))

    for item in raw_order.get("platform_data") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).lower()
        value = item.get("value")
        if platform in path and "user" in path and "id" in path:
            extracted = scalar_string(value)
            if extracted:
                return extracted

    return None


async def find_or_create_bot_user(
    session: AsyncSession,
    identity: dict[str, str | None],
    raw_order: dict[str, Any],
) -> BotUser | None:
    platform = identity.get("platform")
    platform_user_id = identity.get("platform_user_id")
    if platform and platform_user_id:
        result = await session.execute(
            select(BotUser).where(
                BotUser.platform == platform,
                BotUser.platform_user_id == platform_user_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = BotUser(
                platform=platform,
                platform_user_id=platform_user_id,
                chat_id=platform_user_id,
                username=None,
                first_seen_at=datetime_now(),
                raw_profile={"readyscript_order": raw_order},
            )
            session.add(user)
            await session.flush()
        return user

    for field_name, value in (
        ("phone", identity.get("phone")),
        ("email", identity.get("email")),
    ):
        if not value:
            continue
        column = BotUser.phone if field_name == "phone" else BotUser.email
        result = await session.execute(select(BotUser).where(column == value))
        user = result.scalar_one_or_none()
        if user is not None:
            return user

    return None


def first_value(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def scalar_string(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("id", "user_id", "telegram_user_id", "max_user_id"):
            if key in value:
                return scalar_string(value[key])
        return None
    if isinstance(value, list):
        return scalar_string(value[0]) if value else None
    return first_value(value)


def normalize_phone(value: str | None) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or value


def normalize_email(value: str | None) -> str | None:
    return value.lower() if value and "@" in value else value


def parse_int(value: object, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip()
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
            continue
        if digits:
            break
    return int(digits) if digits else default


def parse_float(value: object, default: float) -> float:
    if value is None:
        return default
    text = str(value).strip().replace(",", ".")
    allowed = ""
    for char in text:
        if char.isdigit() or char == ".":
            allowed += char
            continue
        if allowed:
            break
    try:
        return float(allowed)
    except ValueError:
        return default


def parse_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def run_once() -> dict[str, int]:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    settings = get_settings()
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await sync_readyscript_orders(session=session, settings=settings)
    logger.info("ReadyScript sync complete: %s", result)
    return result


async def run_loop(interval_seconds: int | None) -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    settings = get_settings()
    rs_settings = readyscript_settings(settings)
    interval = interval_seconds or rs_settings["interval_seconds"] or 60
    session_factory = get_session_factory()

    logger.info("ReadyScript polling started: interval=%ss", interval)
    while True:
        started = time.monotonic()
        try:
            async with session_factory() as session:
                result = await sync_readyscript_orders(session=session, settings=settings)
            logger.info("ReadyScript sync complete: %s", result)
        except Exception:
            logger.exception("ReadyScript sync iteration failed")

        duration = time.monotonic() - started
        await asyncio.sleep(max(1, interval - duration))


def main() -> None:
    parser = argparse.ArgumentParser(description="Import ReadyScript orders into admin DB")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=None)
    args = parser.parse_args()

    if args.loop:
        asyncio.run(run_loop(args.interval))
        return

    started = time.monotonic()
    result = asyncio.run(run_once())
    print(
        "ReadyScript sync complete: "
        f"imported={result['imported']} linked={result['linked']} "
        f"duration={time.monotonic() - started:.1f}s"
    )


if __name__ == "__main__":
    main()
