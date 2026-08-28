from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.bot_configuration import order_values, render_template, setting
from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotOrder, BotUser, datetime_now
from michelangelo_bots.max_bot import MaxClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdminTarget:
    platform: Literal["telegram", "max"]
    recipient_id: str
    recipient_type: Literal["chat_id", "user_id"]

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.recipient_type}:{self.recipient_id}"


def parse_recipient_ids(value: str) -> list[str]:
    normalized = value.replace(";", ",").replace("\n", ",")
    result: list[str] = []
    for part in normalized.split(","):
        recipient_id = part.strip()
        if recipient_id and recipient_id not in result:
            result.append(recipient_id)
    return result


def admin_targets(settings: Settings) -> list[AdminTarget]:
    targets = [
        AdminTarget("telegram", recipient_id, "chat_id")
        for recipient_id in parse_recipient_ids(
            settings.order_notification_telegram_chat_ids
        )
    ]
    targets.extend(
        AdminTarget("max", recipient_id, "user_id")
        for recipient_id in parse_recipient_ids(settings.order_notification_max_user_ids)
    )
    targets.extend(
        AdminTarget("max", recipient_id, "chat_id")
        for recipient_id in parse_recipient_ids(settings.order_notification_max_chat_ids)
    )
    return targets


def build_order_notification(
    order: BotOrder,
    user: BotUser | None,
    template: str | None = None,
) -> str:
    if template is not None:
        return render_template(template, order_values(order, user))
    platform_title = {"telegram": "Telegram", "max": "MAX"}.get(order.platform or "")
    order_number = order.external_order_number or order.external_order_id
    lines = [
        "🦋 Оформлен новый заказ",
        f"Заказ: №{order_number}",
    ]
    if platform_title:
        lines.append(f"Мессенджер: {platform_title}")
    else:
        lines.append("Источник: сайт / ReadyScript")

    if order.total_amount:
        amount = f"{order.total_amount} {order.currency or ''}".strip()
        lines.append(f"Сумма: {amount}")
    if order.status:
        lines.append(f"Статус: {order.status}")

    lines.append("")
    lines.append("Контакт покупателя:")
    lines.append(f"Имя: {order.customer_name or value_from_user(user, 'full_name') or '—'}")
    lines.append(f"Телефон: {order.customer_phone or value_from_user(user, 'phone') or '—'}")
    lines.append(f"Email: {order.customer_email or value_from_user(user, 'email') or '—'}")

    username = value_from_user(user, "username")
    if username:
        lines.append(f"Username: @{username.lstrip('@')}")
    if platform_title:
        lines.append(f"{platform_title} user ID: {order.platform_user_id or '—'}")

    items = order_items(order)
    if items:
        lines.extend(["", "Состав заказа:", *items])

    return "\n".join(lines)


def value_from_user(user: BotUser | None, field: str) -> str | None:
    if user is None:
        return None
    value = getattr(user, field, None)
    return str(value).strip() if value else None


def order_items(order: BotOrder) -> list[str]:
    payload = order.raw_payload or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return []

    result: list[str] = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        title = first_text(item.get("title"), item.get("name"), item.get("product_title"))
        quantity = first_text(item.get("amount"), item.get("quantity"), item.get("qty"))
        if title:
            result.append(f"• {title}" + (f" × {quantity}" if quantity else ""))
    if len(items) > 10:
        result.append(f"• ещё позиций: {len(items) - 10}")
    return result


def first_text(*values: object) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


