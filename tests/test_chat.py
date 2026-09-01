import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx
import pytest

from michelangelo_bots import admin
from michelangelo_bots.chat import (
    ChatAttachment,
    ChatDeliveryError,
    ChatItem,
    deliver_message,
    incoming_attachments,
    merge_thread,
    outgoing_attachments,
    recipient_id,
    send_admin_message,
    telegram_attachments,
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


@contextlib.contextmanager
def patched_httpx(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[None]:
    """Перехватывает все клиенты httpx: чат создаёт их сам внутри доставки."""

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(original):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: object) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    httpx.AsyncClient = PatchedClient  # type: ignore[misc]
    try:
        yield
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


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


def telegram_photo_event(text: str | None = None) -> BotEvent:
    return BotEvent(
        user_id=7,
        platform="telegram",
        action="unknown_message",
        event_type="message",
        message_text=text,
        raw_update={
            "message_id": 11,
            "caption": text,
            "photo": [
                {"file_id": "small", "width": 90},
                {"file_id": "big-file-id", "width": 1280},
            ],
        },
        occurred_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )


def test_merge_thread_keeps_incoming_photo_without_text() -> None:
    thread = merge_thread([telegram_photo_event()], [])

    assert len(thread) == 1
    assert thread[0].text == ""
    assert [(a.kind, a.url) for a in thread[0].attachments] == [
        ("photo", "/media/telegram/big-file-id")
    ]


def test_telegram_attachments_take_largest_photo_and_video() -> None:
    attachments = telegram_attachments(
        {
            "photo": [{"file_id": "thumb"}, {"file_id": "full"}],
            "video": {"file_id": "vid", "mime_type": "video/mp4"},
            "document": {"file_id": "doc", "mime_type": "application/pdf"},
        }
    )

    assert [(a.kind, a.url) for a in attachments] == [
        ("photo", "/media/telegram/full"),
        ("video", "/media/telegram/vid"),
    ]


def test_max_attachments_read_photo_and_video_urls() -> None:
    event = BotEvent(
        user_id=7,
        platform="max",
        action="unknown_message",
        event_type="message",
        message_text=None,
        raw_update={
            "message": {
                "body": {
                    "attachments": [
                        {"type": "image", "payload": {"url": "https://cdn.max/p.jpg"}},
                        {"type": "video", "payload": {"url": "https://cdn.max/v.mp4"}},
                    ]
                }
            }
        },
        occurred_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )

    assert [(a.kind, a.url) for a in incoming_attachments(event)] == [
        ("photo", "https://cdn.max/p.jpg"),
        ("video", "https://cdn.max/v.mp4"),
    ]


def test_outgoing_attachments_are_served_by_admin() -> None:
    message = ChatMessage(
        user_id=7,
        direction="out",
        text="",
        attachments=[{"filename": "cat.jpg", "kind": "photo", "path": "/uploads/cat.jpg"}],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    message.id = 15

    assert outgoing_attachments(message) == [
        ChatAttachment(kind="photo", url="/media/chat/15/0", name="cat.jpg")
    ]


def test_chat_thread_html_renders_photo_and_video() -> None:
    thread = [
        ChatItem(
            at=datetime(2026, 1, 1, tzinfo=UTC),
            direction="in",
            text="",
            attachments=(
                ChatAttachment(kind="photo", url="/media/telegram/abc"),
                ChatAttachment(kind="video", url="/media/chat/1/0", name="clip.mp4"),
            ),
        )
    ]

    html = admin.chat_thread_html(thread)

    assert '<img class="bubble-media" src="/media/telegram/abc"' in html
    assert '<video class="bubble-media" src="/media/chat/1/0"' in html
    assert "bubble-text" not in html


async def test_send_admin_message_uploads_photo_to_telegram(tmp_path) -> None:
    session = FakeSession()
    photo = tmp_path / "cat.jpg"
    photo.write_bytes(b"jpeg-bytes")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        request.read()
        return httpx.Response(200, json={"ok": True})

    with patched_httpx(handler):
        message = await send_admin_message(
            session,  # type: ignore[arg-type]
            user=telegram_user(),
            text="Смотрите",
            settings=settings_with_tokens(),
            attachments=[
                {
                    "filename": "cat.jpg",
                    "path": str(photo),
                    "content_type": "image/jpeg",
                    "kind": "photo",
                }
            ],
        )

    assert message.status == "sent"
    assert message.attachments is not None
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/sendPhoto")
    body = requests[0].content
    assert b"jpeg-bytes" in body
    assert "Смотрите".encode() in body


async def test_send_admin_message_sends_media_group_for_several_files(tmp_path) -> None:
    session = FakeSession()
    files = []
    for index, kind in enumerate(("photo", "video")):
        path = tmp_path / f"f{index}"
        path.write_bytes(b"data")
        files.append(
            {
                "filename": path.name,
                "path": str(path),
                "content_type": "image/jpeg" if kind == "photo" else "video/mp4",
                "kind": kind,
            }
        )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        request.read()
        return httpx.Response(200, json={"ok": True})

    with patched_httpx(handler):
        message = await send_admin_message(
            session,  # type: ignore[arg-type]
            user=telegram_user(),
            text="Два файла",
            settings=settings_with_tokens(),
            attachments=files,
        )

    assert message.status == "sent"
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == ["sendMediaGroup"]
    body = requests[0].content.decode("utf-8", "replace")
    assert '"type": "photo"' in body
    assert '"type": "video"' in body


async def test_send_admin_message_uploads_attachment_to_max(tmp_path) -> None:
    session = FakeSession()
    photo = tmp_path / "cat.jpg"
    photo.write_bytes(b"jpeg-bytes")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        if request.url.path == "/uploads":
            return httpx.Response(200, json={"url": "https://upload.max/slot"})
        if str(request.url) == "https://upload.max/slot":
            return httpx.Response(200, json={"photos": {"p1": {"token": "tok"}}})
        request.read()
        return httpx.Response(200, json={"message": {}})

    user = BotUser(platform="max", platform_user_id="99", chat_id="500")
    user.id = 7

    with patched_httpx(handler):
        message = await send_admin_message(
            session,  # type: ignore[arg-type]
            user=user,
            text="Фото",
            settings=settings_with_tokens(),
            attachments=[
                {
                    "filename": "cat.jpg",
                    "path": str(photo),
                    "content_type": "image/jpeg",
                    "kind": "photo",
                }
            ],
        )

    assert message.status == "sent", message.error
    assert any("/uploads" in call for call in calls)
    assert any("upload.max/slot" in call for call in calls)
    assert any("/messages" in call for call in calls)


async def test_send_admin_message_reports_max_upload_failure(tmp_path) -> None:
    session = FakeSession()
    photo = tmp_path / "cat.jpg"
    photo.write_bytes(b"jpeg-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="too big")

    user = BotUser(platform="max", platform_user_id="99", chat_id="500")
    user.id = 7

    with patched_httpx(handler):
        message = await send_admin_message(
            session,  # type: ignore[arg-type]
            user=user,
            text="Фото",
            settings=settings_with_tokens(),
            attachments=[
                {
                    "filename": "cat.jpg",
                    "path": str(photo),
                    "content_type": "image/jpeg",
                    "kind": "photo",
                }
            ],
        )

    assert message.status == "failed"
    assert "cat.jpg" in (message.error or "")
