from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.content import START_TEXT, MenuButton, main_menu_buttons
from michelangelo_bots.db import BotMenuItem, BotOrder, BotSetting, BotUser

DEFAULT_SETTINGS: dict[str, str] = {
    "start_text": START_TEXT,
    "admin_order_template": (
        "🦋 Оформлен новый заказ\n"
        "Заказ: №{order_number}\n"
        "Источник: {source}\n"
        "Статус: {status}\n"
        "Сумма: {amount}\n"
        "Клиент: {customer_name}\n"
        "Телефон: {customer_phone}\n"
        "Email: {customer_email}\n"
        "{items}"
    ),
    "admin_incoming_template": (
        "💬 Новое обращение\n"
        "Источник: {source}\n"
        "Клиент: {customer_name}\n"
        "Username: {username}\n"
        "User ID: {platform_user_id}\n\n"
        "{message}"
    ),
    "customer_order_template": (
        "✅ Заказ №{order_number} принят!\n"
        "Статус: {status}\n"
        "Сумма: {amount}\n\n"
        "Мы сообщим здесь, когда статус доставки изменится."
    ),
    "customer_status_template": (
        "📦 Статус заказа №{order_number} изменился\n\n"
        "{status_title}"
    ),
    "customer_cancel_button_text": "❌ Отменить заказ",
    "customer_cancel_confirm_button_text": "Да, отменить",
    "customer_cancel_abort_button_text": "Нет",
    "customer_cancel_confirm_text": (
        "Отменить заказ №{order_number}? Если заказ уже передан в СДЭК, "
        "заявка на доставку также будет отменена."
    ),
    "customer_cancel_success_text": (
        "✅ Заказ №{order_number} отменён. Заявка на доставку в СДЭК также отменена, "
        "если она уже была создана."
    ),
    "customer_cancel_failure_text": (
        "Не удалось отменить заказ №{order_number} автоматически. "
        "Пожалуйста, свяжитесь с менеджером."
    ),
    "customer_cancel_too_late_text": (
        "Заказ №{order_number} уже нельзя отменить автоматически. "
        "Пожалуйста, свяжитесь с менеджером."
    ),
    "incoming_ack_text": (
        "Спасибо! Сообщение передано менеджеру. Он ответит вам в этом чате."
    ),
    "notification_failure_template": (
        "⚠️ Системная проблема с уведомлениями клиентов\n\n"
        "Не доставлено: {failed_count}\n"
        "Telegram: {telegram_count}, MAX: {max_count}\n"
        "Самая старая ошибка: {oldest_minutes} мин.\n"
        "Заказы: {orders}\n\n"
        "Система продолжает повторные попытки автоматически.\n"
        "Последняя ошибка: {last_error}"
    ),
    "notification_recovery_template": (
        "✅ Доставка уведомлений восстановлена\n\n"
        "Очередь проблемных сообщений очищена. Клиентские уведомления снова отправляются."
    ),
    "sync_failure_template": (
        "⚠️ Синхронизация ReadyScript/СДЭК не работает\n\n"
        "Сбой длится {oldest_minutes} мин., неудачных циклов: {failed_count}.\n"
        "Пока синхронизация не восстановится, новые статусы доставки не поступают.\n"
        "Последняя ошибка: {last_error}"
    ),
    "sync_recovery_template": (
        "✅ Синхронизация ReadyScript/СДЭК восстановлена\n\n"
        "Новые заказы и статусы снова обрабатываются."
    ),
    "telegram_manager_url": "https://t.me/michelangelo_nabor",
    "max_manager_url": "",
}

STATUS_TITLES: dict[str, str] = {
    "created": "Заказ оформлен",
    "accepted": "Заказ принят в работу",
    "ready": "Заказ подготовлен к отправке",
    "sent": "Заказ отправлен",
    "shipped": "Заказ отправлен",
    "delivered": "Заказ доставлен",
    "received": "Заказ выдан получателю",
    "at_pickup_point": "Заказ прибыл в пункт выдачи СДЭК",
    "pickup": "Заказ прибыл в пункт выдачи СДЭК",
    "problem": "Возникла проблема с доставкой. Менеджер свяжется с вами",
    "cancelled": "Заказ отменён",
}


