from michelangelo_bots.config import Settings
from michelangelo_bots.readyscript_sync import extract_customer_identity


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
