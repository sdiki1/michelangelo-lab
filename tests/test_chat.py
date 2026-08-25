from datetime import UTC, datetime

import httpx
import pytest

from michelangelo_bots import admin
from michelangelo_bots.chat import (
    ChatDeliveryError,
    ChatItem,
    deliver_message,
    merge_thread,
    recipient_id,
    send_admin_message,
)
from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotEvent, BotOrder, BotUser, ChatMessage


class FakeSession:
    """Заменяет AsyncSession: чат сохраняет сообщение даже при сбое доставки."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, instance: object) -> None:
        return None


def telegram_user() -> BotUser:
    user = BotUser(platform="telegram", platform_user_id="42", chat_id="4242", username="ivan")
    user.id = 7
    return user


def settings_with_tokens() -> Settings:
    return Settings(TELEGRAM_BOT_TOKEN="tg-token", MAX_BOT_TOKEN="max-token", _env_file=None)


def test_merge_thread_orders_incoming_and_outgoing_by_time() -> None:
    events = [
        BotEvent(
            user_id=7,
            platform="telegram",
            action="unknown_message",
            event_type="message",
            message_text="Когда доставка?",
            occurred_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        ),
        BotEvent(
            user_id=7,
            platform="telegram",
            action="main_menu",
            event_type="callback",
            message_text=None,
            occurred_at=datetime(2026, 1, 1, 10, 1, tzinfo=UTC),
        ),
    ]
    messages = [
        ChatMessage(
            user_id=7,
            direction="out",
            text="Завтра",
            status="sent",
            created_at=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        )
    ]

    thread = merge_thread(events, messages)

    assert [(item.direction, item.text) for item in thread] == [
        ("in", "Когда доставка?"),
        ("out", "Завтра"),
    ]


def test_merge_thread_limit_keeps_latest_messages() -> None:
    messages = [
        ChatMessage(
            user_id=7,
            direction="out",
            text=str(index),
            created_at=datetime(2026, 1, 1, 10, index, tzinfo=UTC),
        )
        for index in range(5)
    ]

    thread = merge_thread([], messages, limit=2)

    assert [item.text for item in thread] == ["3", "4"]


def test_recipient_id_prefers_chat_id() -> None:
    user = BotUser(platform="max", platform_user_id="99", chat_id=None)
    assert recipient_id(user) == "99"
    user.chat_id = "500"
    assert recipient_id(user) == "500"


async def test_deliver_message_without_recipient_is_rejected() -> None:
    user = BotUser(platform="telegram", platform_user_id="", chat_id=None)

    with pytest.raises(ChatDeliveryError, match="chat_id"):
        await deliver_message(user, "Привет", settings_with_tokens())


async def test_send_admin_message_delivers_to_telegram() -> None:
    session = FakeSession()
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["body"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(original):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: object) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    httpx.AsyncClient = PatchedClient  # type: ignore[misc]
    try:
        message = await send_admin_message(
            session,  # type: ignore[arg-type]
            user=telegram_user(),
            text="Здравствуйте",
            settings=settings_with_tokens(),
            author="admin",
            order_id=3,
        )
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert message.status == "sent"
    assert message.order_id == 3
    assert message.chat_id == "4242"
    assert session.added == [message]
    assert "sendMessage" in str(sent["url"])
    assert "Здравствуйте" in str(sent["body"])


async def test_send_admin_message_records_failed_delivery() -> None:
    session = FakeSession()
    user = BotUser(platform="whatsapp", platform_user_id="42", chat_id="42")
    user.id = 7

    message = await send_admin_message(
        session,  # type: ignore[arg-type]
        user=user,
        text="Привет",
        settings=settings_with_tokens(),
    )

    assert message.status == "failed"
    assert "whatsapp" in (message.error or "")
    assert session.added == [message]


async def test_deliver_message_rejects_unknown_platform() -> None:
    user = BotUser(platform="whatsapp", platform_user_id="42", chat_id="42")

    with pytest.raises(ChatDeliveryError):
        await deliver_message(user, "Привет", settings_with_tokens())


def test_chat_thread_html_marks_failed_outgoing_message() -> None:
    thread = [
        ChatItem(at=datetime(2026, 1, 1, tzinfo=UTC), direction="in", text="Привет"),
        ChatItem(
            at=datetime(2026, 1, 2, tzinfo=UTC),
            direction="out",
            text="Здравствуйте",
            status="failed",
            error="Telegram API HTTP 403",
            author="admin",
        ),
    ]

    html = admin.chat_thread_html(thread)

    assert 'class="bubble in"' in html
    assert 'class="bubble out failed"' in html
    assert "не доставлено" in html
    assert "Telegram API HTTP 403" in html


def test_rows_link_whole_row_to_order_and_user() -> None:
    user = telegram_user()
    order = BotOrder(
        external_source="readyscript",
        external_order_id="52",
        external_order_number="A-52",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    order.id = 3

    assert 'data-href="/orders/3"' in admin.order_row(order, user)
    assert 'data-href="/orders/3"' in admin.user_order_row(order)
    assert 'data-href="/users/7"' in admin.user_row(user)
    assert 'data-href="/users/7"' in admin.client_row(user)
    assert 'data-href="/chats/7"' in admin.chat_row(user, None)
