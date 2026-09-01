from datetime import UTC, datetime, timedelta

from michelangelo_bots.config import Settings
from michelangelo_bots.notification_health import (
    DeliveryFailure,
    monitor_customer_delivery_health,
    send_incident,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def failure(*, minutes: int, number: str = "A-1", platform: str = "telegram"):
    return DeliveryFailure(
        order_number=number,
        platform=platform,
        kind="status",
        status="at_pickup_point",
        error="messenger unavailable",
        first_failed_at=NOW - timedelta(minutes=minutes),
    )


async def configure_monitor(monkeypatch, *, failures, state=None):
    saved = []
    incidents = []
    recoveries = []

    async def fake_load_failures(_session):
        return failures

    async def fake_load_state(_session, _key):
        return dict(state or {})

    async def fake_save_state(_session, key, value):
        saved.append((key, value))

    async def fake_setting(_session, key):
        return key + " {failed_count} {oldest_minutes}"

    async def fake_incident(_settings, message, current_state, *, now):
        incidents.append((message, current_state, now))
        return 1, {"alerted_targets": ["telegram:chat_id:1"], "last_alert_at": now.isoformat()}

    async def fake_recovery(_settings, message, current_state):
        recoveries.append((message, current_state))
        return 1

    monkeypatch.setattr(
        "michelangelo_bots.notification_health.load_delivery_failures",
        fake_load_failures,
    )
    monkeypatch.setattr("michelangelo_bots.notification_health.load_state", fake_load_state)
    monkeypatch.setattr("michelangelo_bots.notification_health.save_state", fake_save_state)
    monkeypatch.setattr("michelangelo_bots.notification_health.setting", fake_setting)
    monkeypatch.setattr("michelangelo_bots.notification_health.send_incident", fake_incident)
    monkeypatch.setattr("michelangelo_bots.notification_health.send_recovery", fake_recovery)
    monkeypatch.setattr("michelangelo_bots.notification_health.datetime_now", lambda: NOW)
    return saved, incidents, recoveries


async def test_monitor_ignores_one_recent_delivery_failure(monkeypatch) -> None:
    saved, incidents, recoveries = await configure_monitor(
        monkeypatch,
        failures=[failure(minutes=120)],
    )

    delivered = await monitor_customer_delivery_health(
        object(),  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    assert delivered == 0
    assert incidents == []
    assert recoveries == []
    assert saved == []


async def test_monitor_alerts_about_three_failures_older_than_three_hours(monkeypatch) -> None:
    saved, incidents, _ = await configure_monitor(
        monkeypatch,
        failures=[
            failure(minutes=181, number="A-1"),
            failure(minutes=190, number="A-2", platform="max"),
            failure(minutes=200, number="A-3"),
        ],
    )

    delivered = await monitor_customer_delivery_health(
        object(),  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    assert delivered == 1
    assert len(incidents) == 1
    assert saved[-1][1]["active"] is True
    assert saved[-1][1]["failed_count"] == 3


async def test_monitor_alerts_about_one_failure_only_after_a_day(monkeypatch) -> None:
    _, incidents, _ = await configure_monitor(
        monkeypatch,
        failures=[failure(minutes=1441)],
    )

    await monitor_customer_delivery_health(
        object(),  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    assert len(incidents) == 1


async def test_monitor_sends_one_recovery_after_incident(monkeypatch) -> None:
    saved, _, recoveries = await configure_monitor(
        monkeypatch,
        failures=[],
        state={"active": True, "alerted_targets": ["telegram:chat_id:1"]},
    )

    delivered = await monitor_customer_delivery_health(
        object(),  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    assert delivered == 1
    assert len(recoveries) == 1
    assert saved[-1][1] == {}


async def test_incident_does_not_repeat_before_cooldown(monkeypatch) -> None:
    calls = []

    async def fake_deliver(_settings, _message, targets):
        calls.append([target.key for target in targets])
        return {target.key for target in targets}

    monkeypatch.setattr(
        "michelangelo_bots.notification_health.deliver_system_message",
        fake_deliver,
    )
    settings = Settings(
        ORDER_NOTIFICATION_TELEGRAM_CHAT_IDS="1",
        NOTIFICATION_ALERT_REMINDER_MINUTES=360,
        _env_file=None,
    )

    delivered, state = await send_incident(settings, "problem", {}, now=NOW)
    delivered_again, _ = await send_incident(
        settings,
        "problem",
        state,
        now=NOW + timedelta(minutes=30),
    )

    assert delivered == 1
    assert delivered_again == 0
    assert calls == [["telegram:chat_id:1"], []]
