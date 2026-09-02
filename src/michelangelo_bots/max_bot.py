import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from michelangelo_bots.bot_configuration import (
    menu_buttons,
    menu_response,
    order_values,
    render_start_template,
    render_template,
    setting,
)
from michelangelo_bots.config import Settings, get_settings
from michelangelo_bots.content import (
    Action,
    MenuButton,
    back_to_main_buttons,
    render_start_text,
    text_for_action,
)
from michelangelo_bots.db import get_session_factory, init_db
from michelangelo_bots.order_cancellation import cancel_customer_order, get_customer_order
from michelangelo_bots.tracking import UserSnapshot, track_interaction

logger = logging.getLogger(__name__)
DEFAULT_RATE_LIMIT_SLEEP_SECONDS = 60.0
MAX_RATE_LIMIT_SLEEP_SECONDS = 300.0


def max_keyboard(
    buttons: Sequence[MenuButton],
    *,
    web_app: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    for button in buttons:
        if button.url:
            if web_app and button.web_app:
                rows.append(
                    [{"type": "open_app", "text": button.title, "web_app": web_app}]
                )
            else:
                rows.append([{"type": "link", "text": button.title, "url": button.url}])
        elif button.action:
            action_value = (
                button.action.value if isinstance(button.action, Action) else button.action
            )
            rows.append(
                [{"type": "callback", "text": button.title, "payload": action_value}]
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
    photo_urls: tuple[str, ...] = ()


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

    async def upload_attachment(
        self,
        *,
        kind: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        """Загружает файл в MAX и возвращает готовое вложение для send_message.

        MAX принимает медиа в два шага: сначала выдаёт одноразовый upload URL,
        затем по нему принимает файл и возвращает токен (для фото — словарь
        ``photos``), который и подставляется в attachments сообщения.
        """

        upload_type = "video" if kind == "video" else "image"
        response = await self._client.post(
            "/uploads",
            params={"type": upload_type},
            headers=self._auth_headers,
        )
        response.raise_for_status()
        upload_url = response.json().get("url")
        if not upload_url:
            raise RuntimeError(f"MAX did not return an upload URL for {upload_type}")

        # Upload URL абсолютный и живёт вне base_url MAX API, ходим отдельным клиентом.
        async with httpx.AsyncClient(timeout=120) as upload_client:
            uploaded = await upload_client.post(
                upload_url,
                files={"data": (filename, content, content_type)},
            )
        uploaded.raise_for_status()
        return max_attachment_from_upload(upload_type, upload_response_payload(uploaded))

    async def answer_callback(self, callback_id: str) -> None:
        response = await self._client.post(
            "/answers",
            params={"callback_id": callback_id},
            headers=self._auth_headers,
            json={},
        )
        response.raise_for_status()


class MaxBot:
    def __init__(
        self,
        client: MaxClient,
        settings: Settings,
        *,
        web_app: str | None = None,
    ):
        self._client = client
        self._settings = settings
        self._web_app = normalize_max_web_app(web_app or settings.max_bot_username)

    async def handle_update(self, update: dict[str, Any]) -> None:
        incoming = parse_update(update)
        if incoming is None:
            logger.info("Skipping unsupported MAX update type=%s", update.get("update_type"))
            return

        if is_get_my_id_command(incoming.text):
            await track_max_interaction(incoming, action="getmyid", raw_update=update)
            recipient_id = outgoing_recipient_id(incoming)
            if recipient_id is None:
                logger.warning("Cannot answer /getmyid without chat_id or user_id: %s", update)
                return
            await self._client.send_message(
                recipient_id,
                get_my_id_text(incoming),
                attachments=[],
                recipient_type=outgoing_recipient_type(incoming),
            )
            return

        callback_answered = False
        action = action_from_incoming(incoming)
        action_value = incoming.payload or (
            "unknown_message" if is_customer_question(incoming) else action.value
        )
        user = await track_max_interaction(incoming, action=action_value, raw_update=update)
        if is_admin_channel_message(incoming, self._settings):
            return
        if incoming.payload and incoming.payload.startswith("order_cancel:"):
            if incoming.callback_id:
                await self._client.answer_callback(incoming.callback_id)
                callback_answered = True
            order_id = callback_order_id(incoming.payload)
            order = (
                await get_customer_order(order_id, bot_user_id=user.id, platform="max")
                if order_id is not None
                else None
            )
            if order is None:
                response_text = "Этот заказ недоступен."
                keyboard = []
            else:
                async with get_session_factory()() as session:
                    template = await setting(session, "customer_cancel_confirm_text")
                    confirm_button_text = await setting(
                        session, "customer_cancel_confirm_button_text"
                    )
                    abort_button_text = await setting(
                        session, "customer_cancel_abort_button_text"
                    )
                response_text = render_template(template, order_values(order, user))
                keyboard = [{
                    "type": "inline_keyboard",
                    "payload": {"buttons": [[
                        {
                            "type": "callback",
                            "text": confirm_button_text,
                            "payload": f"order_cancel_confirm:{order.id}",
                        },
                        {
                            "type": "callback",
                            "text": abort_button_text,
                            "payload": f"order_cancel_abort:{order.id}",
                        },
                    ]]},
                }]
        elif incoming.payload and incoming.payload.startswith("order_cancel_confirm:"):
            if incoming.callback_id:
                await self._client.answer_callback(incoming.callback_id)
                callback_answered = True
            order_id = callback_order_id(incoming.payload)
            if order_id is None:
                response_text = "Этот заказ недоступен."
                keyboard = []
            else:
                result = await cancel_customer_order(
                    order_id,
                    bot_user_id=user.id,
                    platform="max",
                    platform_user_id=user.platform_user_id,
                    settings=self._settings,
                )
                async with get_session_factory()() as session:
                    if result.outcome == "cancelled":
                        key = "customer_cancel_success_text"
                    elif result.outcome == "too_late":
                        key = "customer_cancel_too_late_text"
                    elif result.outcome == "processing":
                        key = None
                    else:
                        key = "customer_cancel_failure_text"
                    response_text = (
                        render_template(
                            await setting(session, key),
                            {"order_number": result.order_number},
                        )
                        if key
                        else "Запрос на отмену этого заказа уже обрабатывается."
                    )
                    manager_url = await setting(session, "max_manager_url")
                keyboard = []
                if result.outcome != "cancelled" and manager_url:
                    keyboard = [{
                        "type": "inline_keyboard",
                        "payload": {"buttons": [[{
                            "type": "link",
                            "text": "✉️ Связаться с менеджером",
                            "url": manager_url,
                        }]]},
                    }]
        elif incoming.payload and incoming.payload.startswith("order_cancel_abort:"):
            if incoming.callback_id:
                await self._client.answer_callback(incoming.callback_id)
                callback_answered = True
            response_text = "Заказ не отменён."
            keyboard = []
        elif is_customer_question(incoming):
            from michelangelo_bots.inbound_notifications import notify_admins_about_incoming

            await notify_admins_about_incoming(
                settings=self._settings,
                source="max",
                user=user,
                message=incoming.text or "📷 Клиент прислал фотографию",
                photo_urls=list(incoming.photo_urls),
            )
            async with get_session_factory()() as session:
                response_text = await setting(session, "incoming_ack_text")
            keyboard: list[dict[str, Any]] = []
        elif incoming.payload and incoming.payload.startswith("config:"):
            async with get_session_factory()() as session:
                response_text = await menu_response(session, incoming.payload, "max")
            response_text = response_text if response_text is not None else "Раздел недоступен"
            keyboard = max_keyboard(back_to_main_buttons())
        else:
            response_text = text_for_incoming(action, incoming)
            if action is Action.MAIN_MENU:
                async with get_session_factory()() as session:
                    buttons = await menu_buttons(
                        session, "max", str(self._settings.max_miniapp_url)
                    )
                    start_text = await setting(session, "start_text")
                keyboard = max_keyboard(buttons, web_app=self._web_app)
                response_text = render_start_template(start_text, max_display_name(incoming))
            else:
                keyboard = max_keyboard(back_to_main_buttons())
        recipient_id = outgoing_recipient_id(incoming)
        recipient_type = outgoing_recipient_type(incoming)
        if recipient_id is None:
            logger.warning("Cannot answer MAX update without chat_id or user_id: %s", update)
            return

        await self._client.send_message(
            recipient_id,
            response_text,
            attachments=keyboard,
            recipient_type=recipient_type,
        )

        if incoming.callback_id and not callback_answered:
            await self._client.answer_callback(incoming.callback_id)

    async def polling(self) -> None:
        marker: int | str | None = None
        while True:
            try:
                data = await self._client.get_updates(
                    marker=marker,
                    timeout_seconds=self._settings.max_poll_timeout_seconds,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    retry_after = retry_after_seconds(exc.response)
                    logger.warning("MAX rate limit reached, sleeping %.1f seconds", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                logger.error(
                    "MAX get_updates failed: HTTP %s %s",
                    exc.response.status_code,
                    response_detail(exc.response),
                )
                await asyncio.sleep(3)
                continue
            except Exception:
                logger.exception("MAX get_updates failed")
                await asyncio.sleep(3)
                continue

            # Каждый апдейт обрабатываем отдельно: сбой на одном не должен
            # выбрасывать нас из пачки и не должен уносить остальные вместе с
            # уже сдвинутым marker — иначе сообщения теряются безвозвратно.
            for update in data.get("updates", []):
                try:
                    await self.handle_update(update)
                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "MAX update handling failed: HTTP %s %s | update=%s",
                        exc.response.status_code,
                        response_detail(exc.response),
                        update,
                    )
                except Exception:
                    logger.exception("MAX update handling failed: update=%s", update)

            marker = data.get("marker", marker)


def action_from_incoming(incoming: IncomingMessage) -> Action:
    if not incoming.payload:
        return Action.MAIN_MENU
    try:
        return Action(incoming.payload)
    except ValueError:
        return Action.MAIN_MENU


def callback_order_id(payload: str | None) -> int | None:
    try:
        return int((payload or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def is_customer_question(incoming: IncomingMessage) -> bool:
    if incoming.payload or incoming.callback_id:
        return False
    text = (incoming.text or "").strip()
    return bool(incoming.photo_urls) or bool(text and not text.lower().startswith("/start"))


def is_admin_channel_message(incoming: IncomingMessage, settings: Settings) -> bool:
    if incoming.chat_id is None:
        return False
    from michelangelo_bots.order_notifications import parse_recipient_ids

    return str(incoming.chat_id) in parse_recipient_ids(
        settings.order_notification_max_chat_ids
    )

def is_get_my_id_command(text: str | None) -> bool:
    if not text:
        return False
    command = text.strip().split(maxsplit=1)[0].lower()
    return command.split("@", 1)[0] == "/getmyid"


def get_my_id_text(incoming: IncomingMessage) -> str:
    if incoming.user_id is None:
        return (
            "Не удалось определить MAX user ID. "
            "Отправьте команду /getmyid боту в личном чате."
        )
    return (
        "Ваш MAX user ID:\n"
        f"{incoming.user_id}\n\n"
        "Строка для .env:\n"
        f"ORDER_NOTIFICATION_MAX_USER_IDS={incoming.user_id}"
    )

def outgoing_recipient_id(incoming: IncomingMessage) -> int | str | None:
    return incoming.chat_id or incoming.user_id


def outgoing_recipient_type(incoming: IncomingMessage) -> str:
    return "chat_id" if incoming.chat_id is not None else "user_id"


def text_for_incoming(action: Action, incoming: IncomingMessage) -> str:
    if action is Action.MAIN_MENU:
        return render_start_text(max_display_name(incoming))
    return text_for_action(action)


def response_detail(response: httpx.Response) -> str:
    """Тело ответа MAX — без него ошибка 400 не диагностируется."""
    try:
        return json.dumps(response.json(), ensure_ascii=False)[:500]
    except ValueError:
        return response.text[:500]


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
) -> Any:
    platform_user_id = str(incoming.user_id or incoming.chat_id)
    full_name = " ".join(part for part in [incoming.first_name, incoming.last_name] if part) or None
    return await track_interaction(
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
    attachments = body.get("attachments") or message.get("attachments") or []
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
        photo_urls=tuple(extract_photo_urls(attachments)),
    )


def upload_response_payload(response: httpx.Response) -> dict[str, Any]:
    """Ответ upload-эндпоинта MAX: обычно JSON, для видео иногда пустое тело."""

    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def max_attachment_from_upload(upload_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if upload_type == "image":
        photos = payload.get("photos")
        if isinstance(photos, dict) and photos:
            return {"type": "image", "payload": {"photos": photos}}
    token = payload.get("token")
    if token:
        return {"type": upload_type, "payload": {"token": token}}
    raise RuntimeError(f"MAX upload response has no token: {payload}")


MAX_MEDIA_KINDS = {
    "image": "photo",
    "photo": "photo",
    "video": "video",
}


def extract_media(attachments: Any) -> list[dict[str, str]]:
    """Достаёт из вложений MAX ссылки на фото и видео с их типом."""

    if not isinstance(attachments, list):
        return []
    media: list[dict[str, str]] = []
    seen: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        kind = MAX_MEDIA_KINDS.get(str(attachment.get("type") or ""))
        if kind is None:
            continue
        payload = attachment.get("payload")
        if not isinstance(payload, dict):
            payload = attachment
        for url in find_http_urls(payload):
            if url in seen:
                continue
            seen.add(url)
            media.append({"kind": kind, "url": url})
    return media


def extract_photo_urls(attachments: Any) -> list[str]:
    return [item["url"] for item in extract_media(attachments) if item["kind"] == "photo"]


def find_http_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, dict):
        result: list[str] = []
        for key, child in value.items():
            if key in {"url", "src", "source", "photos"}:
                result.extend(find_http_urls(child))
        return result
    if isinstance(value, list):
        return [url for item in value for url in find_http_urls(item)]
    return []


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


def normalize_max_web_app(value: Any) -> str | None:
    username = str(value or "").strip().lstrip("@")
    return username or None


def max_web_app_from_profile(profile: dict[str, Any]) -> str | None:
    return normalize_max_web_app(first_value(profile.get("username"), profile.get("login")))


async def log_max_bot_profile(client: MaxClient) -> str | None:
    try:
        profile = await client.get_me()
    except httpx.HTTPStatusError as exc:
        logger.warning("Could not load MAX bot profile: %s", exc)
        logger.info("Starting MAX bot polling: profile unavailable")
        return None
    except httpx.HTTPError as exc:
        logger.warning("Could not load MAX bot profile: %s", exc)
        logger.info("Starting MAX bot polling: profile unavailable")
        return None

    username = max_web_app_from_profile(profile)
    logger.info(
        "Starting MAX bot polling: id=%s username=%s name=%s",
        first_value(profile.get("user_id"), profile.get("id"), profile.get("bot_id")),
        username,
        first_value(profile.get("name"), profile.get("first_name"), profile.get("title")),
    )
    return username


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
    try:
        profile_web_app = await log_max_bot_profile(client)
        web_app = normalize_max_web_app(settings.max_bot_username) or profile_web_app
        if not web_app:
            logger.warning(
                "MAX miniapp button will be an external link: set MAX_BOT_USERNAME"
            )
        bot = MaxBot(client=client, settings=settings, web_app=web_app)
        await bot.polling()
    finally:
        await client.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
