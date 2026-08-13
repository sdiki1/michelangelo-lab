"""Подпись запросов между ReadyScript и админ-панелью.

Схема (одинаковая на обеих сторонах):

    base      = f"{timestamp}.{raw_body}"
    signature = hex(hmac_sha256(secret, base))

Заголовки запроса:

    X-ML-Timestamp: 1723545600
    X-ML-Signature: sha256=<hex>

Подписывается именно сырое тело запроса, а не разобранный JSON: любая
пересериализация меняет байты и ломает подпись.
"""

import hashlib
import hmac
import time

SIGNATURE_PREFIX = "sha256="
DEFAULT_TOLERANCE_SECONDS = 300


class WebhookSignatureError(ValueError):
    """Подпись отсутствует, испорчена или просрочена."""


def build_signature(*, secret: str, timestamp: int | str, body: bytes) -> str:
    base = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Бросает WebhookSignatureError, если запрос нельзя принять."""
    if not secret:
        raise WebhookSignatureError("Webhook secret is not configured")
    if not timestamp or not signature:
        raise WebhookSignatureError("Missing signature headers")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise WebhookSignatureError("Timestamp is not an integer") from exc

    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance_seconds:
        raise WebhookSignatureError("Timestamp is outside the allowed window")

    expected = build_signature(secret=secret, timestamp=sent_at, body=body)
    if not hmac.compare_digest(expected, signature):
        raise WebhookSignatureError("Signature mismatch")
