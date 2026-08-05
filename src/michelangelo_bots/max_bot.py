import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from michelangelo_bots.config import Settings, get_settings
from michelangelo_bots.content import (
    Action,
    MenuButton,
    back_to_main_buttons,
    main_menu_buttons,
    render_start_text,
    text_for_action,
)
from michelangelo_bots.db import init_db
from michelangelo_bots.tracking import UserSnapshot, track_interaction

logger = logging.getLogger(__name__)
DEFAULT_RATE_LIMIT_SLEEP_SECONDS = 60.0
MAX_RATE_LIMIT_SLEEP_SECONDS = 300.0


def max_keyboard(buttons: Sequence[MenuButton]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for button in buttons:
        if button.url:
            rows.append([{"type": "link", "text": button.title, "url": button.url}])
        elif button.action:
            rows.append(
                [{"type": "callback", "text": button.title, "payload": button.action.value}]
            )
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int | str | None
    user_id: int | str | None = None
    text: str | None = None
    payload: str | None = None
    callback_id: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    raw_user: dict[str, Any] | None = None


class MaxClient:
    """Minimal MAX/TamTam-compatible Bot API client."""

    def __init__(self, token: str, base_url: str, http_client: httpx.AsyncClient | None = None):
        self._token = token
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=40)

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": self._token}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_updates(
        self,
        *,
        marker: int | str | None,
        timeout_seconds: int,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_seconds, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        response = await self._client.get("/updates", params=params, headers=self._auth_headers)
        response.raise_for_status()
        return response.json()

    async def get_me(self) -> dict[str, Any]:
        response = await self._client.get("/me", headers=self._auth_headers)
        response.raise_for_status()
        return response.json()

    async def send_message(
        self,
        recipient_id: int | str,
        text: str,
        attachments: list[dict[str, Any]],
        *,
        recipient_type: str = "chat_id",
    ) -> None:
        response = await self._client.post(
            "/messages",
            params={recipient_type: recipient_id},
            headers=self._auth_headers,
            json={"text": text, "attachments": attachments},
        )
        response.raise_for_status()

    async def answer_callback(self, callback_id: str) -> None:
        response = await self._client.post(
            "/answers",
            params={"callback_id": callback_id},
            headers=self._auth_headers,
            json={},
        )
        response.raise_for_status()


