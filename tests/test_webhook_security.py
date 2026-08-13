import pytest

from michelangelo_bots.webhook_security import (
    WebhookSignatureError,
    build_signature,
    verify_signature,
)

SECRET = "s3cret"
BODY = b'{"order_id":"42","total":"5900"}'


def test_signature_roundtrip() -> None:
    signature = build_signature(secret=SECRET, timestamp=1_700_000_000, body=BODY)

    verify_signature(
        secret=SECRET,
        timestamp="1700000000",
        signature=signature,
        body=BODY,
        now=1_700_000_010,
    )


def test_rejects_tampered_body() -> None:
    signature = build_signature(secret=SECRET, timestamp=1_700_000_000, body=BODY)

    with pytest.raises(WebhookSignatureError, match="mismatch"):
        verify_signature(
            secret=SECRET,
            timestamp="1700000000",
            signature=signature,
            body=b'{"order_id":"42","total":"1"}',
            now=1_700_000_010,
        )


def test_rejects_wrong_secret() -> None:
    signature = build_signature(secret="other", timestamp=1_700_000_000, body=BODY)

    with pytest.raises(WebhookSignatureError, match="mismatch"):
        verify_signature(
            secret=SECRET,
            timestamp="1700000000",
            signature=signature,
            body=BODY,
            now=1_700_000_010,
        )


def test_rejects_replayed_request() -> None:
    signature = build_signature(secret=SECRET, timestamp=1_700_000_000, body=BODY)

    with pytest.raises(WebhookSignatureError, match="window"):
        verify_signature(
            secret=SECRET,
            timestamp="1700000000",
            signature=signature,
            body=BODY,
            now=1_700_000_000 + 3600,
        )


def test_rejects_missing_headers() -> None:
    with pytest.raises(WebhookSignatureError, match="Missing"):
        verify_signature(secret=SECRET, timestamp=None, signature=None, body=BODY)


def test_rejects_unconfigured_secret() -> None:
    with pytest.raises(WebhookSignatureError, match="not configured"):
        verify_signature(secret="", timestamp="1700000000", signature="x", body=BODY)


def test_signature_matches_php_module() -> None:
    """Контрольный вектор, сверенный с Michelangelo\\Model\\Api::sign() в PHP.

    Если этот тест упал — разошлись реализации подписи на двух сторонах,
    и ReadyScript перестанет достукиваться до админ-панели.
    """
    signature = build_signature(secret="test-secret", timestamp=1700000000, body=b'{"a":1}')

    assert signature == (
        "sha256=8cb2c3355fca388e9ac2caec004f4d5d7045d74937ab5faad61dc11682247a9f"
    )
