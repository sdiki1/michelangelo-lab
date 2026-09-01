from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.bot_configuration import order_values, render_template, setting
from michelangelo_bots.config import Settings
from michelangelo_bots.db import (
    BotOrder,
    BotUser,
    OrderStatusNotification,
    datetime_now,
)
from michelangelo_bots.max_bot import MaxClient

logger = logging.getLogger(__name__)


async def deliver_customer_notifications(session: AsyncSession, settings: Settings) -> int:
    result = await session.execute(
        select(BotOrder, BotUser)
        .join(BotUser, BotUser.id == BotOrder.bot_user_id)
        .where(
            BotOrder.bot_user_id.is_not(None),
            (BotOrder.customer_notified_at.is_(None))
            | (BotOrder.last_customer_status.is_distinct_from(BotOrder.status)),
        )
        .order_by(BotOrder.updated_at)
        .limit(300)
    )
    confirmation_template = await setting(session, "customer_order_template")
    status_template = await setting(session, "customer_status_template")
    telegram_manager_url = await setting(session, "telegram_manager_url")
    max_manager_url = await setting(session, "max_manager_url")
    delivered_count = 0

    async with httpx.AsyncClient(
        base_url=str(settings.max_api_base_url).rstrip("/"), timeout=30
    ) as http_client:
        max_client = MaxClient(
            settings.max_bot_token,
            str(settings.max_api_base_url),
            http_client=http_client,
        )
        for order, user in result.all():
            current_status = (order.status or "").strip()
            if order.customer_notified_at is None:
                attempted_at = datetime_now()
                order.customer_notification_attempts = (
                    order.customer_notification_attempts or 0
                ) + 1
                order.customer_notification_last_attempt_at = attempted_at
                try:
                    await send_customer_message(
                        http_client,
                        max_client,
                        settings,
                        user,
                        render_template(confirmation_template, order_values(order, user)),
                        telegram_manager_url=telegram_manager_url,
                        max_manager_url=max_manager_url,
                    )
                except Exception as exc:
                    order.customer_notification_error = str(exc)[:1000]
                    order.customer_notification_first_failed_at = (
                        order.customer_notification_first_failed_at or attempted_at
                    )
                    logger.exception("Customer order confirmation failed: order_id=%s", order.id)
                else:
                    order.customer_notified_at = datetime_now()
                    order.customer_notification_error = None
                    order.customer_notification_first_failed_at = None
                    order.last_customer_status = current_status or None
                    delivered_count += 1
                await session.commit()
                continue

            if not current_status:
                continue
            if order.last_customer_status is None:
                order.last_customer_status = current_status
                await session.commit()
                continue
            if order.last_customer_status == current_status:
                continue

            ledger = (
                await session.execute(
                    select(OrderStatusNotification).where(
                        OrderStatusNotification.order_id == order.id,
                        OrderStatusNotification.status == current_status,
                    )
                )
            ).scalar_one_or_none()
            if ledger and ledger.delivered_at:
                order.last_customer_status = current_status
                await session.commit()
                continue
            if ledger is None:
                ledger = OrderStatusNotification(order_id=order.id, status=current_status)
                session.add(ledger)

            attempted_at = datetime_now()
            ledger.attempt_count = (ledger.attempt_count or 0) + 1
            ledger.last_attempt_at = attempted_at
            try:
                await send_customer_message(
                    http_client,
                    max_client,
                    settings,
                    user,
                    render_template(status_template, order_values(order, user)),
                    telegram_manager_url=telegram_manager_url,
                    max_manager_url=max_manager_url,
                )
            except Exception as exc:
                ledger.error = str(exc)[:1000]
                ledger.first_failed_at = ledger.first_failed_at or attempted_at
                logger.exception("Customer status notification failed: order_id=%s", order.id)
            else:
                ledger.delivered_at = datetime_now()
                ledger.error = None
                ledger.first_failed_at = None
                order.last_customer_status = current_status
                delivered_count += 1
            await session.commit()

    return delivered_count


async def send_customer_message(
    http_client: httpx.AsyncClient,
    max_client: MaxClient,
    settings: Settings,
    user: BotUser,
    text: str,
    *,
    telegram_manager_url: str,
    max_manager_url: str,
) -> None:
    recipient = user.chat_id or user.platform_user_id
    if not recipient:
        raise RuntimeError("У клиента нет chat_id")
    if user.platform == "telegram":
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        payload: dict[str, Any] = {"chat_id": recipient, "text": text}
        if telegram_manager_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{
                    "text": "✉️ Связаться с менеджером",
                    "url": telegram_manager_url,
                }]]
            }
        response = await http_client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API rejected message: {data}")
        return

    if user.platform == "max":
        if not settings.max_bot_token:
            raise RuntimeError("MAX_BOT_TOKEN is not set")
        attachments: list[dict[str, Any]] = []
        if max_manager_url:
            attachments.append(
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [[{
                            "type": "link",
                            "text": "✉️ Связаться с менеджером",
                            "url": max_manager_url,
                        }]]
                    },
                }
            )
        await max_client.send_message(
            recipient,
            text,
            attachments,
            recipient_type="chat_id" if user.chat_id else "user_id",
        )
        return

    raise RuntimeError(f"Unsupported customer platform: {user.platform}")


def extract_delivery_status(raw_order: dict[str, Any]) -> str | None:
    """Prefer CDEK delivery fields, then fall back to the ReadyScript status title."""
    direct_keys = ("cdek_status_title", "cdek_status")
    for key in direct_keys:
        value = raw_order.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()

    for container_key in ("cdek", "delivery", "shipment", "courier"):
        value = raw_order.get(container_key)
        if not isinstance(value, dict):
            continue
        for key in ("status_title", "status_name", "status", "state", "code"):
            nested = value.get(key)
            if nested not in (None, "") and not isinstance(nested, (dict, list)):
                return str(nested).strip()
    for key in ("delivery_status_title", "delivery_status", "status_title", "status"):
        value = raw_order.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return None
