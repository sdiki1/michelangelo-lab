import httpx
import pytest

from michelangelo_bots import max_bot
from michelangelo_bots.config import Settings
from michelangelo_bots.content import ABOUT_TEXT, Action, MenuButton
from michelangelo_bots.max_bot import (
    IncomingMessage,
    MaxBot,
    MaxClient,
    action_from_incoming,
    callback_order_id,
    get_my_id_text,
    is_get_my_id_command,
    max_keyboard,
    max_web_app_from_profile,
    normalize_max_web_app,
    parse_update,
    retry_after_seconds,
)


class FakeMaxClient:
    def __init__(self) -> None:
        self.messages = []
        self.answers = []

    async def send_message(  # noqa: ANN001
        self,
        recipient_id,
        text,
        attachments,
        *,
        recipient_type="chat_id",
    ):
        self.messages.append((recipient_id, text, attachments, recipient_type))

    async def answer_callback(self, callback_id: str) -> None:
        self.answers.append(callback_id)


def test_max_keyboard_builds_callback_and_open_app_buttons() -> None:
    keyboard = max_keyboard(
        [
            MenuButton("Миниапп", url="https://example.com/app"),
            MenuButton("О нас", action=Action.ABOUT),
        ],
        web_app="michelangelo_bot",
    )

    buttons = keyboard[0]["payload"]["buttons"]
    assert buttons[0][0] == {
        "type": "open_app",
        "text": "Миниапп",
        "web_app": "michelangelo_bot",
    }
    assert buttons[1][0] == {"type": "callback", "text": "О нас", "payload": "about"}


def test_max_keyboard_falls_back_to_link_without_bot_username() -> None:
    keyboard = max_keyboard([MenuButton("Миниапп", url="https://example.com/app")])

    assert keyboard[0]["payload"]["buttons"][0][0] == {
        "type": "link",
        "text": "Миниапп",
        "url": "https://example.com/app",
    }


def test_max_configured_link_stays_link_when_web_app_is_available() -> None:
    keyboard = max_keyboard(
        [MenuButton("Документы", url="https://example.com/docs", web_app=False)],
        web_app="michelangelo_bot",
    )

    assert keyboard[0]["payload"]["buttons"][0][0] == {
        "type": "link",
        "text": "Документы",
        "url": "https://example.com/docs",
    }


def test_max_web_app_uses_profile_username() -> None:
    assert normalize_max_web_app("@michelangelo_bot") == "michelangelo_bot"
    assert max_web_app_from_profile({"username": "michelangelo_bot"}) == "michelangelo_bot"


def test_parse_message_created_update() -> None:
    incoming = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "recipient": {"chat_id": 123},
                "body": {"text": "/start"},
            },
        }
    )

    assert incoming == IncomingMessage(chat_id=123, text="/start")


def test_parse_callback_update() -> None:
    incoming = parse_update(
        {
            "update_type": "message_callback",
            "callback": {
                "callback_id": "cb-1",
                "payload": "about",
                "message": {"recipient": {"chat_id": 123}},
            },
        }
    )

    assert incoming == IncomingMessage(chat_id=123, payload="about", callback_id="cb-1")


def test_parse_bot_started_update() -> None:
    incoming = parse_update(
        {
            "update_type": "bot_started",
            "chat_id": 123,
            "user": {"user_id": 456, "username": "ivan"},
        }
    )

    assert incoming == IncomingMessage(
        chat_id=123,
        user_id=456,
        username="ivan",
        raw_user={"user_id": 456, "username": "ivan"},
    )


def test_parse_direct_message_without_chat_id_uses_sender_user_id() -> None:
    incoming = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 456, "username": "ivan"},
                "recipient": {"user_id": 244666192},
                "body": {"text": "hello"},
            },
        }
    )

    assert incoming == IncomingMessage(
        chat_id=None,
        user_id=456,
        text="hello",
        username="ivan",
        raw_user={"user_id": 456, "username": "ivan"},
    )


def test_get_my_id_command_parser_and_text() -> None:
    incoming = IncomingMessage(chat_id=None, user_id=456, text="/getmyid")

    assert is_get_my_id_command("  /GETMYID@michelangelo_bot  ")
    assert not is_get_my_id_command("/start")
    assert "ORDER_NOTIFICATION_MAX_USER_IDS=456" in get_my_id_text(incoming)


@pytest.mark.asyncio
async def test_max_bot_returns_sender_user_id_for_getmyid() -> None:
    client = FakeMaxClient()
    settings = Settings(
        MAX_BOT_TOKEN="token",
        MINIAPP_URL="https://example.com/app",
        MAX_API_BASE_URL="https://botapi.max.ru",
        _env_file=None,
    )
    bot = MaxBot(client=client, settings=settings)  # type: ignore[arg-type]

    await bot.handle_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 456, "username": "admin"},
                "recipient": {"user_id": 244666192},
                "body": {"text": "/getmyid"},
            },
        }
    )

    assert client.messages == [
        (
            456,
            "Ваш MAX user ID:\n456\n\n"
            "Строка для .env:\nORDER_NOTIFICATION_MAX_USER_IDS=456",
            [],
            "user_id",
        )
    ]

