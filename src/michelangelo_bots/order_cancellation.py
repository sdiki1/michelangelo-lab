from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from michelangelo_bots.bot_configuration import is_terminal_order_status
from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotOrder, datetime_now, get_session_factory

logger = logging.getLogger(__name__)
PROCESSING_TIMEOUT = timedelta(minutes=10)


@dataclass(frozen=True)
class CancellationResult:
    outcome: str
    order_number: str
    error: str | None = None


async def get_customer_order(
    order_id: int,
    *,
    bot_user_id: int,
    platform: str,
) -> BotOrder | None:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(BotOrder).where(
                    BotOrder.id == order_id,
                    BotOrder.bot_user_id == bot_user_id,
                    BotOrder.platform == platform,
                )
            )
        ).scalar_one_or_none()


async def cancel_customer_order(
    order_id: int,
    *,
    bot_user_id: int,
    platform: str,
    platform_user_id: str,
    settings: Settings,
) -> CancellationResult:
    """Claims one cancellation attempt, calls ReadyScript, then stores its outcome."""
    now = datetime_now()
    async with get_session_factory()() as session:
        order = (
            await session.execute(
                select(BotOrder)
                .where(
                    BotOrder.id == order_id,
                    BotOrder.bot_user_id == bot_user_id,
                    BotOrder.platform == platform,
                    BotOrder.platform_user_id == platform_user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if order is None:
            return CancellationResult("not_found", str(order_id))

        number = order.external_order_number or order.external_order_id
        if order.cancellation_state == "cancelled" or _is_cancelled(order.status):
            return CancellationResult("cancelled", number)
        if is_terminal_order_status(order.status):
            return CancellationResult("too_late", number)
        if order.external_source != "readyscript":
            return CancellationResult("failed", number, "Заказ не связан с ReadyScript")
        if (
            order.cancellation_state == "processing"
            and order.cancellation_requested_at
            and now - order.cancellation_requested_at < PROCESSING_TIMEOUT
        ):
            return CancellationResult("processing", number)

        order.cancellation_state = "processing"
        order.cancellation_requested_at = now
        order.cancellation_attempts = (order.cancellation_attempts or 0) + 1
        order.cancellation_error = None
        await session.commit()
        external_order_id = order.external_order_id

    try:
        api_result = await asyncio.to_thread(
            cancel_readyscript_order,
            settings,
            external_order_id,
            platform,
            platform_user_id,
        )
    except Exception as exc:
        error = str(exc)[:1000]
        logger.exception(
            "ReadyScript/CDEK cancellation failed: local_order_id=%s external_order_id=%s",
            order_id,
            external_order_id,
        )
        await _store_failure(order_id, error)
        return CancellationResult("failed", number, error)

    if not api_result.get("success"):
        error = str(api_result.get("message") or "ReadyScript отклонил отмену")[:1000]
        await _store_failure(order_id, error)
        return CancellationResult("failed", number, error)

    async with get_session_factory()() as session:
        order = await session.get(BotOrder, order_id, with_for_update=True)
        if order is None:
            return CancellationResult("cancelled", number)
        completed_at = datetime_now()
        order.cancellation_state = "cancelled"
        order.cancelled_at = completed_at
        order.cancellation_error = None
        order.status = "cancelled"
        # Клиент уже получает отдельное подтверждение из обработчика кнопки.
        order.last_customer_status = "cancelled"
        raw_payload = dict(order.raw_payload or {})
        raw_payload["customer_cancellation"] = {
            "completed_at": completed_at.isoformat(),
            "readyscript": api_result,
        }
        order.raw_payload = raw_payload
        order.updated_at = completed_at
        await session.commit()
    return CancellationResult("cancelled", number)


async def _store_failure(order_id: int, error: str) -> None:
    async with get_session_factory()() as session:
        order = await session.get(BotOrder, order_id, with_for_update=True)
        if order is not None and order.cancellation_state != "cancelled":
            order.cancellation_state = "failed"
            order.cancellation_error = error
            order.updated_at = datetime_now()
            await session.commit()


def cancel_readyscript_order(
    settings: Settings,
    order_id: str,
    platform: str,
    platform_user_id: str,
) -> dict[str, Any]:
    # Deferred import avoids a max_bot -> cancellation -> sync -> max_bot cycle.
    from michelangelo_bots.readyscript_sync import (
        configure_script,
        load_readyscript_script,
        readyscript_settings,
        validate_api_settings,
    )

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
        return client.cancel_order(order_id, platform, platform_user_id)
    finally:
        client.close()


def _is_cancelled(status: str | None) -> bool:
    normalized = (status or "").strip().lower()
    return "cancel" in normalized or "отмен" in normalized
