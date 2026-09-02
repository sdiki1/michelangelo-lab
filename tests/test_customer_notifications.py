from michelangelo_bots.bot_configuration import render_template, status_title
from michelangelo_bots.customer_notifications import (
    extract_delivery_status,
    max_customer_keyboard,
    telegram_customer_keyboard,
)
from michelangelo_bots.db import BotOrder


def test_extract_delivery_status_prefers_cdek_fields() -> None:
    assert (
        extract_delivery_status(
            {
                "status_title": "Оплачен",
                "cdek": {"status_title": "Прибыл в пункт выдачи"},
            }
        )
        == "Прибыл в пункт выдачи"
    )


def test_template_renderer_supports_editable_placeholders() -> None:
    assert render_template("Заказ {order_number}: {status}", {"order_number": 42}) == "Заказ 42:"
    assert status_title("at_pickup_point") == "Заказ прибыл в пункт выдачи СДЭК"


def test_customer_order_keyboard_contains_manager_and_cancel_buttons() -> None:
    order = BotOrder(id=42, external_order_id="100", status="sent")

    telegram = telegram_customer_keyboard(
        order,
        manager_url="https://t.me/manager",
        cancel_button_text="Отменить",
    )
    assert telegram == {
        "inline_keyboard": [
            [{"text": "✉️ Связаться с менеджером", "url": "https://t.me/manager"}],
            [{"text": "Отменить", "callback_data": "order_cancel:42"}],
        ]
    }

    max_markup = max_customer_keyboard(
        order,
        manager_url="https://max.ru/manager",
        cancel_button_text="Отменить",
    )
    assert max_markup[0]["payload"]["buttons"][1][0]["payload"] == "order_cancel:42"


def test_customer_order_keyboard_hides_cancel_for_terminal_order() -> None:
    order = BotOrder(
        id=42,
        external_order_id="100",
        status="cancelled",
        cancellation_state="cancelled",
    )

    telegram = telegram_customer_keyboard(
        order,
        manager_url="https://t.me/manager",
        cancel_button_text="Отменить",
    )
    assert telegram == {
        "inline_keyboard": [
            [{"text": "✉️ Связаться с менеджером", "url": "https://t.me/manager"}]
        ]
    }
