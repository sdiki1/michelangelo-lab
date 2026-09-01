from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx
from aiogram.types import Message

from michelangelo_bots.bot_configuration import DEFAULT_SETTINGS, render_template
from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotSetting, BotUser
from michelangelo_bots.max_bot import MaxClient
from michelangelo_bots.order_notifications import admin_targets

logger = logging.getLogger(__name__)


async def notify_admins_about_telegram_message(
    message: Message,
    user: BotUser | None,
    settings: Settings,
) -> None:
    text = message.text or message.caption or incoming_media_placeholder(message)
    await notify_admins_about_incoming(
        settings=settings,
        source="telegram",
        user=user,
        message=text,
        telegram_message=message,
    )


async def notify_admins_about_incoming(
    *,
    settings: Settings,
    source: str,
    user: BotUser | None,
    message: str,
    photo_urls: list[str] | None = None,
    telegram_message: Message | None = None,
) -> None:
    targets = admin_targets(settings)
    if not targets:
        return

    template = await configured_template("admin_incoming_template")
    source_title = {"telegram": "Telegram", "max": "MAX", "site": "Сайт"}.get(
        source, source
    )
    notification = render_template(
        template,
        {
            "source": source_title,
            "customer_name": user.full_name if user else None,
            "username": f"@{user.username.lstrip('@')}" if user and user.username else None,
            "platform_user_id": user.platform_user_id if user else None,
            "message": message,
        },
    )
    contact_url = contact_link(settings, user)

    async with httpx.AsyncClient(
        base_url=str(settings.max_api_base_url).rstrip("/"), timeout=40
    ) as client:
        resolved_photo_urls = list(photo_urls or [])
        if telegram_message and telegram_message.photo:
            telegram_photo_url = await telegram_file_url(
                client, settings, telegram_message.photo[-1].file_id
            )
            if telegram_photo_url:
                resolved_photo_urls.append(telegram_photo_url)
        max_client = MaxClient(
            settings.max_bot_token,
            str(settings.max_api_base_url),
            http_client=client,
        )
        for target in targets:
            try:
                if target.platform == "telegram":
                    await send_telegram_admin_message(
                        client,
                        settings,
                        target.recipient_id,
                        notification,
                        contact_url,
                    )
                    if telegram_message and has_media(telegram_message):
                        await copy_telegram_message(
                            client, settings, telegram_message, target.recipient_id
                        )
                    for photo_url in photo_urls or []:
                        await send_telegram_photo(
                            client, settings, target.recipient_id, photo_url
                        )
                else:
                    attachments: list[dict[str, Any]] = []
                    if contact_url:
                        attachments.append(link_keyboard(contact_url))
                    await max_client.send_message(
                        target.recipient_id,
                        notification,
                        attachments,
                        recipient_type=target.recipient_type,
                    )
                    for photo_url in resolved_photo_urls:
                        await max_client.send_message(
                            target.recipient_id,
                            "📷 Фото клиента",
                            [{"type": "image", "payload": {"url": photo_url}}],
                            recipient_type=target.recipient_type,
                        )
            except Exception:
                logger.exception("Could not forward incoming message to %s", target.key)


async def configured_template(key: str) -> str:
    # Notification handlers are called from bot processes; use the shared session lazily
    # to avoid coupling them to the request session that may already be committing an event.
    from michelangelo_bots.db import get_session_factory

    async with get_session_factory()() as session:
        row = await session.get(BotSetting, key)
        return row.value if row else DEFAULT_SETTINGS[key]


def contact_link(settings: Settings, user: BotUser | None) -> str | None:
    if user is None:
        return None
    reply_url = (user.raw_profile or {}).get("reply_url")
    if isinstance(reply_url, str) and reply_url.startswith(("https://", "http://")):
        return reply_url
    if settings.admin_base_url:
        return f"{settings.admin_base_url.rstrip('/')}/chats/{user.id}"
    if user.platform == "telegram":
        if user.username:
            return f"https://t.me/{quote(user.username.lstrip('@'))}"
        return f"tg://user?id={quote(user.platform_user_id)}"
    return None


def link_keyboard(url: str) -> dict[str, Any]:
    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [[{"type": "link", "text": "✉️ Ответить клиенту", "url": url}]]
        },
    }


async def send_telegram_admin_message(
    client: httpx.AsyncClient,
    settings: Settings,
    chat_id: str,
    text: str,
    contact_url: str | None,
) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if contact_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "✉️ Ответить клиенту", "url": contact_url}]]
        }
    response = await client.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json=payload,
    )
    response.raise_for_status()


def has_media(message: Message) -> bool:
    return bool(
        message.photo or message.video or message.animation or message.video_note
        or message.document
    )


def incoming_media_placeholder(message: Message) -> str:
    if message.photo:
        return "📷 Клиент прислал фотографию"
    if message.video or message.animation or message.video_note:
        return "🎬 Клиент прислал видео"
    if message.document:
        return "📎 Клиент прислал файл"
    return "Клиент прислал сообщение"


async def copy_telegram_message(
    client: httpx.AsyncClient,
    settings: Settings,
    message: Message,
    target_chat_id: str,
) -> None:
    response = await client.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/copyMessage",
        json={
            "chat_id": target_chat_id,
            "from_chat_id": message.chat.id,
            "message_id": message.message_id,
        },
    )
    response.raise_for_status()


async def send_telegram_photo(
    client: httpx.AsyncClient,
    settings: Settings,
    chat_id: str,
    photo_url: str,
) -> None:
    response = await client.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto",
        json={"chat_id": chat_id, "photo": photo_url},
    )
    response.raise_for_status()


async def telegram_file_url(
    client: httpx.AsyncClient,
    settings: Settings,
    file_id: str,
) -> str | None:
    response = await client.get(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile",
        params={"file_id": file_id},
    )
    response.raise_for_status()
    data = response.json()
    file_path = (data.get("result") or {}).get("file_path")
    if not file_path:
        return None
    return f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
