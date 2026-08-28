from pathlib import Path

from michelangelo_bots.config import Settings
from michelangelo_bots.readyscript_sync import (
    extract_customer_identity,
    extract_platform_user_id,
    load_readyscript_script,
)

ROOT = Path(__file__).resolve().parents[1]


def test_extract_customer_identity_from_readyscript_order() -> None:
    settings = Settings(_env_file=None)

    identity = extract_customer_identity(
        {
            "id": "10",
            "contact_person": "Иван Иванов",
            "user_phone": "+7 (901) 900-15-61",
            "user_email": "IVAN@example.com",
            "telegram_user_id": "123",
        },
        settings,
    )

    assert identity == {
        "platform": "telegram",
        "platform_user_id": "123",
        "full_name": "Иван Иванов",
        "phone": "79019001561",
        "email": "ivan@example.com",
    }


def test_extract_customer_identity_from_platform_data() -> None:
    settings = Settings(_env_file=None)

    identity = extract_customer_identity(
        {
            "contact_person": "Max User",
            "platform_data": [
                {
                    "path": "data.order.max_user_id",
                    "value": "456",
                }
            ],
        },
        settings,
    )

    assert identity["platform"] == "max"
    assert identity["platform_user_id"] == "456"


def test_extract_customer_identity_from_normalized_module_fields() -> None:
    settings = Settings(_env_file=None)

    telegram = extract_customer_identity(
        {"telegram_user_id": "123", "max_user_id": "456"},
        settings,
    )
    max_only = extract_customer_identity({"max_user_id": "456"}, settings)

    assert telegram["platform"] == "telegram"
    assert telegram["platform_user_id"] == "123"
    assert max_only["platform"] == "max"
    assert max_only["platform_user_id"] == "456"


def test_extract_platform_user_id_from_legacy_module_fields() -> None:
    settings = Settings(_env_file=None)

    assert (
        extract_platform_user_id(
            {"ml_platform": "max", "ml_platform_user_id": "789"},
            settings,
            "max",
        )
        == "789"
    )


def test_order_pagination_uses_actual_collected_count_when_api_caps_page_size() -> None:
    script = load_readyscript_script(ROOT / "readyscript-orders" / "script.py")
    client = object.__new__(script.ReadyScriptClient)
    pages = {
        1: {"list": [{"id": "1"}, {"id": "2"}], "summary": {"total": 5}},
        2: {"list": [{"id": "3"}, {"id": "4"}], "summary": {"total": 5}},
        3: {"list": [{"id": "5"}], "summary": {"total": 5}},
    }
    client.get_orders_page = lambda page: pages[page]

    orders = client.get_all_orders()

    assert [order["id"] for order in orders] == ["1", "2", "3", "4", "5"]
