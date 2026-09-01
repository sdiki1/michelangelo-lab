"""Self-monitoring for customer messages and the ReadyScript/CDEK sync.

Delivery itself is retried by ``deliver_customer_notifications`` every polling
cycle.  This module deliberately alerts only when failures become systematic:
several messages have been failing for hours, or one message has been stuck for
a full day.  Incident state is stored in PostgreSQL, so a worker restart does
not reset the anti-spam cooldown.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.bot_configuration import render_template, setting
from michelangelo_bots.config import Settings
from michelangelo_bots.db import (
    BotOrder,
    BotUser,
    IntegrationState,
    OrderStatusNotification,
    datetime_now,
)
from michelangelo_bots.max_bot import MaxClient
from michelangelo_bots.order_notifications import AdminTarget, admin_targets

logger = logging.getLogger(__name__)

DELIVERY_HEALTH_STATE_KEY = "customer_notification_health"
SYNC_HEALTH_STATE_KEY = "readyscript_sync_health"


@dataclass(frozen=True)
class DeliveryFailure:
    order_number: str
    platform: str
    kind: str
    status: str | None
    error: str
    first_failed_at: datetime


async def monitor_customer_delivery_health(
    session: AsyncSession,
    settings: Settings,
) -> int:
    """Send a deduplicated incident/recovery message and return deliveries made."""

    failures = await load_delivery_failures(session)
    now = datetime_now()
    old_enough = [
        failure
        for failure in failures
        if age_minutes(failure.first_failed_at, now)
        >= settings.notification_alert_after_minutes
    ]
    isolated_too_old = any(
        age_minutes(failure.first_failed_at, now)
        >= settings.notification_alert_isolated_after_minutes
        for failure in failures
    )
    incident = (
        len(old_enough) >= settings.notification_alert_min_failures
        or isolated_too_old
    )
    state = await load_state(session, DELIVERY_HEALTH_STATE_KEY)

    if not incident:
        if state.get("active"):
            template = await setting(session, "notification_recovery_template")
            delivered = await send_recovery(
                settings,
                template,
                state,
            )
            await save_state(session, DELIVERY_HEALTH_STATE_KEY, {})
            return delivered
        if state:
            await save_state(session, DELIVERY_HEALTH_STATE_KEY, {})
        return 0

    oldest = min(failures, key=lambda item: as_utc(item.first_failed_at))
    values = failure_values(failures, now)
    template = await setting(session, "notification_failure_template")
    message = render_template(template, values)
    delivered, updated = await send_incident(
        settings,
        message,
        state,
        now=now,
    )
    updated.update(
        {
            "active": True,
            "kind": "customer_delivery",
            "failed_count": len(failures),
            "oldest_failed_at": as_utc(oldest.first_failed_at).isoformat(),
        }
    )
    await save_state(session, DELIVERY_HEALTH_STATE_KEY, updated)
    return delivered


async def record_sync_failure(
    session: AsyncSession,
    settings: Settings,
    error: Exception | str,
) -> int:
    """Persist a failed polling cycle and alert after a sustained outage."""

    now = datetime_now()
    state = await load_state(session, SYNC_HEALTH_STATE_KEY)
    first_failed_at = parse_datetime(state.get("first_failed_at")) or now
    failed_count = int(state.get("failed_count") or 0) + 1
    state.update(
        {
            "first_failed_at": first_failed_at.isoformat(),
            "failed_count": failed_count,
            "last_error": str(error)[:1000],
        }
    )

    if age_minutes(first_failed_at, now) < settings.notification_alert_after_minutes:
        await save_state(session, SYNC_HEALTH_STATE_KEY, state)
        return 0

    template = await setting(session, "sync_failure_template")
    message = render_template(
        template,
        {
            "oldest_minutes": age_minutes(first_failed_at, now),
            "failed_count": failed_count,
            "last_error": str(error)[:500],
        },
    )
    delivered, state = await send_incident(settings, message, state, now=now)
    state["active"] = True
    await save_state(session, SYNC_HEALTH_STATE_KEY, state)
    return delivered


async def record_sync_success(session: AsyncSession, settings: Settings) -> int:
    """Clear the polling incident, with one recovery message if it was alerted."""

    state = await load_state(session, SYNC_HEALTH_STATE_KEY)
    if not state:
        return 0
    delivered = 0
    if state.get("active"):
        template = await setting(session, "sync_recovery_template")
        delivered = await send_recovery(settings, template, state)
    await save_state(session, SYNC_HEALTH_STATE_KEY, {})
    return delivered


async def load_delivery_failures(session: AsyncSession) -> list[DeliveryFailure]:
    failures: list[DeliveryFailure] = []
    confirmations = (
        await session.execute(
            select(BotOrder, BotUser)
            .join(BotUser, BotUser.id == BotOrder.bot_user_id)
            .where(
                BotOrder.customer_notified_at.is_(None),
                BotOrder.customer_notification_error.is_not(None),
                BotOrder.customer_notification_first_failed_at.is_not(None),
            )
        )
    ).all()
    for order, user in confirmations:
        failures.append(
            DeliveryFailure(
                order_number=order.external_order_number or order.external_order_id,
                platform=user.platform,
                kind="order_confirmation",
                status=order.status,
                error=order.customer_notification_error or "unknown error",
                first_failed_at=order.customer_notification_first_failed_at,
            )
        )

    statuses = (
        await session.execute(
            select(OrderStatusNotification, BotOrder, BotUser)
            .join(BotOrder, BotOrder.id == OrderStatusNotification.order_id)
            .join(BotUser, BotUser.id == BotOrder.bot_user_id)
            .where(
                OrderStatusNotification.delivered_at.is_(None),
                OrderStatusNotification.error.is_not(None),
                OrderStatusNotification.first_failed_at.is_not(None),
                # An undelivered old status becomes obsolete after a newer status
                # arrives.  Alert only about the current status that is still retried.
                OrderStatusNotification.status == BotOrder.status,
            )
        )
    ).all()
    for ledger, order, user in statuses:
        failures.append(
            DeliveryFailure(
                order_number=order.external_order_number or order.external_order_id,
                platform=user.platform,
                kind="status",
                status=ledger.status,
                error=ledger.error or "unknown error",
                first_failed_at=ledger.first_failed_at,
            )
        )
    return failures


def failure_values(failures: list[DeliveryFailure], now: datetime) -> dict[str, Any]:
    order_numbers = list(dict.fromkeys(item.order_number for item in failures))
    oldest_minutes = max(age_minutes(item.first_failed_at, now) for item in failures)
    latest = max(failures, key=lambda item: as_utc(item.first_failed_at))
    return {
        "failed_count": len(failures),
        "telegram_count": sum(item.platform == "telegram" for item in failures),
        "max_count": sum(item.platform == "max" for item in failures),
        "oldest_minutes": oldest_minutes,
        "orders": ", ".join(order_numbers[:10])
        + (f" и ещё {len(order_numbers) - 10}" if len(order_numbers) > 10 else ""),
        "last_error": latest.error[:500],
    }


async def send_incident(
    settings: Settings,
    message: str,
    state: dict[str, Any],
    *,
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    targets = admin_targets(settings)
    if not targets:
        logger.warning("Cannot report notification incident: no administrator targets")
        return 0, state

    last_alert_at = parse_datetime(state.get("last_alert_at"))
    reminder_due = last_alert_at is None or (
        now - last_alert_at
        >= timedelta(minutes=settings.notification_alert_reminder_minutes)
    )
    already_delivered = set(state.get("alerted_targets") or [])
    if reminder_due:
        already_delivered.clear()

    delivered = await deliver_system_message(
        settings,
        message,
        [target for target in targets if target.key not in already_delivered],
    )
    already_delivered.update(delivered)
    updated = dict(state)
    updated["alerted_targets"] = sorted(already_delivered)
    if delivered and reminder_due:
        updated["last_alert_at"] = now.isoformat()
    return len(delivered), updated


async def send_recovery(
    settings: Settings,
    message: str,
    state: dict[str, Any],
) -> int:
    target_by_key = {target.key: target for target in admin_targets(settings)}
    alerted_keys = state.get("alerted_targets") or list(target_by_key)
    targets = [target_by_key[key] for key in alerted_keys if key in target_by_key]
    delivered = await deliver_system_message(settings, message, targets)
    return len(delivered)


async def deliver_system_message(
    settings: Settings,
    message: str,
    targets: list[AdminTarget],
) -> set[str]:
    delivered: set[str] = set()
    if not targets:
        return delivered

    async with httpx.AsyncClient(
        base_url=str(settings.max_api_base_url).rstrip("/"),
        timeout=30,
    ) as client:
        max_client = MaxClient(
            settings.max_bot_token,
            str(settings.max_api_base_url),
            http_client=client,
        )
        for target in targets:
            try:
                if target.platform == "telegram":
                    if not settings.telegram_bot_token:
                        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
                    response = await client.post(
                        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                        json={"chat_id": target.recipient_id, "text": message},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("ok"):
                        raise RuntimeError(f"Telegram API rejected health alert: {payload}")
                else:
                    if not settings.max_bot_token:
                        raise RuntimeError("MAX_BOT_TOKEN is not set")
                    await max_client.send_message(
                        target.recipient_id,
                        message,
                        [],
                        recipient_type=target.recipient_type,
                    )
            except Exception:
                logger.exception("Could not send system alert to %s", target.key)
            else:
                delivered.add(target.key)
    return delivered


async def load_state(session: AsyncSession, key: str) -> dict[str, Any]:
    row = await session.get(IntegrationState, key)
    if row is None:
        return {}
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def save_state(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await session.get(IntegrationState, key)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if row is None:
        session.add(IntegrationState(key=key, value=serialized))
    else:
        row.value = serialized
    await session.commit()


def parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def age_minutes(started_at: datetime, now: datetime) -> int:
    return max(0, int((as_utc(now) - as_utc(started_at)).total_seconds() // 60))
