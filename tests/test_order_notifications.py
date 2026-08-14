from michelangelo_bots.config import Settings
from michelangelo_bots.db import BotOrder, BotUser
from michelangelo_bots.order_notifications import (
    admin_targets,
    build_order_notification,
    parse_recipient_ids,
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
