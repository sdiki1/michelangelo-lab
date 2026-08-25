"""Диалог администратора с клиентом.

Входящие сообщения пользователя уже пишутся ботами в ``bot_events``; исходящие
сообщения администратора хранятся в ``chat_messages``. Лента диалога — merge
этих двух источников по времени, поэтому история, накопленная до появления
чата в админке, остаётся видимой.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotEvent, BotUser, ChatMessage, datetime_now
from michelangelo_bots.max_bot import MaxClient

logger = logging.getLogger(__name__)

INCOMING = "in"
OUTGOING = "out"


class ChatDeliveryError(RuntimeError):
    """Сообщение не удалось доставить в мессенджер."""


@dataclass(frozen=True)
class ChatItem:
    """Одна реплика в ленте диалога."""

    at: datetime
    direction: str
    text: str
    status: str | None = None
    error: str | None = None
    author: str | None = None


def merge_thread(
    events: list[BotEvent],
    messages: list[ChatMessage],
    *,
    limit: int | None = None,
) -> list[ChatItem]:
    """Собирает ленту диалога: входящие из bot_events, исходящие из chat_messages."""

    items = [
        ChatItem(at=event.occurred_at, direction=INCOMING, text=event.message_text or "")
        for event in events
        if event.message_text
    ]
    items += [
        ChatItem(
            at=message.created_at,
            direction=message.direction,
            text=message.text,
            status=message.status,
            error=message.error,
            author=message.author,
        )
        for message in messages
    ]
    items.sort(key=lambda item: item.at)
    if limit is not None and len(items) > limit:
        items = items[-limit:]
    return items


async def load_thread(
    session: AsyncSession,
    user: BotUser,
    *,
    limit: int = 200,
) -> list[ChatItem]:
    events = list(
        (
            await session.execute(
                select(BotEvent)
                .where(BotEvent.user_id == user.id, BotEvent.message_text.is_not(None))
                .order_by(desc(BotEvent.occurred_at))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    messages = list(
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.user_id == user.id)
                .order_by(desc(ChatMessage.created_at))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return merge_thread(events, messages, limit=limit)


def recipient_id(user: BotUser) -> str | None:
    return user.chat_id or user.platform_user_id


async def send_admin_message(
    session: AsyncSession,
    *,
    user: BotUser,
    text: str,
    settings: Settings,
    author: str | None = None,
    order_id: int | None = None,
) -> ChatMessage:
    """Отправляет сообщение клиенту и всегда сохраняет попытку в историю."""

    message = ChatMessage(
        user_id=user.id,
        direction=OUTGOING,
        platform=user.platform,
        chat_id=recipient_id(user),
        text=text,
        author=author,
        order_id=order_id,
        status="sent",
        created_at=datetime_now(),
    )
    try:
        await deliver_message(user, text, settings)
    except Exception as exc:
        message.status = "failed"
        message.error = str(exc)[:1000]
        logger.exception("Admin chat delivery failed for user_id=%s", user.id)

    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def deliver_message(user: BotUser, text: str, settings: Settings) -> None:
    target = recipient_id(user)
    if not target:
        raise ChatDeliveryError("У пользователя нет chat_id — написать первым нельзя")

    if user.platform == "telegram":
        await deliver_telegram_message(target, text, settings)
        return
    if user.platform == "max":
        await deliver_max_message(user, target, text, settings)
        return
    raise ChatDeliveryError(f"Платформа {user.platform!r} не поддерживает отправку сообщений")


async def deliver_telegram_message(chat_id: str, text: str, settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise ChatDeliveryError("TELEGRAM_BOT_TOKEN is not set")

    async with httpx.AsyncClient(timeout=30) as http_client:
        response = await http_client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    if response.is_error:
        raise ChatDeliveryError(f"Telegram API HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    if not payload.get("ok"):
        raise ChatDeliveryError(f"Telegram API rejected message: {payload}")


async def deliver_max_message(
    user: BotUser,
    target: str,
    text: str,
    settings: Settings,
) -> None:
    if not settings.max_bot_token:
        raise ChatDeliveryError("MAX_BOT_TOKEN is not set")

    recipient_type = "chat_id" if user.chat_id else "user_id"
    async with httpx.AsyncClient(
        base_url=str(settings.max_api_base_url).rstrip("/"),
        timeout=30,
    ) as http_client:
        client = MaxClient(
            token=settings.max_bot_token,
            base_url=str(settings.max_api_base_url),
            http_client=http_client,
        )
        await client.send_message(target, text, [], recipient_type=recipient_type)
