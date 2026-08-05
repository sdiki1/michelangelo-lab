import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from michelangelo_bots.telegram_webapp import verify_telegram_init_data


def test_verify_telegram_init_data_returns_user() -> None:
    bot_token = "123:token"
    init_data = signed_init_data(bot_token, {"id": 42, "first_name": "Ivan"})

    parsed = verify_telegram_init_data(init_data, bot_token)

    assert parsed["user"]["id"] == 42
    assert parsed["user"]["first_name"] == "Ivan"


def test_verify_telegram_init_data_rejects_invalid_hash() -> None:
    bot_token = "123:token"
    init_data = signed_init_data(bot_token, {"id": 42}) + "broken"

    with pytest.raises(ValueError, match="invalid"):
        verify_telegram_init_data(init_data, bot_token)


def signed_init_data(bot_token: str, user: dict[str, object]) -> str:
    payload = {
        "auth_date": "1720000000",
        "query_id": "query",
        "user": json.dumps(user, separators=(",", ":")),
    }
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(payload.items())
    )
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()
    payload["hash"] = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return urlencode(payload)