@pytest.mark.asyncio
async def test_max_bot_sends_about_for_callback() -> None:
    client = FakeMaxClient()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="",
        MAX_BOT_TOKEN="token",
        MINIAPP_URL="https://example.com/app",
        MAX_API_BASE_URL="https://botapi.max.ru",
    )
    bot = MaxBot(client=client, settings=settings)  # type: ignore[arg-type]

    await bot.handle_update(
        {
            "callback": {
                "callback_id": "cb-1",
                "payload": "about",
                "message": {"recipient": {"chat_id": 123}},
            }
        }
    )

    assert client.messages[0][0] == 123
    assert client.messages[0][1] == ABOUT_TEXT
    assert client.messages[0][3] == "chat_id"
    assert client.answers == ["cb-1"]


def test_unknown_text_returns_main_menu() -> None:
    assert action_from_incoming(IncomingMessage(chat_id=123, text="hello")) is Action.MAIN_MENU


def test_order_cancel_callback_parser_accepts_only_numeric_ids() -> None:
    assert callback_order_id("order_cancel:42") == 42
    assert callback_order_id("order_cancel_confirm:42") == 42
    assert callback_order_id("order_cancel:bad") is None


def test_retry_after_seconds_parses_header() -> None:
    response = httpx.Response(429, headers={"Retry-After": "12"})

    assert retry_after_seconds(response) == 12


def test_retry_after_seconds_uses_default_without_header() -> None:
    response = httpx.Response(429)

    assert retry_after_seconds(response) == 60


@pytest.mark.asyncio
async def test_max_client_uses_authorization_header_for_messages() -> None:
    captured_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, json={})

    http_client = httpx.AsyncClient(
        base_url="https://botapi.max.ru",
        transport=httpx.MockTransport(handler),
    )
    client = MaxClient(token="token", base_url="https://botapi.max.ru", http_client=http_client)

    await client.send_message(123, "hello", [])

    assert captured_request is not None
    assert captured_request.headers["Authorization"] == "token"
    assert "access_token" not in str(captured_request.url)
    assert "chat_id=123" in str(captured_request.url)


@pytest.mark.asyncio
async def test_max_client_can_send_message_to_user_id() -> None:
    captured_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, json={})

    http_client = httpx.AsyncClient(
        base_url="https://botapi.max.ru",
        transport=httpx.MockTransport(handler),
    )
    client = MaxClient(token="token", base_url="https://botapi.max.ru", http_client=http_client)

    await client.send_message(456, "hello", [], recipient_type="user_id")

    assert captured_request is not None
    assert "user_id=456" in str(captured_request.url)
    assert "chat_id" not in str(captured_request.url)


@pytest.mark.asyncio
async def test_max_client_uses_authorization_header_for_updates() -> None:
    captured_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, json={"updates": [], "marker": 42})

    http_client = httpx.AsyncClient(
        base_url="https://botapi.max.ru",
        transport=httpx.MockTransport(handler),
    )
    client = MaxClient(token="token", base_url="https://botapi.max.ru", http_client=http_client)

    data = await client.get_updates(marker=10, timeout_seconds=30)

    assert data["marker"] == 42
    assert captured_request is not None
    assert captured_request.headers["Authorization"] == "token"
    assert "access_token" not in str(captured_request.url)
    assert "marker=10" in str(captured_request.url)


@pytest.mark.asyncio
async def test_max_client_loads_profile_with_authorization_header() -> None:
    captured_request: httpx.Request | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, json={"user_id": 123, "username": "michelangelo_bot"})

    http_client = httpx.AsyncClient(
        base_url="https://botapi.max.ru",
        transport=httpx.MockTransport(handler),
    )
    client = MaxClient(token="token", base_url="https://botapi.max.ru", http_client=http_client)

    data = await client.get_me()

    assert data["username"] == "michelangelo_bot"
    assert captured_request is not None
    assert captured_request.headers["Authorization"] == "token"
    assert captured_request.url.path == "/me"
    assert "access_token" not in str(captured_request.url)


def test_extract_media_splits_photos_and_videos() -> None:
    media = max_bot.extract_media(
        [
            {"type": "image", "payload": {"url": "https://cdn.max/p.jpg"}},
            {"type": "video", "payload": {"url": "https://cdn.max/v.mp4"}},
            {"type": "inline_keyboard", "payload": {"buttons": []}},
        ]
    )

    assert media == [
        {"kind": "photo", "url": "https://cdn.max/p.jpg"},
        {"kind": "video", "url": "https://cdn.max/v.mp4"},
    ]


def test_max_attachment_from_upload_supports_photos_and_tokens() -> None:
    assert max_bot.max_attachment_from_upload("image", {"photos": {"p": {"token": "t"}}}) == {
        "type": "image",
        "payload": {"photos": {"p": {"token": "t"}}},
    }
    assert max_bot.max_attachment_from_upload("video", {"token": "vt"}) == {
        "type": "video",
        "payload": {"token": "vt"},
    }
    with pytest.raises(RuntimeError, match="no token"):
        max_bot.max_attachment_from_upload("image", {})