async def deliver_pending_order_notifications(
    session: AsyncSession,
    settings: Settings,
) -> int:
    targets = admin_targets(settings)
    if not targets:
        logger.warning(
            "Order notifications are disabled: configure "
            "ORDER_NOTIFICATION_TELEGRAM_CHAT_IDS or ORDER_NOTIFICATION_MAX_*_IDS"
        )
        return 0

    result = await session.execute(
        select(BotOrder)
        # Уведомляем обо всех заказах ReadyScript. Обычные заказы сайта не
        # имеют platform/platform_user_id, но для менеджеров они не менее
        # важны, чем заказы из Telegram/MAX miniapp.
        .where(BotOrder.admin_notified_at.is_(None))
        .order_by(BotOrder.created_at)
        .limit(100)
    )
    orders = list(result.scalars())
    notified = 0
    template = await setting(session, "admin_order_template")

    async with httpx.AsyncClient(
        base_url=str(settings.max_api_base_url).rstrip("/"),
        timeout=30,
    ) as http_client:
        max_client = MaxClient(
            token=settings.max_bot_token,
            base_url=str(settings.max_api_base_url),
            http_client=http_client,
        )
        for order in orders:
            user = await session.get(BotUser, order.bot_user_id) if order.bot_user_id else None
            message = build_order_notification(order, user, template)
            delivered = set(order.admin_notification_delivered or [])
            order.admin_notification_attempts = (order.admin_notification_attempts or 0) + 1

            for target in targets:
                if target.key in delivered:
                    continue
                try:
                    await send_notification(
                        http_client,
                        max_client,
                        settings,
                        target,
                        message,
                        order=order,
                        user=user,
                    )
                except Exception as exc:
                    order.admin_notification_error = str(exc)[:1000]
                    logger.exception(
                        "Order notification failed: order=%s target=%s",
                        order.external_order_id,
                        target.key,
                    )
                else:
                    delivered.add(target.key)
                    order.admin_notification_delivered = sorted(delivered)
                    order.admin_notification_error = None
                await session.commit()

            if all(target.key in delivered for target in targets):
                order.admin_notified_at = datetime_now()
                order.admin_notification_error = None
                await session.commit()
                notified += 1

    return notified


async def send_notification(
    http_client: httpx.AsyncClient,
    max_client: MaxClient,
    settings: Settings,
    target: AdminTarget,
    message: str,
    *,
    order: BotOrder,
    user: BotUser | None,
) -> None:
    if target.platform == "telegram":
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        payload: dict[str, object] = {"chat_id": target.recipient_id, "text": message}
        reply_markup = universal_telegram_contact_reply_markup(order, user, settings)
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = await http_client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json=payload,
        )
        if response.is_error:
            raise RuntimeError(
                f"Telegram API HTTP {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API rejected notification: {data}")
        return

    if not settings.max_bot_token:
        raise RuntimeError("MAX_BOT_TOKEN is not set")
    attachments: list[dict[str, object]] = []
    contact_url = universal_contact_url(order, user, settings)
    if contact_url:
        attachments.append(
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [[{
                        "type": "link",
                        "text": "✉️ Связаться с клиентом",
                        "url": contact_url,
                    }]]
                },
            }
        )
    await max_client.send_message(
        target.recipient_id,
        message,
        attachments,
        recipient_type=target.recipient_type,
    )


def telegram_contact_reply_markup(
    order: BotOrder,
    user: BotUser | None,
) -> dict[str, list[list[dict[str, str]]]] | None:
    url = telegram_user_url(order, user)
    if not url:
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✉️ Написать пользователю",
                    "url": url,
                }
            ]
        ]
    }


def universal_telegram_contact_reply_markup(
    order: BotOrder,
    user: BotUser | None,
    settings: Settings,
) -> dict[str, list[list[dict[str, str]]]] | None:
    url = universal_contact_url(order, user, settings)
    if not url:
        return None
    return {
        "inline_keyboard": [[{"text": "✉️ Связаться с клиентом", "url": url}]]
    }


def universal_contact_url(
    order: BotOrder,
    user: BotUser | None,
    settings: Settings,
) -> str | None:
    if user is not None and settings.admin_base_url:
        return f"{settings.admin_base_url.rstrip('/')}/chats/{user.id}?order={order.id}"
    if settings.admin_base_url:
        return f"{settings.admin_base_url.rstrip('/')}/orders/{order.id}"
    return telegram_user_url(order, user)


def telegram_user_url(order: BotOrder, user: BotUser | None) -> str | None:
    if order.platform != "telegram":
        return None

    username = value_from_user(user, "username")
    if username:
        return f"https://t.me/{username.lstrip('@')}"

    telegram_user_id = order.platform_user_id or order.telegram_user_id
    if telegram_user_id:
        return f"tg://user?id={telegram_user_id}"
    return None
