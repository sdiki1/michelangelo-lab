from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from michelangelo_bots.config import get_settings


def datetime_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class BotUser(Base):
    __tablename__ = "bot_users"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id", name="uq_platform_user"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    platform_user_id: Mapped[str] = mapped_column(String(128), index=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), index=True)
    username: Mapped[str | None] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(64), index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64), default="active", index=True)
    referral: Mapped[str | None] = mapped_column(String(255), index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(512), index=True)
    language_code: Mapped[str | None] = mapped_column(String(32))
    is_bot: Mapped[bool | None]
    raw_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime_now,
        index=True,
    )
    total_actions: Mapped[int] = mapped_column(Integer, default=0)
    last_action: Mapped[str | None] = mapped_column(String(128), index=True)
    last_message_text: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list["BotEvent"]] = relationship(back_populates="user")


class BotBroadcast(Base):
    __tablename__ = "bot_broadcasts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    text: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(32))
    media_url: Mapped[str | None] = mapped_column(Text)
    media_files: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    include_miniapp_button: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(64), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime_now,
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    total_recipients: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class BotOrder(Base):
    __tablename__ = "bot_orders"
    __table_args__ = (
        UniqueConstraint("external_source", "external_order_id", name="uq_external_order"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_source: Mapped[str] = mapped_column(String(64), default="readyscript", index=True)
    external_order_id: Mapped[str] = mapped_column(String(128), index=True)
    external_order_number: Mapped[str | None] = mapped_column(String(128), index=True)
    platform: Mapped[str | None] = mapped_column(String(32), index=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    bot_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("bot_users.id", ondelete="SET NULL"),
        index=True,
    )
    platform_user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    bind_source: Mapped[str | None] = mapped_column(String(32), index=True)
    status: Mapped[str | None] = mapped_column(String(128), index=True)
    total_amount: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str | None] = mapped_column(String(16))
    customer_name: Mapped[str | None] = mapped_column(String(512), index=True)
    customer_phone: Mapped[str | None] = mapped_column(String(64), index=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), index=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    admin_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    admin_notification_attempts: Mapped[int] = mapped_column(Integer, default=0)
    admin_notification_error: Mapped[str | None] = mapped_column(Text)
    admin_notification_delivered: Mapped[list[str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime_now,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime_now,
        index=True,
    )


class IntegrationState(Base):
    __tablename__ = "integration_states"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class SiteEvent(Base):
    """Действие пользователя на витрине ReadyScript, открытой в миниаппе."""

    __tablename__ = "site_events"
    __table_args__ = (
        UniqueConstraint("external_source", "external_event_id", name="uq_site_event"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_source: Mapped[str] = mapped_column(String(64), default="readyscript", index=True)
    # Идентификатор события на стороне сайта — защита от повторной доставки.
    external_event_id: Mapped[str] = mapped_column(String(128), index=True)
    bot_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("bot_users.id", ondelete="SET NULL"),
        index=True,
    )
    platform: Mapped[str | None] = mapped_column(String(32), index=True)
    platform_user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    path: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[str | None] = mapped_column(String(128), index=True)
    product_title: Mapped[str | None] = mapped_column(Text)
    referrer: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime_now,
        index=True,
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime_now)


class BotEvent(Base):
    __tablename__ = "bot_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    message_text: Mapped[str | None] = mapped_column(Text)
    callback_payload: Mapped[str | None] = mapped_column(Text)
    raw_update: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime_now,
        index=True,
    )

    user: Mapped[BotUser] = relationship(back_populates="events")


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def init_db() -> None:
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS phone VARCHAR(64)")
        )
        await connection.execute(
            text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS email VARCHAR(255)")
        )
        await connection.execute(
            text(
                "ALTER TABLE bot_users "
                "ADD COLUMN IF NOT EXISTS status VARCHAR(64) DEFAULT 'active'"
            )
        )
        await connection.execute(
            text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referral VARCHAR(255)")
        )
        await connection.execute(
            text("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS comment TEXT")
        )
        await connection.execute(
            text("ALTER TABLE bot_broadcasts ADD COLUMN IF NOT EXISTS media_files JSONB")
        )
        await connection.execute(
            text("ALTER TABLE bot_orders ADD COLUMN IF NOT EXISTS platform_user_id VARCHAR(128)")
        )
        await connection.execute(
            text("ALTER TABLE bot_orders ADD COLUMN IF NOT EXISTS bind_source VARCHAR(32)")
        )
        await connection.execute(
            text(
                "ALTER TABLE bot_orders "
                "ADD COLUMN IF NOT EXISTS admin_notified_at TIMESTAMPTZ"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE bot_orders "
                "ADD COLUMN IF NOT EXISTS admin_notification_attempts INTEGER DEFAULT 0"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE bot_orders "
                "ADD COLUMN IF NOT EXISTS admin_notification_error TEXT"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE bot_orders "
                "ADD COLUMN IF NOT EXISTS admin_notification_delivered JSONB"
            )
        )
        # Заказы, привязанные до появления platform_user_id, переносим на новую колонку.
        await connection.execute(
            text(
                "UPDATE bot_orders SET platform_user_id = telegram_user_id "
                "WHERE platform_user_id IS NULL AND telegram_user_id IS NOT NULL"
            )
        )


async def get_session() -> AsyncIterator[AsyncSession]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session