class MaxBot:
    def __init__(self, client: MaxClient, settings: Settings):
        self._client = client
        self._settings = settings

    async def handle_update(self, update: dict[str, Any]) -> None:
        incoming = parse_update(update)
        if incoming is None:
            logger.info("Skipping unsupported MAX update type=%s", update.get("update_type"))
            return

        action = action_from_incoming(incoming)
        await track_max_interaction(incoming, action=action.value, raw_update=update)
        keyboard = (
            max_keyboard(main_menu_buttons(str(self._settings.max_miniapp_url)))
            if action is Action.MAIN_MENU
            else max_keyboard(back_to_main_buttons())
        )
        recipient_id = outgoing_recipient_id(incoming)
        recipient_type = outgoing_recipient_type(incoming)
        if recipient_id is None:
            logger.warning("Cannot answer MAX update without chat_id or user_id: %s", update)
            return

        await self._client.send_message(
            recipient_id,
            text_for_incoming(action, incoming),
            attachments=keyboard,
            recipient_type=recipient_type,
        )

        if incoming.callback_id:
            await self._client.answer_callback(incoming.callback_id)

    async def polling(self) -> None:
        marker: int | str | None = None
        while True:
            try:
                data = await self._client.get_updates(
                    marker=marker,
                    timeout_seconds=self._settings.max_poll_timeout_seconds,
                )
                marker = data.get("marker", marker)
                for update in data.get("updates", []):
                    await self.handle_update(update)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    retry_after = retry_after_seconds(exc.response)
                    logger.warning("MAX rate limit reached, sleeping %.1f seconds", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                logger.exception("MAX polling iteration failed")
                await asyncio.sleep(3)
            except Exception:
                logger.exception("MAX polling iteration failed")
                await asyncio.sleep(3)


def action_from_incoming(incoming: IncomingMessage) -> Action:
    if incoming.payload:
        try:
            return Action(incoming.payload)
        except ValueError:
            return Action.MAIN_MENU

    if (incoming.text or "").strip().lower() == "/start":
        return Action.MAIN_MENU

    return Action.MAIN_MENU


def outgoing_recipient_id(incoming: IncomingMessage) -> int | str | None:
    return incoming.chat_id or incoming.user_id


def outgoing_recipient_type(incoming: IncomingMessage) -> str:
    return "chat_id" if incoming.chat_id is not None else "user_id"


def text_for_incoming(action: Action, incoming: IncomingMessage) -> str:
    if action is Action.MAIN_MENU:
        return render_start_text(max_display_name(incoming))
    return text_for_action(action)


def retry_after_seconds(response: httpx.Response) -> float:
    header_value = response.headers.get("Retry-After")
    if not header_value:
        return DEFAULT_RATE_LIMIT_SLEEP_SECONDS

    try:
        seconds = float(header_value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header_value)
        except (TypeError, ValueError):
            return DEFAULT_RATE_LIMIT_SLEEP_SECONDS
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()

    return min(max(seconds, 1.0), MAX_RATE_LIMIT_SLEEP_SECONDS)


def max_display_name(incoming: IncomingMessage) -> str | None:
    full_name = " ".join(part for part in [incoming.first_name, incoming.last_name] if part)
    return full_name or incoming.username


async def track_max_interaction(
    incoming: IncomingMessage,
    *,
    action: str,
    raw_update: dict[str, Any],
) -> None:
    platform_user_id = str(incoming.user_id or incoming.chat_id)
    full_name = " ".join(part for part in [incoming.first_name, incoming.last_name] if part) or None
    await track_interaction(
        user=UserSnapshot(
            platform="max",
            platform_user_id=platform_user_id,
            chat_id=str(incoming.chat_id) if incoming.chat_id is not None else None,
            username=incoming.username,
            first_name=incoming.first_name,
            last_name=incoming.last_name,
            full_name=full_name,
            raw_profile=incoming.raw_user,
        ),
        action=action,
        event_type="callback" if incoming.callback_id else "message",
        message_text=incoming.text,
        callback_payload=incoming.payload,
        raw_update=raw_update,
    )


def parse_update(update: dict[str, Any]) -> IncomingMessage | None:
    callback = update.get("callback") or update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat_id = extract_chat_id(message) or extract_chat_id(callback) or extract_chat_id(update)
        user = extract_user(callback) or extract_user(message) or extract_user(update)
        user_id = extract_user_id(user)
        if chat_id is None and user_id is None:
            return None
        return IncomingMessage(
            chat_id=chat_id,
            user_id=user_id,
            payload=callback.get("payload") or callback.get("data"),
            callback_id=callback.get("callback_id") or callback.get("id"),
            username=user.get("username") if user else None,
            first_name=user.get("first_name") or user.get("name") if user else None,
            last_name=user.get("last_name") if user else None,
            raw_user=user,
        )

    message = update.get("message") or update.get("event", {}).get("message")
    if not isinstance(message, dict):
        chat_id = extract_chat_id(update)
        user = extract_user(update)
        user_id = extract_user_id(user)
        if chat_id is None and user_id is None:
            return None
        return IncomingMessage(
            chat_id=chat_id,
            user_id=user_id,
            username=user.get("username") if user else None,
            first_name=user.get("first_name") or user.get("name") if user else None,
            last_name=user.get("last_name") if user else None,
            raw_user=user,
        )

    chat_id = extract_chat_id(message) or extract_chat_id(update)

    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    text = body.get("text") or message.get("text")
    user = extract_user(message) or extract_user(update)
    user_id = extract_user_id(user)
    if chat_id is None and user_id is None:
        return None
    return IncomingMessage(
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        username=user.get("username") if user else None,
        first_name=user.get("first_name") or user.get("name") if user else None,
        last_name=user.get("last_name") if user else None,
        raw_user=user,
    )


def extract_chat_id(payload: dict[str, Any]) -> int | str | None:
    recipient = payload.get("recipient")
    if isinstance(recipient, dict) and recipient.get("chat_id") is not None:
        return recipient["chat_id"]

    chat = payload.get("chat")
    if isinstance(chat, dict) and chat.get("id") is not None:
        return chat["id"]

    if payload.get("chat_id") is not None:
        return payload["chat_id"]

    return None


def extract_user(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("user", "sender", "from"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return None


def extract_user_id(user: dict[str, Any] | None) -> int | str | None:
    if not user:
        return None
    return user.get("user_id") or user.get("id")


async def log_max_bot_profile(client: MaxClient) -> None:
    try:
        profile = await client.get_me()
    except httpx.HTTPStatusError as exc:
        logger.warning("Could not load MAX bot profile: %s", exc)
        logger.info("Starting MAX bot polling: profile unavailable")
        return
    except httpx.HTTPError as exc:
        logger.warning("Could not load MAX bot profile: %s", exc)
        logger.info("Starting MAX bot polling: profile unavailable")
        return

    logger.info(
        "Starting MAX bot polling: id=%s username=%s name=%s",
        first_value(profile.get("user_id"), profile.get("id"), profile.get("bot_id")),
        first_value(profile.get("username"), profile.get("login")),
        first_value(profile.get("name"), profile.get("first_name"), profile.get("title")),
    )


def first_value(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


async def run() -> None:
    settings = get_settings()
    if not settings.max_bot_token:
        raise RuntimeError("MAX_BOT_TOKEN is not set")

    logging.basicConfig(level=logging.INFO)
    await init_db()
    client = MaxClient(
        token=settings.max_bot_token,
        base_url=str(settings.max_api_base_url),
    )
    bot = MaxBot(client=client, settings=settings)
    try:
        await log_max_bot_profile(client)
        await bot.polling()
    finally:
        await client.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
