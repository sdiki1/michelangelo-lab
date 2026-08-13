from michelangelo_bots.config import Settings
from michelangelo_bots.readyscript_sync import extract_customer_identity, extract_platform_user_id


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
