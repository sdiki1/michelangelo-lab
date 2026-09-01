"""Диалог администратора с клиентом.

Входящие сообщения пользователя уже пишутся ботами в ``bot_events``; исходящие
сообщения администратора хранятся в ``chat_messages``. Лента диалога — merge
этих двух источников по времени, поэтому история, накопленная до появления
чата в админке, остаётся видимой.

Медиа в ленте не дублируется на диск: входящие фото/видео Telegram отдаются
прокси-эндпоинтом админки по ``file_id``, входящие медиа MAX — по прямым
ссылкам из апдейта, исходящие — из ``uploads_dir`` админки.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotEvent, BotUser, ChatMessage, datetime_now
from michelangelo_bots.max_bot import MaxClient, extract_media

logger = logging.getLogger(__name__)

INCOMING = "in"
OUTGOING = "out"

PHOTO = "photo"
VIDEO = "video"
FILE = "file"

# Telegram обрезает подпись к медиа на 1024 символах — длинный текст уходит отдельно.
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_MEDIA_GROUP_LIMIT = 10


class ChatDeliveryError(RuntimeError):
    """Сообщение не удалось доставить в мессенджер."""


@dataclass(frozen=True)
class ChatAttachment:
    """Фото/видео реплики: ``url`` уже готов для вставки в разметку админки."""

    kind: str
    url: str
    name: str | None = None


@dataclass(frozen=True)
class ChatItem:
    """Одна реплика в ленте диалога."""

    at: datetime
    direction: str
    text: str
    status: str | None = None
    error: str | None = None
    author: str | None = None
    attachments: tuple[ChatAttachment, ...] = ()


def merge_thread(
    events: list[BotEvent],
    messages: list[ChatMessage],
    *,
    limit: int | None = None,
) -> list[ChatItem]:
    """Собирает ленту диалога: входящие из bot_events, исходящие из chat_messages."""

    items: list[ChatItem] = []
    for event in events:
        attachments = incoming_attachments(event)
        if not event.message_text and not attachments:
            continue
        items.append(
            ChatItem(
                at=event.occurred_at,
                direction=INCOMING,
                text=event.message_text or "",
                attachments=tuple(attachments),
            )
        )
    items += [
        ChatItem(
            at=message.created_at,
            direction=message.direction,
            text=message.text or "",
            status=message.status,
            error=message.error,
            author=message.author,
            attachments=tuple(outgoing_attachments(message)),
        )
        for message in messages
    ]
    items.sort(key=lambda item: item.at)
    if limit is not None and len(items) > limit:
        items = items[-limit:]
    return items


def outgoing_attachments(message: ChatMessage) -> list[ChatAttachment]:
    """Файлы, отправленные администратором: отдаются админкой из uploads_dir."""

    stored = message.attachments or []
    return [
        ChatAttachment(
            kind=item.get("kind") or FILE,
            url=f"/media/chat/{message.id}/{index}",
            name=item.get("filename"),
        )
        for index, item in enumerate(stored)
    ]


def incoming_attachments(event: BotEvent) -> list[ChatAttachment]:
    """Медиа входящего сообщения, восстановленное из сохранённого апдейта."""

    raw = event.raw_update
    if not isinstance(raw, dict):
        return []
    if event.platform == "telegram":
        return telegram_attachments(raw)
    if event.platform == "max":
        return max_attachments(raw)
    return []


def telegram_attachments(raw_update: dict[str, Any]) -> list[ChatAttachment]:
    attachments: list[ChatAttachment] = []

    photo_sizes = raw_update.get("photo")
    if isinstance(photo_sizes, list) and photo_sizes:
        largest = photo_sizes[-1]
        if isinstance(largest, dict) and largest.get("file_id"):
            attachments.append(telegram_attachment(PHOTO, largest))

    for key, kind in (("video", VIDEO), ("animation", VIDEO), ("video_note", VIDEO)):
        value = raw_update.get(key)
        if isinstance(value, dict) and value.get("file_id"):
            attachments.append(telegram_attachment(kind, value))

    document = raw_update.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        mime = str(document.get("mime_type") or "")
        if mime.startswith("image/"):
            attachments.append(telegram_attachment(PHOTO, document))
        elif mime.startswith("video/"):
            attachments.append(telegram_attachment(VIDEO, document))

    return attachments


def telegram_attachment(kind: str, payload: dict[str, Any]) -> ChatAttachment:
    file_id = str(payload["file_id"])
    return ChatAttachment(
        kind=kind,
        url=f"/media/telegram/{quote(file_id, safe='')}",
        name=payload.get("file_name"),
    )


def max_attachments(raw_update: dict[str, Any]) -> list[ChatAttachment]:
    message = raw_update.get("message")
    if not isinstance(message, dict):
        return []
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    attachments = body.get("attachments") or message.get("attachments") or []
    return [
        ChatAttachment(kind=item["kind"], url=item["url"]) for item in extract_media(attachments)
    ]


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
                .where(BotEvent.user_id == user.id, BotEvent.event_type == "message")
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
    attachments: list[dict[str, str]] | None = None,
) -> ChatMessage:
    """Отправляет сообщение клиенту и всегда сохраняет попытку в историю."""

    media = attachments or []
    message = ChatMessage(
        user_id=user.id,
        direction=OUTGOING,
        platform=user.platform,
        chat_id=recipient_id(user),
        text=text,
        attachments=media or None,
        author=author,
        order_id=order_id,
        status="sent",
        created_at=datetime_now(),
    )
    try:
        await deliver_message(user, text, settings, attachments=media)
    except Exception as exc:
        message.status = "failed"
        message.error = str(exc)[:1000]
        logger.exception("Admin chat delivery failed for user_id=%s", user.id)

    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def deliver_message(
    user: BotUser,
    text: str,
    settings: Settings,
    *,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    target = recipient_id(user)
    if not target:
        raise ChatDeliveryError("У пользователя нет chat_id — написать первым нельзя")

    media = attachments or []
    if user.platform == "telegram":
        await deliver_telegram_message(target, text, settings, media)
        return
    if user.platform == "max":
        await deliver_max_message(user, target, text, settings, media)
        return
    raise ChatDeliveryError(f"Платформа {user.platform!r} не поддерживает отправку сообщений")


async def read_attachment(media_file: dict[str, str]) -> tuple[str, bytes, str]:
    path = Path(media_file["path"])
    content = await asyncio.to_thread(path.read_bytes)
    return (
        media_file.get("filename") or path.name,
        content,
        media_file.get("content_type") or "application/octet-stream",
    )


async def deliver_telegram_message(
    chat_id: str,
    text: str,
    settings: Settings,
    attachments: list[dict[str, str]],
) -> None:
    if not settings.telegram_bot_token:
        raise ChatDeliveryError("TELEGRAM_BOT_TOKEN is not set")

    async with httpx.AsyncClient(timeout=120) as http_client:
        if not attachments:
            await telegram_call(
                http_client, settings, "sendMessage", {"chat_id": chat_id, "text": text}
            )
            return

        # Подпись помещается в медиа, длинный текст уходит отдельным сообщением.
        caption = text if len(text) <= TELEGRAM_CAPTION_LIMIT else ""
        if text and not caption:
            await telegram_call(
                http_client, settings, "sendMessage", {"chat_id": chat_id, "text": text}
            )

        if len(attachments) == 1:
            await send_telegram_single_media(
                http_client, settings, chat_id, caption, attachments[0]
            )
            return
        await send_telegram_media_group(http_client, settings, chat_id, caption, attachments)


async def send_telegram_single_media(
    http_client: httpx.AsyncClient,
    settings: Settings,
    chat_id: str,
    caption: str,
    media_file: dict[str, str],
) -> None:
    is_video = media_file.get("kind") == VIDEO
    method = "sendVideo" if is_video else "sendPhoto"
    field_name = VIDEO if is_video else PHOTO
    filename, content, content_type = await read_attachment(media_file)
    await telegram_call(
        http_client,
        settings,
        method,
        {"chat_id": chat_id, "caption": caption},
        files={field_name: (filename, content, content_type)},
    )


async def send_telegram_media_group(
    http_client: httpx.AsyncClient,
    settings: Settings,
    chat_id: str,
    caption: str,
    attachments: list[dict[str, str]],
) -> None:
    media_payload: list[dict[str, Any]] = []
    files_payload: dict[str, tuple[str, bytes, str]] = {}
    for index, media_file in enumerate(attachments[:TELEGRAM_MEDIA_GROUP_LIMIT]):
        attach_name = f"file{index}"
        filename, content, content_type = await read_attachment(media_file)
        item: dict[str, Any] = {
            "type": VIDEO if media_file.get("kind") == VIDEO else PHOTO,
            "media": f"attach://{attach_name}",
        }
        if index == 0 and caption:
            item["caption"] = caption
        media_payload.append(item)
        files_payload[attach_name] = (filename, content, content_type)

    await telegram_call(
        http_client,
        settings,
        "sendMediaGroup",
        {"chat_id": chat_id, "media": json.dumps(media_payload)},
        files=files_payload,
    )


async def telegram_call(
    http_client: httpx.AsyncClient,
    settings: Settings,
    method: str,
    data: dict[str, Any],
    *,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    if files:
        response = await http_client.post(url, data=data, files=files)
    else:
        response = await http_client.post(url, json=data)
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
    attachments: list[dict[str, str]],
) -> None:
    if not settings.max_bot_token:
        raise ChatDeliveryError("MAX_BOT_TOKEN is not set")

    recipient_type = "chat_id" if user.chat_id else "user_id"
    async with httpx.AsyncClient(
        base_url=str(settings.max_api_base_url).rstrip("/"),
        timeout=120,
    ) as http_client:
        client = MaxClient(
            token=settings.max_bot_token,
            base_url=str(settings.max_api_base_url),
            http_client=http_client,
        )
        uploaded = [await upload_max_attachment(client, media) for media in attachments]
        await send_max_with_attachments(client, target, text, uploaded, recipient_type)


# Загруженное видео MAX обрабатывает асинхронно и до готовности отвечает
# attachment.not.ready — ждём и повторяем, иначе отправка падает без причины.
MAX_ATTACHMENT_RETRIES = 5
MAX_ATTACHMENT_RETRY_DELAY_SECONDS = 3.0


async def send_max_with_attachments(
    client: MaxClient,
    target: str,
    text: str,
    attachments: list[dict[str, Any]],
    recipient_type: str,
) -> None:
    for attempt in range(MAX_ATTACHMENT_RETRIES):
        try:
            await client.send_message(target, text, attachments, recipient_type=recipient_type)
            return
        except httpx.HTTPStatusError as exc:
            last_attempt = attempt == MAX_ATTACHMENT_RETRIES - 1
            if last_attempt or "not.ready" not in exc.response.text:
                raise ChatDeliveryError(
                    f"MAX API HTTP {exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            await asyncio.sleep(MAX_ATTACHMENT_RETRY_DELAY_SECONDS)


async def upload_max_attachment(
    client: MaxClient,
    media_file: dict[str, str],
) -> dict[str, Any]:
    filename, content, content_type = await read_attachment(media_file)
    try:
        return await client.upload_attachment(
            kind=media_file.get("kind") or PHOTO,
            filename=filename,
            content=content,
            content_type=content_type,
        )
    except httpx.HTTPStatusError as exc:
        raise ChatDeliveryError(
            f"MAX не принял файл {filename}: HTTP {exc.response.status_code} "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ChatDeliveryError(f"MAX не принял файл {filename}: {exc}") from exc
