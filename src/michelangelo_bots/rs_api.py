"""Приём вебхуков от модуля michelangelo в ReadyScript.

Три канала:

* ``POST /api/rs/orders``   — заказ создан или изменён;
* ``POST /api/rs/events``   — пачка действий пользователя на витрине;
* ``POST /api/rs/identity`` — миниапп сообщил, кто открыл сайт.

Все запросы подписаны HMAC (см. webhook_security). Идентичность Telegram
подтверждается здесь, а не на стороне сайта: initData проверяется токеном бота,
который живёт только в этой системе. Значение, присланное сайтом без initData,
authoritative не считается и помечается как ``bind_source="manual"``.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.config import Settings, get_settings
from michelangelo_bots.db import BotOrder, BotUser, SiteEvent, datetime_now, get_session
from michelangelo_bots.telegram_webapp import verify_telegram_init_data
from michelangelo_bots.webhook_security import WebhookSignatureError, verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rs", tags=["readyscript"])

SUPPORTED_PLATFORMS = {"telegram", "max"}


class PlatformIdentity(BaseModel):
    """Кто открыл витрину. init_data — единственный проверяемый источник."""

    model_config = ConfigDict(extra="allow")

    platform: str | None = None
    platform_user_id: str | int | None = None
    init_data: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class OrderWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    event: str = Field(default="order.updated")
    order_id: str | int
    order_num: str | int | None = None
    status: str | None = None
    total: str | int | float | None = None
    currency: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    customer_email: str | None = None
    identity: PlatformIdentity | None = None
    items: list[dict[str, Any]] | None = None
    occurred_at: datetime | None = None


class SiteEventIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    action: str
    path: str | None = None
    title: str | None = None
    product_id: str | int | None = None
    product_title: str | None = None
    referrer: str | None = None
    session_id: str | None = None
    occurred_at: datetime | None = None
    payload: dict[str, Any] | None = None


class SiteEventBatch(BaseModel):
    identity: PlatformIdentity | None = None
    events: list[SiteEventIn] = Field(default_factory=list, max_length=200)


async def require_signature(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    x_ml_timestamp: Annotated[str | None, Header(alias="X-ML-Timestamp")] = None,
    x_ml_signature: Annotated[str | None, Header(alias="X-ML-Signature")] = None,
) -> None:
    if not settings.rs_module_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RS_MODULE_SECRET is not configured",
        )
    try:
        verify_signature(
            secret=settings.rs_module_secret,
            timestamp=x_ml_timestamp,
            signature=x_ml_signature,
            body=await request.body(),
            tolerance_seconds=settings.rs_signature_tolerance_seconds,
        )
    except WebhookSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def resolve_identity(
    identity: PlatformIdentity | None,
    settings: Settings,
) -> tuple[str | None, str | None, str]:
    """Возвращает (platform, platform_user_id, bind_source).

    initData, проверенный подписью Telegram, всегда побеждает значение,
    присланное сайтом «на слово».
    """
    if identity is None:
        return None, None, "none"

    if identity.init_data:
        try:
            parsed = verify_telegram_init_data(identity.init_data, settings.telegram_bot_token)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Telegram initData: {exc}") from exc
        user = parsed.get("user") or {}
        if user.get("id") is not None:
            return "telegram", str(user["id"]), "miniapp"

    platform = (identity.platform or "").strip().lower() or None
    if platform is not None and platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=422, detail=f"Unknown platform: {platform}")

    raw_id = identity.platform_user_id
    platform_user_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None
    if platform is None or platform_user_id is None:
        return None, None, "none"

    return platform, platform_user_id, "manual"


async def find_or_create_user(
    session: AsyncSession,
    *,
    platform: str | None,
    platform_user_id: str | None,
    identity: PlatformIdentity | None,
) -> BotUser | None:
    if not platform or not platform_user_id:
        return None

    result = await session.execute(
        select(BotUser).where(
            BotUser.platform == platform,
            BotUser.platform_user_id == platform_user_id,
        )
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    # Пользователь мог оформить заказ в миниаппе, ни разу не написав боту.
    user = BotUser(
        platform=platform,
        platform_user_id=platform_user_id,
        chat_id=platform_user_id,
        username=identity.username if identity else None,
        first_name=identity.first_name if identity else None,
        last_name=identity.last_name if identity else None,
        full_name=display_name(identity),
        status="lead",
        raw_profile={"source": "readyscript_miniapp"},
    )
    session.add(user)
    await session.flush()
    return user


def display_name(identity: PlatformIdentity | None) -> str | None:
    if identity is None:
        return None
    parts = [identity.first_name, identity.last_name]
    return " ".join(part for part in parts if part) or None


@router.post("/orders")
async def receive_order(
    payload: OrderWebhook,
    _: Annotated[None, Depends(require_signature)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    platform, platform_user_id, bind_source = resolve_identity(payload.identity, settings)
    user = await find_or_create_user(
        session,
        platform=platform,
        platform_user_id=platform_user_id,
        identity=payload.identity,
    )

    external_order_id = str(payload.order_id)
    now = datetime_now()
    result = await session.execute(
        select(BotOrder).where(
            BotOrder.external_source == "readyscript",
            BotOrder.external_order_id == external_order_id,
        )
    )
    order = result.scalar_one_or_none()
    if order is None:
        order = BotOrder(
            external_source="readyscript",
            external_order_id=external_order_id,
            created_at=now,
        )
        session.add(order)

    order.external_order_number = optional_str(payload.order_num)
    order.status = payload.status
    order.total_amount = optional_str(payload.total)
    order.currency = payload.currency
    order.customer_name = payload.customer_name or order.customer_name
    order.customer_phone = payload.customer_phone or order.customer_phone
    order.customer_email = payload.customer_email or order.customer_email
    order.raw_payload = payload.model_dump(mode="json", exclude_none=True)
    order.updated_at = now

    # Привязку не сбрасываем: пустой identity в апдейте статуса не должен
    # стирать то, что уже определил миниапп.
    if platform and platform_user_id:
        order.platform = platform
        order.platform_user_id = platform_user_id
        order.bind_source = bind_source
        order.telegram_user_id = platform_user_id if platform == "telegram" else None
    if user is not None:
        order.bot_user_id = user.id
        user.phone = order.customer_phone or user.phone
        user.email = order.customer_email or user.email
        user.full_name = order.customer_name or user.full_name

    await session.commit()
    await session.refresh(order)
    return {
        "ok": True,
        "order_id": order.id,
        "external_order_id": order.external_order_id,
        "platform": order.platform,
        "platform_user_id": order.platform_user_id,
        "bind_source": order.bind_source,
        "bot_user_id": order.bot_user_id,
    }


@router.post("/events")
async def receive_events(
    batch: SiteEventBatch,
    _: Annotated[None, Depends(require_signature)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    platform, platform_user_id, _bind = resolve_identity(batch.identity, settings)
    user = await find_or_create_user(
        session,
        platform=platform,
        platform_user_id=platform_user_id,
        identity=batch.identity,
    )

    stored = 0
    for event in batch.events:
        statement = (
            insert(SiteEvent)
            .values(
                external_source="readyscript",
                external_event_id=event.event_id,
                bot_user_id=user.id if user else None,
                platform=platform,
                platform_user_id=platform_user_id,
                session_id=event.session_id,
                action=event.action,
                path=event.path,
                title=event.title,
                product_id=optional_str(event.product_id),
                product_title=event.product_title,
                referrer=event.referrer,
                payload=event.payload,
                occurred_at=as_utc(event.occurred_at) or datetime_now(),
            )
            # Сайт вправе переслать пачку повторно — принимаем идемпотентно.
            .on_conflict_do_nothing(
                index_elements=[SiteEvent.external_source, SiteEvent.external_event_id]
            )
        )
        result = await session.execute(statement)
        stored += result.rowcount or 0

    if user is not None and batch.events:
        user.last_seen_at = datetime_now()
        user.last_action = batch.events[-1].action

    await session.commit()
    return {"ok": True, "received": len(batch.events), "stored": stored}


@router.post("/identity")
async def receive_identity(
    identity: PlatformIdentity,
    _: Annotated[None, Depends(require_signature)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Подтверждает initData и возвращает сайту доверенный id пользователя."""
    platform, platform_user_id, bind_source = resolve_identity(identity, settings)
    if not platform or not platform_user_id:
        raise HTTPException(status_code=422, detail="Identity could not be resolved")

    user = await find_or_create_user(
        session,
        platform=platform,
        platform_user_id=platform_user_id,
        identity=identity,
    )
    await session.commit()
    return {
        "ok": True,
        "platform": platform,
        "platform_user_id": platform_user_id,
        "bind_source": bind_source,
        "bot_user_id": user.id if user else None,
    }


def optional_str(value: object) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
