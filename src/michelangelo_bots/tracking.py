import logging
from dataclasses import dataclass
from typing import Any

from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.db import BotEvent, BotUser, datetime_now, get_session_factory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserSnapshot:
    platform: str
    platform_user_id: str
    chat_id: str | None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    language_code: str | None = None
    is_bot: bool | None = None
    raw_profile: dict[str, Any] | None = None


async def track_interaction(
    *,
    user: UserSnapshot,
    action: str,
    event_type: str,
    message_text: str | None = None,
    callback_payload: str | None = None,
    raw_update: dict[str, Any] | None = None,
) -> None:
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            db_user = await upsert_user(
                session,
                user=user,
                action=action,
                message_text=message_text,
            )
            session.add(
                BotEvent(
                    user_id=db_user.id,
                    platform=user.platform,
                    chat_id=user.chat_id,
                    action=action,
                    event_type=event_type,
                    message_text=message_text,
                    callback_payload=callback_payload,
                    raw_update=raw_update,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to track bot interaction")


async def upsert_user(
    session: AsyncSession,
    *,
    user: UserSnapshot,
    action: str,
    message_text: str | None,
) -> BotUser:
    now = datetime_now()
    statement = (
        insert(BotUser)
        .values(
            platform=user.platform,
            platform_user_id=user.platform_user_id,
            chat_id=user.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            full_name=user.full_name,
            language_code=user.language_code,
            is_bot=user.is_bot,
            raw_profile=user.raw_profile,
            first_seen_at=now,
            last_seen_at=now,
            total_actions=1,
            last_action=action,
            last_message_text=message_text,
        )
        .on_conflict_do_update(
            index_elements=[BotUser.platform, BotUser.platform_user_id],
            set_={
                "chat_id": user.chat_id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "full_name": user.full_name,
                "language_code": user.language_code,
                "is_bot": user.is_bot,
                "raw_profile": user.raw_profile,
                "last_seen_at": now,
                "total_actions": BotUser.total_actions + 1,
                "last_action": action,
                "last_message_text": message_text,
            },
        )
        .returning(BotUser)
    )
    result = await session.execute(statement)
    return result.scalar_one()


def telegram_user_snapshot(message_or_callback: Message | CallbackQuery) -> UserSnapshot | None:
    tg_user = message_or_callback.from_user
    if tg_user is None:
        return None

    chat_id: str | None = None
    if isinstance(message_or_callback, Message) and message_or_callback.chat:
        chat_id = str(message_or_callback.chat.id)
    elif isinstance(message_or_callback, CallbackQuery) and message_or_callback.message:
        chat = getattr(message_or_callback.message, "chat", None)
        chat_id = str(chat.id) if chat else None

    first_name = tg_user.first_name
    last_name = tg_user.last_name
    full_name = " ".join(part for part in [first_name, last_name] if part) or None
    return UserSnapshot(
        platform="telegram",
        platform_user_id=str(tg_user.id),
        chat_id=chat_id,
        username=tg_user.username,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        language_code=tg_user.language_code,
        is_bot=tg_user.is_bot,
        raw_profile=tg_user.model_dump(mode="json"),
    )


async def track_telegram_message(message: Message, *, action: str) -> None:
    user = telegram_user_snapshot(message)
    if user is None:
        return
    await track_interaction(
        user=user,
        action=action,
        event_type="message",
        message_text=message.text,
        raw_update=message.model_dump(mode="json"),
    )


async def track_telegram_callback(callback: CallbackQuery, *, action: str) -> None:
    user = telegram_user_snapshot(callback)
    if user is None:
        return
    await track_interaction(
        user=user,
        action=action,
        event_type="callback",
        callback_payload=callback.data,
        raw_update=callback.model_dump(mode="json"),
    )


async def find_user(session: AsyncSession, user_id: int) -> BotUser | None:
    result = await session.execute(select(BotUser).where(BotUser.id == user_id))
    return result.scalar_one_or_none()