class SafeValues(defaultdict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def render_template(template: str, values: dict[str, Any]) -> str:
    normalized = SafeValues(str)
    normalized.update(
        {key: "—" if value in (None, "") else str(value) for key, value in values.items()}
    )
    try:
        return template.format_map(normalized).strip()
    except ValueError:
        return template.strip()


async def setting(session: AsyncSession, key: str) -> str:
    row = await session.get(BotSetting, key)
    return row.value if row is not None else DEFAULT_SETTINGS[key]


async def all_settings(session: AsyncSession) -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    rows = (await session.execute(select(BotSetting))).scalars()
    values.update({row.key: row.value for row in rows})
    return values


async def menu_buttons(
    session: AsyncSession,
    platform: str,
    miniapp_url: str,
) -> list[MenuButton]:
    configured_rows = list(
        (
            await session.execute(
                select(BotMenuItem)
                .where(
                    BotMenuItem.platform.in_(("all", platform)),
                )
                .order_by(BotMenuItem.position, BotMenuItem.id)
            )
        ).scalars()
    )
    if not configured_rows:
        return main_menu_buttons(miniapp_url)
    rows = [row for row in configured_rows if row.active]
    return [
        MenuButton(
            row.title,
            action=f"config:{row.id}" if row.kind == "message" else None,
            url=row.url if row.kind == "link" else None,
            web_app=False,
        )
        for row in rows
    ]


async def menu_response(session: AsyncSession, action: str, platform: str) -> str | None:
    if not action.startswith("config:"):
        return None
    try:
        item_id = int(action.split(":", 1)[1])
    except ValueError:
        return None
    item = await session.get(BotMenuItem, item_id)
    if item is None or not item.active or item.platform not in ("all", platform):
        return None
    return item.body or ""


def order_values(order: BotOrder, user: BotUser | None = None) -> dict[str, str]:
    payload = order.raw_payload or {}
    items_value = payload.get("items")
    item_lines: list[str] = []
    if isinstance(items_value, list):
        for item in items_value[:20]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or item.get("product_title")
            qty = item.get("amount") or item.get("quantity") or item.get("qty")
            if title:
                item_lines.append(f"• {title}" + (f" × {qty}" if qty else ""))
    source = {"telegram": "Telegram", "max": "MAX"}.get(order.platform or "", "Сайт")
    return {
        "order_number": order.external_order_number or order.external_order_id,
        "source": source,
        "status": order.status or "принят",
        "status_title": status_title(order.status),
        "amount": f"{order.total_amount or ''} {order.currency or ''}".strip() or "—",
        "customer_name": order.customer_name or (user.full_name if user else None) or "—",
        "customer_phone": order.customer_phone or (user.phone if user else None) or "—",
        "customer_email": order.customer_email or (user.email if user else None) or "—",
        "username": f"@{user.username.lstrip('@')}" if user and user.username else "—",
        "platform_user_id": order.platform_user_id or "—",
        "items": "\n".join(item_lines) or "Состав заказа: —",
    }


def status_title(status: str | None) -> str:
    raw = (status or "").strip()
    normalized = raw.lower().replace("-", "_").replace(" ", "_")
    return STATUS_TITLES.get(normalized, raw or "Статус уточняется")


def is_terminal_order_status(status: str | None) -> bool:
    normalized = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    terminal_markers = (
        "cancel",
        "отмен",
        "received",
        "delivered",
        "выдан",
        "вручен",
        "доставлен",
        "success",
        "completed",
    )
    return any(marker in normalized for marker in terminal_markers)


def render_start_template(template: str, name: str | None) -> str:
    return template.replace("{{name}}", (name or "").strip() or "дорогой друг")
