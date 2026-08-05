import hashlib
import hmac
import json
from typing import Any
from urllib.parse import parse_qsl


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict[str, Any]:
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("Telegram initData hash is missing")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Telegram initData hash is invalid")

    if parsed.get("user"):
        parsed["user"] = json.loads(parsed["user"])
    return parsed
