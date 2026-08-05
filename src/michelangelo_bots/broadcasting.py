import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotBroadcast, BotUser
from michelangelo_bots.max_bot import MaxClient

logger = logging.getLogger(__name__)


async def send_broadcast(
    session: AsyncSession,
    *,
    broadcast: BotBroadcast,
    settings: Settings,
) -> BotBroadcast:
    result = await session.execute(
        select(BotUser).where(BotUser.chat_id.is_not(None)).order_by(BotUser.id)
    )
    users = result.scalars().all()

    broadcast.status = "sending"
    broadcast.total_recipients = len(users)
    broadcast.success_count = 0
    broadcast.failed_count = 0
    broadcast.last_error = None
    await session.flush()

    async with httpx.AsyncClient(timeout=30) as http_client:
        max_client = MaxClient(
            token=settings.max_bot_token,
            base_url=str(settings.max_api_base_url),
            http_client=http_client,
        )
        for user in users:
            try:
                if user.platform == "telegram":
                    await send_telegram_broadcast(http_client, settings, broadcast, user)
                elif user.platform == "max":
                    await send_max_broadcast(max_client, settings, broadcast, user)
                else:
                    continue
                broadcast.success_count += 1
            except Exception as exc:
                broadcast.failed_count += 1
                broadcast.last_error = str(exc)
                logger.exception("Broadcast delivery failed for user_id=%s", user.id)

    broadcast.sent_at = datetime.now(UTC)
    broadcast.status = "sent" if broadcast.failed_count == 0 else "partial_failed"
    await session.commit()
    await session.refresh(broadcast)
    return broadcast


async def send_telegram_broadcast(
    http_client: httpx.AsyncClient,
    settings: Settings,
    broadcast: BotBroadcast,
    user: BotUser,
) -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    if broadcast.media_files:
        await send_telegram_uploaded_media(http_client, settings, broadcast, user)
        return

    method = "sendMessage"
    payload: dict[str, Any] = {"chat_id": user.chat_id}
    if broadcast.media_type == "photo" and broadcast.media_url:
        method = "sendPhoto"
        payload["photo"] = broadcast.media_url
        payload["caption"] = broadcast.text
    elif broadcast.media_type == "video" and broadcast.media_url:
        method = "sendVideo"
        payload["video"] = broadcast.media_url
        payload["caption"] = broadcast.text
    else:
        payload["text"] = broadcast.text

    if broadcast.include_miniapp_button:
        payload["reply_markup"] = telegram_miniapp_keyboard(str(settings.telegram_miniapp_url))

    response = await http_client.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
        json=payload,
    )
    response.raise_for_status()


async def send_max_broadcast(
    max_client: MaxClient,
    settings: Settings,
    broadcast: BotBroadcast,
    user: BotUser,
) -> None:
    if not settings.max_bot_token:
        raise RuntimeError("MAX_BOT_TOKEN is not set")

    text = broadcast.text
    if broadcast.media_type and broadcast.media_url:
        text = f"{text}\n\n{broadcast.media_type}: {broadcast.media_url}"
    if broadcast.media_files:
        file_names = ", ".join(item.get("filename", "") for item in broadcast.media_files)
        text = f"{text}\n\nФайлы в рассылке: {file_names}"

    attachments = []
    if broadcast.include_miniapp_button:
        attachments = max_miniapp_keyboard(str(settings.max_miniapp_url))

    await max_client.send_message(user.chat_id or user.platform_user_id, text, attachments)


def telegram_miniapp_keyboard(miniapp_url: str) -> dict[str, Any]:
    button: dict[str, Any] = {"text": "Миниапп"}
    if is_telegram_direct_link(miniapp_url):
        button["url"] = miniapp_url
    else:
        button["web_app"] = {"url": miniapp_url}
    return {"inline_keyboard": [[button]]}


def is_telegram_direct_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"t.me", "telegram.me"}


async def send_telegram_uploaded_media(
    http_client: httpx.AsyncClient,
    settings: Settings,
    broadcast: BotBroadcast,
    user: BotUser,
) -> None:
    media_files = broadcast.media_files or []
    if len(media_files) == 1:
        await send_telegram_single_uploaded_media(
            http_client,
            settings,
            broadcast,
            user,
            media_files[0],
        )
        return

    await send_telegram_uploaded_media_group(http_client, settings, broadcast, user, media_files)

    if broadcast.include_miniapp_button:
        response = await http_client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": user.chat_id,
                "text": "Открыть миниапп",
                "reply_markup": telegram_miniapp_keyboard(str(settings.telegram_miniapp_url)),
            },
        )
        response.raise_for_status()


async def send_telegram_single_uploaded_media(
    http_client: httpx.AsyncClient,
    settings: Settings,
    broadcast: BotBroadcast,
    user: BotUser,
    media_file: dict[str, str],
) -> None:
    field_name = "video" if media_file.get("kind") == "video" else "photo"
    method = "sendVideo" if field_name == "video" else "sendPhoto"
    path = Path(media_file["path"])
    content = await asyncio.to_thread(path.read_bytes)
    data = {
        "chat_id": str(user.chat_id),
        "caption": broadcast.text,
    }
    if broadcast.include_miniapp_button:
        data["reply_markup"] = json.dumps(
            telegram_miniapp_keyboard(str(settings.telegram_miniapp_url))
        )
    response = await http_client.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
        data=data,
        files={
            field_name: (
                media_file.get("filename") or path.name,
                content,
                media_file.get("content_type") or "application/octet-stream",
            )
        },
    )
    response.raise_for_status()


async def send_telegram_uploaded_media_group(
    http_client: httpx.AsyncClient,
    settings: Settings,
    broadcast: BotBroadcast,
    user: BotUser,
    media_files: list[dict[str, str]],
) -> None:
    media_payload: list[dict[str, Any]] = []
    files_payload: dict[str, tuple[str, Any, str]] = {}

    for index, media_file in enumerate(media_files[:10]):
        attach_name = f"file{index}"
        path = Path(media_file["path"])
        content = await asyncio.to_thread(path.read_bytes)
        kind = "video" if media_file.get("kind") == "video" else "photo"
        media_item: dict[str, Any] = {"type": kind, "media": f"attach://{attach_name}"}
        if index == 0:
            media_item["caption"] = broadcast.text
        media_payload.append(media_item)
        files_payload[attach_name] = (
            media_file.get("filename") or path.name,
            content,
            media_file.get("content_type") or "application/octet-stream",
        )

    response = await http_client.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMediaGroup",
        data={"chat_id": str(user.chat_id), "media": json.dumps(media_payload)},
        files=files_payload,
    )
    response.raise_for_status()


def max_miniapp_keyboard(miniapp_url: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {"buttons": [[{"type": "link", "text": "Миниапп", "url": miniapp_url}]]},
        }
    ]
