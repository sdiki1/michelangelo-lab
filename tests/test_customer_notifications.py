from michelangelo_bots.bot_configuration import render_template, status_title
from michelangelo_bots.customer_notifications import extract_delivery_status


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
