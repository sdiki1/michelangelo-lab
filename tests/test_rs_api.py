import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from michelangelo_bots.config import Settings
from michelangelo_bots.rs_api import PlatformIdentity, resolve_identity

BOT_TOKEN = "123:token"


def signed_init_data(bot_token: str, user: dict[str, object]) -> str:
    payload = {
        "auth_date": "1720000000",
        "query_id": "query",
        "user": json.dumps(user, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    payload["hash"] = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(payload)


def settings() -> Settings:
    return Settings(TELEGRAM_BOT_TOKEN=BOT_TOKEN, _env_file=None)


def test_verified_init_data_wins_over_claimed_id() -> None:
    identity = PlatformIdentity(
        platform="telegram",
        # Сайт заявляет чужой id — подпись Telegram должна его перебить.
        platform_user_id="999",
        init_data=signed_init_data(BOT_TOKEN, {"id": 42, "first_name": "Ivan"}),
    )

    assert resolve_identity(identity, settings()) == ("telegram", "42", "miniapp")


def test_forged_init_data_is_rejected() -> None:
    identity = PlatformIdentity(
        platform="telegram",
        init_data=signed_init_data(BOT_TOKEN, {"id": 42}) + "tampered",
    )

    with pytest.raises(HTTPException) as error:
        resolve_identity(identity, settings())
    assert error.value.status_code == 422


def test_manual_binding_without_init_data() -> None:
    identity = PlatformIdentity(platform="max", platform_user_id="456")

    assert resolve_identity(identity, settings()) == ("max", "456", "manual")


def test_unknown_platform_is_rejected() -> None:
    identity = PlatformIdentity(platform="whatsapp", platform_user_id="1")

    with pytest.raises(HTTPException) as error:
        resolve_identity(identity, settings())
    assert error.value.status_code == 422


def test_empty_identity_resolves_to_nothing() -> None:
    assert resolve_identity(None, settings()) == (None, None, "none")
    assert resolve_identity(PlatformIdentity(), settings()) == (None, None, "none")


def test_platform_without_id_resolves_to_nothing() -> None:
    identity = PlatformIdentity(platform="telegram", platform_user_id="   ")

    assert resolve_identity(identity, settings()) == (None, None, "none")


@pytest.mark.parametrize("endpoint", ["orders", "events", "identity"])
def test_unsigned_request_is_rejected_before_touching_the_database(endpoint: str) -> None:
    """Эндпоинт существует (не 404) и отбивает запрос без подписи, не открывая БД."""
    from fastapi.testclient import TestClient

    from michelangelo_bots.admin import app
    from michelangelo_bots.config import get_settings
    from michelangelo_bots.db import get_session

    def broken_session() -> None:
        raise AssertionError("сессия БД не должна открываться для неподписанного запроса")

    app.dependency_overrides[get_settings] = lambda: Settings(
        RS_MODULE_SECRET="configured",
        _env_file=None,
    )
    app.dependency_overrides[get_session] = broken_session
    try:
        response = TestClient(app).post(f"/api/rs/{endpoint}", json={"order_id": "1"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_signed_request_passes_signature_check() -> None:
    """Правильная подпись проходит проверку и запрос доходит до обработчика."""
    import time

    from fastapi.testclient import TestClient

    from michelangelo_bots.admin import app
    from michelangelo_bots.config import get_settings
    from michelangelo_bots.db import get_session
    from michelangelo_bots.webhook_security import build_signature

    secret = "shared-secret"
    body = json.dumps({"platform": "max", "platform_user_id": "77"}).encode()
    timestamp = int(time.time())

    reached = False

    async def fake_session() -> None:
        nonlocal reached
        reached = True
        raise RuntimeError("stop here: подпись уже проверена")

    app.dependency_overrides[get_settings] = lambda: Settings(
        RS_MODULE_SECRET=secret,
        _env_file=None,
    )
    app.dependency_overrides[get_session] = fake_session
    try:
        client = TestClient(app, raise_server_exceptions=False)
        client.post(
            "/api/rs/identity",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-ML-Timestamp": str(timestamp),
                "X-ML-Signature": build_signature(
                    secret=secret, timestamp=timestamp, body=body
                ),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert reached, "подписанный запрос не прошёл дальше проверки подписи"
