from datetime import UTC, datetime

import pytest

from michelangelo_bots import order_notifications
from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotOrder, BotUser
from michelangelo_bots.order_notifications import (
    admin_targets,
    build_order_notification,
    deliver_pending_order_notifications,
    parse_recipient_ids,
    telegram_contact_reply_markup,
    telegram_user_url,
)


def test_parse_recipient_ids_accepts_commas_semicolons_and_lines() -> None:
    assert parse_recipient_ids("1, 2;3\n2") == ["1", "2", "3"]


def test_admin_targets_support_both_messengers() -> None:
    settings = Settings(
        ORDER_NOTIFICATION_TELEGRAM_CHAT_IDS="11,12",
        ORDER_NOTIFICATION_MAX_USER_IDS="21",
        ORDER_NOTIFICATION_MAX_CHAT_IDS="31",
        _env_file=None,
    )

    assert [target.key for target in admin_targets(settings)] == [
        "telegram:chat_id:11",
        "telegram:chat_id:12",
        "max:user_id:21",
        "max:chat_id:31",
    ]


def test_build_order_notification_contains_platform_and_contact() -> None:
    order = BotOrder(
        external_source="readyscript",
        external_order_id="52",
        external_order_number="A-52",
        platform="max",
        platform_user_id="456",
        total_amount="5900",
        currency="RUB",
        customer_name="Иван Иванов",
        customer_phone="79019001561",
        customer_email="ivan@example.com",
        raw_payload={"items": [{"title": "Набор", "amount": 2}]},
    )
    user = BotUser(
        platform="max",
        platform_user_id="456",
        username="ivan",
    )

    message = build_order_notification(order, user)

    assert "Заказ: №A-52" in message
    assert "Мессенджер: MAX" in message
    assert "Телефон: 79019001561" in message
    assert "Email: ivan@example.com" in message
    assert "Username: @ivan" in message
    assert "MAX user ID: 456" in message
    assert "• Набор × 2" in message


def test_build_order_notification_marks_regular_website_order() -> None:
    order = BotOrder(
        external_source="readyscript",
        external_order_id="56",
        external_order_number="WEB-56",
        platform=None,
        customer_name="Обычный покупатель",
    )

    message = build_order_notification(order, None)

    assert "Источник: сайт / ReadyScript" in message
    assert "Мессенджер:" not in message
    assert "user ID:" not in message


@pytest.mark.asyncio
async def test_regular_website_order_is_delivered_to_managers(monkeypatch) -> None:
    order = BotOrder(
        external_source="readyscript",
        external_order_id="57",
        external_order_number="WEB-57",
        platform=None,
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    delivered_orders = []

    class Result:
        def scalars(self):
            return [order]

    class Session:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

        async def get(self, _model, _identity):
            return None

    async def fake_send_notification(*_args, **kwargs):
        delivered_orders.append(kwargs["order"])

    monkeypatch.setattr(order_notifications, "send_notification", fake_send_notification)
    settings = Settings(
        ORDER_NOTIFICATION_TELEGRAM_CHAT_IDS="100",
        _env_file=None,
    )

    notified = await deliver_pending_order_notifications(Session(), settings)

    assert notified == 1
    assert delivered_orders == [order]
    assert order.admin_notified_at is not None


def test_telegram_contact_button_prefers_username() -> None:
    order = BotOrder(
        external_source="readyscript",
        external_order_id="53",
        platform="telegram",
        platform_user_id="123",
    )
    user = BotUser(platform="telegram", platform_user_id="123", username="ivan")

    assert telegram_user_url(order, user) == "https://t.me/ivan"
    assert telegram_contact_reply_markup(order, user) == {
        "inline_keyboard": [
            [{"text": "✉️ Написать пользователю", "url": "https://t.me/ivan"}]
        ]
    }


def test_telegram_contact_button_falls_back_to_user_id() -> None:
    order = BotOrder(
        external_source="readyscript",
        external_order_id="54",
        platform="telegram",
        platform_user_id="123",
    )

    assert telegram_user_url(order, None) == "tg://user?id=123"


def test_max_order_has_no_telegram_contact_button() -> None:
    order = BotOrder(
        external_source="readyscript",
        external_order_id="55",
        platform="max",
        platform_user_id="456",
    )

    assert telegram_contact_reply_markup(order, None) is None
