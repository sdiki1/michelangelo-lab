import asyncio
import html
import json
import secrets
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.bot_configuration import DEFAULT_SETTINGS, all_settings
from michelangelo_bots.bot_flow import (
    Flow,
    Platform,
    encoded_flow,
    ensure_flows,
    flow_key,
    photo_path,
)
from michelangelo_bots.broadcasting import send_broadcast
from michelangelo_bots.chat import (
    ChatAttachment,
    ChatItem,
    load_thread,
    recipient_id,
    send_admin_message,
)
from michelangelo_bots.config import Settings, get_settings
from michelangelo_bots.db import (
    BotBroadcast,
    BotEvent,
    BotMenuItem,
    BotOrder,
    BotSetting,
    BotUser,
    ChatMessage,
    OrderStatusNotification,
    datetime_now,
    get_session,
    get_session_factory,
    init_db,
)
from michelangelo_bots.inbound_notifications import notify_admins_about_incoming
from michelangelo_bots.rs_api import router as readyscript_router
from michelangelo_bots.telegram_webapp import verify_telegram_init_data
from michelangelo_bots.tracking import UserSnapshot, find_user, track_interaction
from michelangelo_bots.user_import import (
    MAX_IMPORT_BYTES,
    UserImportApplyResult,
    UserImportParseResult,
    import_user_records,
    parse_user_import,
)

security = HTTPBasic()
app = FastAPI(title="Michelangelo Bot Admin")
app.include_router(readyscript_router)


class ReadyScriptOrderPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    order_id: str | int | None = None
    id: str | int | None = None
    order_num: str | int | None = None
    number: str | int | None = None
    status: str | None = None
    total: str | int | float | None = None
    amount: str | int | float | None = None
    currency: str | None = None
    telegram_init_data: str | None = Field(default=None)
    telegram_user_id: str | int | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    customer_email: str | None = None
    user: dict[str, Any] | None = None


class SiteMessagePayload(BaseModel):
    client_id: str
    text: str | None = None
    photo_urls: list[str] = Field(default_factory=list)
    customer_name: str | None = None
    phone: str | None = None
    email: str | None = None
    reply_url: str | None = None


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    async with get_session_factory()() as session:
        await ensure_flows(session, get_settings())


def require_admin(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    username_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_readyscript_secret(
    x_readyscript_secret: Annotated[str | None, Header(alias="X-ReadyScript-Secret")],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.readyscript_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="READYSCRIPT_WEBHOOK_SECRET is not configured",
        )
    if not x_readyscript_secret or not secrets.compare_digest(
        x_readyscript_secret,
        settings.readyscript_webhook_secret,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid secret")


@app.post("/api/readyscript/orders")
async def receive_readyscript_order(
    payload: ReadyScriptOrderPayload,
    _: Annotated[None, Depends(require_readyscript_secret)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    order = await upsert_readyscript_order(session, payload, settings)
    return {
        "ok": True,
        "order_id": order.id,
        "external_order_id": order.external_order_id,
        "telegram_user_id": order.telegram_user_id,
        "bot_user_id": order.bot_user_id,
    }


@app.post("/api/site/messages")
async def receive_site_message(
    payload: SiteMessagePayload,
    _: Annotated[None, Depends(require_readyscript_secret)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if not (payload.text or payload.photo_urls):
        raise HTTPException(status_code=422, detail="text or photo_urls is required")
    user = await track_interaction(
        user=UserSnapshot(
            platform="site",
            platform_user_id=payload.client_id,
            chat_id=None,
            full_name=payload.customer_name,
            raw_profile={"reply_url": payload.reply_url} if payload.reply_url else None,
        ),
        action="site_message",
        event_type="message",
        message_text=payload.text,
        raw_update=payload.model_dump(),
    )
    if user is None:
        raise HTTPException(status_code=500, detail="Could not persist site message")
    user.phone = payload.phone or user.phone
    user.email = payload.email or user.email
    await session.merge(user)
    await session.commit()
    await notify_admins_about_incoming(
        settings=settings,
        source="site",
        user=user,
        message=payload.text or "📷 Клиент прислал фотографию",
        photo_urls=payload.photo_urls,
    )
    return {"ok": True, "user_id": user.id}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    total_users = await scalar(session, select(func.count(BotUser.id)))
    telegram_users = await scalar(
        session,
        select(func.count(BotUser.id)).where(BotUser.platform == "telegram"),
    )
    max_users = await scalar(
        session,
        select(func.count(BotUser.id)).where(BotUser.platform == "max"),
    )
    total_events = await scalar(session, select(func.count(BotEvent.id)))
    total_orders = await scalar(session, select(func.count(BotOrder.id)))
    bound_orders = await scalar(
        session,
        select(func.count(BotOrder.id)).where(BotOrder.platform_user_id.is_not(None)),
    )

    action_rows = (
        await session.execute(
            select(BotEvent.action, func.count(BotEvent.id))
            .group_by(BotEvent.action)
            .order_by(desc(func.count(BotEvent.id)))
            .limit(20)
        )
    ).all()
    recent_events = (
        await session.execute(
            select(BotEvent, BotUser)
            .join(BotUser, BotUser.id == BotEvent.user_id)
            .order_by(desc(BotEvent.occurred_at))
            .limit(30)
        )
    ).all()

    action_body = "".join(
        f"<tr><td>{e(action)}</td><td>{count}</td></tr>" for action, count in action_rows
    )
    return page(
        "Статистика",
        f"""
        <section class="stats">
          {stat_card("Всего пользователей", total_users)}
          {stat_card("Telegram", telegram_users)}
          {stat_card("MAX", max_users)}
          {stat_card("Действий в ботах", total_events)}
          {stat_card("Заказов", total_orders)}
          {stat_card("Заказов с привязкой", bound_orders)}
        </section>
        <section class="grid">
          <article>
            <h2>Популярные действия</h2>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Действие</th><th>Количество</th></tr></thead>
                <tbody>{action_body}</tbody>
              </table>
            </div>
          </article>
          <article>
            <h2>Последние события</h2>
            {events_table(recent_events)}
          </article>
        </section>
        """,
    )


@app.get("/clients", response_class=HTMLResponse)
async def clients(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    messenger: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> str:
    statement: Select[tuple[BotUser]] = select(BotUser)
    if messenger:
        statement = statement.where(BotUser.platform == messenger)
    if status_filter:
        statement = statement.where(BotUser.status == status_filter)
    if q:
        pattern = f"%{q}%"
        statement = statement.where(
            BotUser.username.ilike(pattern)
            | BotUser.full_name.ilike(pattern)
            | BotUser.phone.ilike(pattern)
            | BotUser.email.ilike(pattern)
            | BotUser.platform_user_id.ilike(pattern)
            | BotUser.chat_id.ilike(pattern)
        )

    result = await session.execute(statement.order_by(desc(BotUser.first_seen_at)).limit(limit))
    rows = "".join(client_row(user) for user in result.scalars().all())
    return page(
        "Список клиентов",
        f"""
        <div class="toolbar">
          <a class="button-link" href="/users/import">{icon("upload")} Перенести пользователей</a>
        </div>
        <form class="filters" method="get">
          <input name="q" value="{e(q)}" placeholder="Введите: Telegram / ФИО / Телефон / Email">
          <select name="messenger">
            <option value="">Все мессенджеры</option>
            <option value="telegram" {selected(messenger, "telegram")}>Telegram</option>
            <option value="max" {selected(messenger, "max")}>MAX</option>
          </select>
          <select name="status">
            <option value="">Все статусы</option>
            <option value="active" {selected(status_filter, "active")}>active</option>
            <option value="lead" {selected(status_filter, "lead")}>lead</option>
            <option value="blocked" {selected(status_filter, "blocked")}>blocked</option>
          </select>
          <input type="number" name="limit" value="{limit}" min="1" max="1000">
          <button>Фильтровать</button>
        </form>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Статус</th><th>Реферал</th><th>Комментарий</th>
                <th>ФИО</th><th>Телефон</th><th>Email</th><th>Дата и время регистрации</th>
                <th>Ник</th><th>Мессенджер</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
    )


@app.get("/broadcasts", response_class=HTMLResponse)
async def broadcasts(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    result = await session.execute(select(BotBroadcast).order_by(desc(BotBroadcast.created_at)))
    rows = "".join(broadcast_row(broadcast) for broadcast in result.scalars().all())
    return page(
        "Рассылка по клиентам",
        f"""
        <article>
          <h2>Создать рассылку</h2>
          <form class="broadcast-form" method="post" action="/broadcasts" enctype="multipart/form-data">
            <label>Наименование
              <input name="name" required maxlength="255" placeholder="Например: Июльская акция">
            </label>
            <label>Текст
              <textarea name="text" required rows="7" placeholder="Текст рассылки"></textarea>
            </label>
            <div class="form-grid">
              <label>Медиа
                <select name="media_type">
                  <option value="">Без медиа</option>
                  <option value="photo">Фото</option>
                  <option value="video">Видео</option>
                </select>
              </label>
              <label>URL фото/видео
                <input name="media_url" placeholder="https://...">
              </label>
            </div>
            <label>Загрузить файлы
              <input type="file" name="media_uploads" multiple accept="image/*,video/*">
            </label>
            <label class="checkbox">
              <input type="checkbox" name="include_miniapp_button" checked>
              <span>Добавить кнопку на миниапп</span>
            </label>
            <div class="actions">
              <button name="mode" value="draft">Сохранить черновик</button>
              <button name="mode" value="send">Создать и отправить</button>
            </div>
          </form>
        </article>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Наименование</th><th>Дата и время рассылки</th>
                <th>Статус</th><th>Дата создания</th><th>Успешно/всего</th><th>Ошибка</th>
                <th>Действие</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
    )


class FlowSave(BaseModel):
    flow: Flow
    revision: str


def flow_revision(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


@app.get("/bot-builder", response_class=HTMLResponse)
async def bot_builder_page(_: Annotated[str, Depends(require_admin)]) -> str:
    return page("Конструктор", Path(__file__).with_name("flow_editor.html").read_text())


@app.get("/api/bot-flow/{platform}")
async def get_bot_flow(
    platform: Platform,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    draft_key = flow_key(platform, "draft")
    draft = await session.get(BotSetting, draft_key)
    if draft is None:
        await ensure_flows(session, settings)
        draft = await session.get(BotSetting, draft_key)
    live = await session.get(BotSetting, flow_key(platform))
    return {
        "flow": json.loads(draft.value),
        "revision": flow_revision(draft.value),
        "published": live is not None and live.value == draft.value,
    }


@app.put("/api/bot-flow/{platform}")
async def save_bot_flow(
    platform: Platform,
    payload: FlowSave,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    publish: bool = False,
):
    draft = await session.get(BotSetting, flow_key(platform, "draft"))
    if draft is None or flow_revision(draft.value) != payload.revision:
        raise HTTPException(
            409, "Схему изменили в другом окне. Обновите страницу перед сохранением."
        )
    for node in payload.flow.nodes:
        if node.photo and not photo_path(settings.uploads_dir, node.photo).is_file():
            raise HTTPException(
                422, f"Фото блока «{node.title}» не найдено. Загрузите его повторно."
            )
    value = encoded_flow(payload.flow)
    result = await session.execute(
        update(BotSetting)
        .where(BotSetting.key == draft.key, BotSetting.value == draft.value)
        .values(value=value, updated_at=datetime_now())
    )
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "Схему изменили в другом окне. Обновите страницу.")
    if publish:
        await session.execute(
            insert(BotSetting)
            .values(key=flow_key(platform), value=value)
            .on_conflict_do_update(
                index_elements=["key"], set_={"value": value, "updated_at": datetime_now()}
            )
        )
    await session.commit()
    return {"revision": flow_revision(value), "published": publish}


@app.post("/api/bot-flow-photo")
async def upload_flow_photo(
    _: Annotated[str, Depends(require_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
    photo: Annotated[UploadFile, File()],
):
    data = await photo.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Фото должно быть не больше 10 МБ")
    suffix = (
        "jpg"
        if data.startswith(b"\xff\xd8\xff")
        else "png"
        if data.startswith(b"\x89PNG\r\n\x1a\n")
        else None
    )
    if suffix is None:
        raise HTTPException(422, "Загрузите фото в формате JPEG или PNG")
    filename = f"{uuid4().hex}.{suffix}"
    path = photo_path(settings.uploads_dir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"photo": filename}


@app.get("/media/flow/{filename}")
async def flow_photo(
    filename: str,
    _: Annotated[str, Depends(require_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    try:
        path = photo_path(settings.uploads_dir, filename)
    except ValueError as exc:
        raise HTTPException(404) from exc
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/bot-settings", response_class=HTMLResponse)
async def bot_settings_page(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    values = await all_settings(session)
    template_fields = "".join(
        f"<label>{e(label)}<textarea name=\"{e(key)}\" rows=\"{rows}\">"
        f"{e(values[key])}</textarea></label>"
        for key, label, rows in (
            ("admin_order_template", "Уведомление администраторам о заказе", 10),
            ("admin_incoming_template", "Входящее сообщение клиента", 9),
            ("customer_order_template", "Подтверждение заказа клиенту", 8),
            ("customer_status_template", "Изменение статуса СДЭК", 7),
            ("customer_cancel_button_text", "Текст кнопки отмены заказа", 2),
            ("customer_cancel_confirm_button_text", "Текст подтверждающей кнопки", 2),
            ("customer_cancel_abort_button_text", "Текст кнопки отказа от отмены", 2),
            ("customer_cancel_confirm_text", "Подтверждение отмены заказа", 4),
            ("customer_cancel_success_text", "Успешная отмена заказа", 4),
            ("customer_cancel_failure_text", "Ошибка автоматической отмены", 4),
            ("customer_cancel_too_late_text", "Заказ уже нельзя отменить", 4),
            ("incoming_ack_text", "Ответ клиенту после обращения", 4),
            ("notification_failure_template", "Системный сбой доставки уведомлений", 9),
            ("notification_recovery_template", "Восстановление доставки уведомлений", 4),
            ("sync_failure_template", "Сбой синхронизации ReadyScript/СДЭК", 7),
            ("sync_recovery_template", "Восстановление синхронизации ReadyScript/СДЭК", 4),
            ("telegram_manager_url", "Ссылка на менеджера в Telegram", 2),
            ("max_manager_url", "Ссылка на менеджера в MAX", 2),
        )
    )
    return page(
        "Настройки ботов",
        f"""
        <article>
          <h2>Шаблоны сообщений</h2>
          <p>Меню и приветствие после переноса редактируются в <a href="/bot-builder">конструкторе бота</a>. Здесь остаются настройки уведомлений.</p>
          <p class="muted">Доступные переменные: {{order_number}}, {{source}}, {{status}},
          {{status_title}}, {{amount}}, {{customer_name}}, {{customer_phone}},
          {{customer_email}}, {{username}}, {{platform_user_id}}, {{items}}, {{message}}.</p>
          <form class="settings-form" method="post" action="/bot-settings/templates">
            {template_fields}
            <button>Сохранить шаблоны</button>
          </form>
        </article>
        """,
    )


@app.post("/bot-settings/templates")
async def save_bot_templates(
    request: Request,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    form = await request.form()
    for key in DEFAULT_SETTINGS:
        if key not in form:
            continue
        value = str(form[key]).strip()
        row = await session.get(BotSetting, key)
        if row is None:
            session.add(BotSetting(key=key, value=value))
        else:
            row.value = value
            row.updated_at = datetime_now()
    await session.commit()
    return RedirectResponse("/bot-settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/bot-settings/menu")
async def create_menu_item(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    title: Annotated[str, Form()],
    platform: Annotated[str, Form()] = "all",
    kind: Annotated[str, Form()] = "message",
    body: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    position: Annotated[int, Form()] = 100,
) -> RedirectResponse:
    validate_menu_item(platform, kind, title, url)
    session.add(
        BotMenuItem(
            title=title.strip(), platform=platform, kind=kind, body=body.strip() or None,
            url=url.strip() or None, position=position, active=True,
        )
    )
    await session.commit()
    return RedirectResponse("/bot-settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/bot-settings/menu/{item_id}")
async def update_menu_item(
    item_id: int,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    title: Annotated[str, Form()],
    platform: Annotated[str, Form()] = "all",
    kind: Annotated[str, Form()] = "message",
    body: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    position: Annotated[int, Form()] = 100,
    active: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "save",
) -> RedirectResponse:
    item = await session.get(BotMenuItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Menu item not found")
    if mode == "delete":
        await session.delete(item)
    else:
        validate_menu_item(platform, kind, title, url)
        item.title = title.strip()
        item.platform = platform
        item.kind = kind
        item.body = body.strip() or None
        item.url = url.strip() or None
        item.position = position
        item.active = active == "on"
    await session.commit()
    return RedirectResponse("/bot-settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/broadcasts", response_class=HTMLResponse)
async def create_broadcast(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    name: Annotated[str, Form()],
    text_value: Annotated[str, Form(alias="text")],
    media_type: Annotated[str, Form()] = "",
    media_url: Annotated[str, Form()] = "",
    include_miniapp_button: Annotated[bool, Form()] = False,
    mode: Annotated[str, Form()] = "draft",
    media_uploads: Annotated[list[UploadFile] | None, File()] = None,
) -> RedirectResponse:
    media_files = await save_broadcast_uploads(media_uploads or [], settings.uploads_dir)
    broadcast = BotBroadcast(
        name=name,
        text=text_value,
        media_type=resolve_media_type(media_type, media_files) or None,
        media_url=media_url or None,
        media_files=media_files or None,
        include_miniapp_button=include_miniapp_button,
        status="draft",
    )
    session.add(broadcast)
    await session.commit()
    await session.refresh(broadcast)

    if mode == "send":
        await send_broadcast(session, broadcast=broadcast, settings=settings)

    return RedirectResponse("/broadcasts", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/broadcasts/{broadcast_id}/send", response_class=HTMLResponse)
async def send_existing_broadcast(
    broadcast_id: int,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    broadcast = await session.get(BotBroadcast, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=404, detail="Broadcast not found")
    await send_broadcast(session, broadcast=broadcast, settings=settings)
    return RedirectResponse("/broadcasts", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/client-paths", response_class=HTMLResponse)
async def client_paths(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    limit: int = Query(default=300, ge=1, le=1000),
) -> str:
    statement = select(BotEvent, BotUser).join(BotUser, BotUser.id == BotEvent.user_id)
    if q:
        pattern = f"%{q}%"
        statement = statement.where(
            BotUser.username.ilike(pattern)
            | BotUser.full_name.ilike(pattern)
            | BotUser.platform_user_id.ilike(pattern)
            | BotEvent.action.ilike(pattern)
        )
    result = await session.execute(statement.order_by(desc(BotEvent.occurred_at)).limit(limit))
    rows = "".join(client_path_row(event, user) for event, user in result.all())
    return page(
        "Путь клиента",
        f"""
        <form class="filters" method="get">
          <input name="q" value="{e(q)}" placeholder="Поиск по клиенту или действию">
          <input type="number" name="limit" value="{limit}" min="1" max="1000">
          <button>Найти</button>
        </form>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Путь клиента</th><th>Клиент</th>
                <th>Последнее действие</th><th>Дата и время действия</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
    )


@app.get("/orders", response_class=HTMLResponse)
async def orders(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    platform: str | None = Query(default=None),
    linked: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> str:
    statement: Select[tuple[BotOrder, BotUser | None]] = (
        select(BotOrder, BotUser).outerjoin(BotUser, BotUser.id == BotOrder.bot_user_id)
    )
    if status_filter:
        statement = statement.where(BotOrder.status == status_filter)
    if platform:
        statement = statement.where(BotOrder.platform == platform)
    if linked == "yes":
        statement = statement.where(BotOrder.bot_user_id.is_not(None))
    elif linked == "no":
        statement = statement.where(BotOrder.bot_user_id.is_(None))
    if q:
        pattern = f"%{q}%"
        statement = statement.where(
            BotOrder.external_order_id.ilike(pattern)
            | BotOrder.external_order_number.ilike(pattern)
            | BotOrder.telegram_user_id.ilike(pattern)
            | BotOrder.platform_user_id.ilike(pattern)
            | BotOrder.customer_name.ilike(pattern)
            | BotOrder.customer_phone.ilike(pattern)
            | BotOrder.customer_email.ilike(pattern)
        )
    statuses = (
        await session.execute(
            select(BotOrder.status).where(BotOrder.status.is_not(None)).distinct()
        )
    ).scalars()
    status_options = "".join(
        f'<option value="{e(status_value)}" {selected(status_filter, status_value)}>'
        f"{e(status_value)}</option>"
        for status_value in sorted(value for value in statuses if value)
    )
    result = await session.execute(statement.order_by(desc(BotOrder.updated_at)).limit(limit))
    rows = "".join(order_row(order, user) for order, user in result.all())
    return page(
        "Заказы",
        f"""
        <article>
          <h2>Заказы ReadyScript</h2>
          <p class="muted">Заказы синхронизируются автоматически фоновым сервисом раз в минуту.</p>
        </article>
        <form class="filters" method="get">
          <input name="q" value="{e(q)}" placeholder="Поиск: заказ, ФИО, телефон, email, Telegram ID">
          <select name="status">
            <option value="">Все статусы</option>
            {status_options}
          </select>
          <select name="platform">
            <option value="">Все платформы</option>
            <option value="telegram" {selected(platform, "telegram")}>Telegram</option>
            <option value="max" {selected(platform, "max")}>MAX</option>
          </select>
          <select name="linked">
            <option value="">Все привязки</option>
            <option value="yes" {selected(linked, "yes")}>Привязанные</option>
            <option value="no" {selected(linked, "no")}>Без пользователя</option>
          </select>
          <input type="number" name="limit" value="{limit}" min="1" max="1000">
          <button>Фильтровать</button>
        </form>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Номер</th><th>Товары</th><th>Сумма</th><th>Клиент</th><th>Телефон</th><th>Email</th>
                <th>Привязка</th><th>Пользователь</th><th>Обновлён</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
    )


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(
    order_id: int,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    row = (
        await session.execute(
            select(BotOrder, BotUser)
            .outerjoin(BotUser, BotUser.id == BotOrder.bot_user_id)
            .where(BotOrder.id == order_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Order not found")
    order, user = row
    status_notifications = list(
        (
            await session.execute(
                select(OrderStatusNotification)
                .where(OrderStatusNotification.order_id == order.id)
                .order_by(OrderStatusNotification.created_at)
            )
        ).scalars()
    )
    status_notification_rows = "".join(
        order_status_notification_row(notification)
        for notification in status_notifications
    )

    if user is not None:
        chat_button = (
            f'<a class="button-link" href="/chats/{user.id}?order={order.id}">'
            f'{icon("chat")} Перейти в чат с клиентом</a>'
            f'<a class="button-link secondary" href="/users/{user.id}">'
            f'{icon("user")} Профиль пользователя</a>'
        )
    else:
        chat_button = (
            '<span class="muted">Заказ не привязан к пользователю бота — '
            "чат недоступен</span>"
        )

    return page(
        f"Заказ №{order.external_order_number or order.external_order_id}",
        f"""
        <div class="actions toolbar">{chat_button}</div>
        <section class="profile">
          <dl>
            {field("Номер", order.external_order_number or order.external_order_id)}
            {field("Статус", order.status)}
            {field("Сумма", f"{order.total_amount or ''} {order.currency or ''}".strip())}
            {field("Клиент", order.customer_name)}
            {field("Телефон", order.customer_phone)}
            {field("Email", order.customer_email)}
            {field("Платформа", order.platform)}
            {field("Messenger user id", order.platform_user_id or order.telegram_user_id)}
            {field("Источник привязки", BIND_SOURCE_LABELS.get(order.bind_source or "", order.bind_source))}
            {field("Подтверждение клиенту", "доставлено" if order.customer_notified_at else "ожидает доставки")}
            {field("Попыток подтверждения", order.customer_notification_attempts or 0)}
            {field("Последняя попытка", format_dt(order.customer_notification_last_attempt_at))}
            {field("Ошибка подтверждения", order.customer_notification_error)}
            {field("Создан", format_dt(order.created_at))}
            {field("Обновлён", format_dt(order.updated_at))}
          </dl>
        </section>
        <article>
          <h2>Состав заказа</h2>
          <div>{order_items_summary(order) or '<span class="muted">Нет данных о товарах</span>'}</div>
        </article>
        <article>
          <h2>Уведомления о статусах СДЭК</h2>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Статус</th><th>Результат</th><th>Попыток</th><th>Последняя попытка</th><th>Ошибка</th></tr></thead>
              <tbody>{status_notification_rows or '<tr><td colspan="5" class="muted">Изменений статуса пока не было</td></tr>'}</tbody>
            </table>
          </div>
        </article>
        """,
    )


@app.get("/chats", response_class=HTMLResponse)
async def chats(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> str:
    last_outgoing = (
        select(
            ChatMessage.user_id.label("user_id"),
            func.max(ChatMessage.created_at).label("last_outgoing_at"),
        )
        .group_by(ChatMessage.user_id)
        .subquery()
    )
    statement = select(BotUser, last_outgoing.c.last_outgoing_at).outerjoin(
        last_outgoing,
        last_outgoing.c.user_id == BotUser.id,
    )
    if platform:
        statement = statement.where(BotUser.platform == platform)
    if q:
        pattern = f"%{q}%"
        statement = statement.where(
            BotUser.username.ilike(pattern)
            | BotUser.full_name.ilike(pattern)
            | BotUser.phone.ilike(pattern)
            | BotUser.email.ilike(pattern)
            | BotUser.platform_user_id.ilike(pattern)
            | BotUser.chat_id.ilike(pattern)
        )
    result = await session.execute(statement.order_by(desc(BotUser.last_seen_at)).limit(limit))
    rows = "".join(chat_row(user, last_at) for user, last_at in result.all())
    return page(
        "Чаты",
        f"""
        <form class="filters" method="get">
          <input name="q" value="{e(q)}" placeholder="Поиск: username, имя, телефон, id">
          <select name="platform">
            <option value="">Все платформы</option>
            <option value="telegram" {selected(platform, "telegram")}>Telegram</option>
            <option value="max" {selected(platform, "max")}>MAX</option>
          </select>
          <input type="number" name="limit" value="{limit}" min="1" max="1000">
          <button>Фильтровать</button>
        </form>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Клиент</th><th>Платформа</th><th>Последнее сообщение</th>
                <th>Ответ администратора</th><th>Последний контакт</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
    )


@app.get("/chats/{user_id}", response_class=HTMLResponse)
async def chat_detail(
    user_id: int,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    order: int | None = Query(default=None),
    sent: str | None = Query(default=None),
) -> str:
    user = await find_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    thread = await load_thread(session, user)
    order_context = ""
    order_field = ""
    if order is not None:
        bot_order = (
            await session.execute(select(BotOrder).where(BotOrder.id == order))
        ).scalar_one_or_none()
        if bot_order is not None:
            number = bot_order.external_order_number or bot_order.external_order_id
            order_context = (
                f'<p class="muted">Контекст: заказ '
                f'<a href="/orders/{bot_order.id}">№{e(number)}</a></p>'
            )
            order_field = f'<input type="hidden" name="order_id" value="{bot_order.id}">'

    notice = ""
    if sent == "ok":
        notice = '<p class="notice success">Сообщение отправлено</p>'
    elif sent == "empty":
        notice = '<p class="notice danger">Нужен текст или вложение</p>'
    elif sent == "failed":
        notice = (
            '<p class="notice danger">Сообщение не доставлено — '
            "подробности в истории диалога</p>"
        )

    can_write = bool(recipient_id(user))
    form = (
        f"""
        <form class="chat-form" method="post" action="/chats/{user.id}"
              enctype="multipart/form-data">
          {order_field}
          <textarea name="text" rows="3" placeholder="Сообщение клиенту"></textarea>
          <label class="chat-attach">
            {icon("image")} Фото или видео
            <input type="file" name="attachments" multiple accept="image/*,video/*">
          </label>
          <button>Отправить</button>
        </form>
        """
        if can_write
        else '<p class="muted">У клиента нет chat_id — бот не может написать первым.</p>'
    )

    return page(
        f"Чат с {client_label(user)}",
        f"""
        <div class="actions toolbar">
          <a class="button-link secondary" href="/users/{user.id}">{icon("user")} Профиль</a>
          <a class="button-link secondary" href="/orders?q={e(user.platform_user_id)}">{icon("bag")} Заказы клиента</a>
        </div>
        {order_context}
        {notice}
        <article class="chat">
          <div class="chat-thread">{chat_thread_html(thread)}</div>
          {form}
        </article>
        """,
    )


@app.post("/chats/{user_id}", response_class=HTMLResponse)
async def post_chat_message(
    user_id: int,
    admin: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    text: Annotated[str, Form()] = "",
    order_id: Annotated[int | None, Form()] = None,
    attachments: Annotated[list[UploadFile] | None, File()] = None,
) -> RedirectResponse:
    user = await find_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    message_text = text.strip()
    media_files = await save_media_uploads(attachments or [], settings.uploads_dir, "chats")
    if not message_text and not media_files:
        query = "?sent=empty" + (f"&order={order_id}" if order_id else "")
        return RedirectResponse(
            f"/chats/{user.id}{query}", status_code=status.HTTP_303_SEE_OTHER
        )

    message = await send_admin_message(
        session,
        user=user,
        text=message_text,
        settings=settings,
        author=admin,
        order_id=order_id,
        attachments=media_files,
    )
    query = f"?sent={'ok' if message.status == 'sent' else 'failed'}"
    if order_id:
        query += f"&order={order_id}"
    return RedirectResponse(f"/chats/{user.id}{query}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/media/chat/{message_id}/{index}")
async def chat_attachment_file(
    message_id: int,
    index: int,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FileResponse:
    """Отдаёт вложение исходящего сообщения из uploads_dir по записи в БД.

    Путь берётся только из сохранённой записи, произвольные файлы через этот
    эндпоинт не читаются.
    """

    message = await session.get(ChatMessage, message_id)
    stored = (message.attachments or []) if message else []
    if not stored or index < 0 or index >= len(stored):
        raise HTTPException(status_code=404, detail="Attachment not found")

    media_file = stored[index]
    path = Path(media_file["path"])
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(status_code=404, detail="Attachment file is missing")
    return FileResponse(
        path,
        media_type=media_file.get("content_type") or "application/octet-stream",
        filename=media_file.get("filename") or path.name,
        content_disposition_type="inline",
    )


@app.get("/media/telegram/{file_id}")
async def telegram_attachment_file(
    file_id: str,
    _: Annotated[str, Depends(require_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    """Проксирует медиа клиента из Telegram: прямая ссылка содержит токен бота."""

    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN is not set")

    client = httpx.AsyncClient(timeout=60)
    try:
        info = await client.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile",
            params={"file_id": file_id},
        )
        if info.is_error:
            raise HTTPException(status_code=404, detail="Telegram file is unavailable")
        file_path = (info.json().get("result") or {}).get("file_path")
        if not file_path:
            raise HTTPException(status_code=404, detail="Telegram file is unavailable")

        request = client.build_request(
            "GET",
            f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}",
        )
        upstream = await client.send(request, stream=True)
        if upstream.is_error:
            await upstream.aclose()
            raise HTTPException(status_code=404, detail="Telegram file is unavailable")
    except HTTPException:
        await client.aclose()
        raise
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Telegram file proxy failed: {exc}") from exc

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/users", response_class=HTMLResponse)
async def users(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    platform: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> str:
    statement: Select[tuple[BotUser]] = select(BotUser)
    if platform:
        statement = statement.where(BotUser.platform == platform)
    if q:
        pattern = f"%{q}%"
        statement = statement.where(
            BotUser.username.ilike(pattern)
            | BotUser.full_name.ilike(pattern)
            | BotUser.phone.ilike(pattern)
            | BotUser.email.ilike(pattern)
            | BotUser.platform_user_id.ilike(pattern)
            | BotUser.chat_id.ilike(pattern)
        )
    result = await session.execute(statement.order_by(desc(BotUser.last_seen_at)).limit(limit))
    rows = "".join(user_row(user) for user in result.scalars().all())
    return page(
        "Пользователи",
        f"""
        <div class="toolbar">
          <a class="button-link" href="/users/import">{icon("upload")} Перенести пользователей</a>
        </div>
        <form class="filters" method="get">
          <input name="q" value="{e(q)}" placeholder="Поиск: username, имя, id, chat_id">
          <select name="platform">
            <option value="">Все платформы</option>
            <option value="telegram" {selected(platform, "telegram")}>Telegram</option>
            <option value="max" {selected(platform, "max")}>MAX</option>
          </select>
          <input type="number" name="limit" value="{limit}" min="1" max="500">
          <button>Фильтровать</button>
        </form>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Платформа</th><th>User ID</th><th>Username</th>
                <th>Имя</th><th>Действий</th><th>Последнее действие</th><th>Последний контакт</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
    )


@app.get("/users/import", response_class=HTMLResponse)
async def user_import_page(
    _: Annotated[str, Depends(require_admin)],
) -> str:
    return page("Импорт пользователей", user_import_form())


@app.get("/users/import/template")
async def user_import_template(
    _: Annotated[str, Depends(require_admin)],
) -> Response:
    header = (
        "platform,platform_user_id,chat_id,username,first_name,last_name,full_name,"
        "phone,email,status,referral,comment,first_seen_at,last_seen_at,total_actions\r\n"
    )
    return Response(
        content="\ufeff" + header,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="users-import-template.csv"'},
    )


@app.post("/users/import", response_class=HTMLResponse)
async def import_users_from_file(
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user_file: Annotated[UploadFile, File()],
    operation: Annotated[str, Form()] = "preview",
    default_platform: Annotated[str, Form()] = "",
    existing_mode: Annotated[str, Form()] = "merge",
    id_is_platform_user_id: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    if operation not in {"preview", "import"}:
        raise HTTPException(status_code=422, detail="Invalid import operation")
    if default_platform not in {"", "telegram", "max"}:
        raise HTTPException(status_code=422, detail="Invalid default platform")
    if existing_mode not in {"merge", "overwrite", "skip"}:
        raise HTTPException(status_code=422, detail="Invalid existing user mode")
    content = await user_file.read(MAX_IMPORT_BYTES + 1)
    try:
        parsed = parse_user_import(
            user_file.filename or "users.csv",
            content,
            default_platform=default_platform or None,
            id_is_platform_user_id=id_is_platform_user_id is not None,
        )
        applied = await import_user_records(
            session,
            parsed.records,
            apply=operation == "import",
            existing_mode=existing_mode,
            source_filename=parsed.filename,
        )
    except ValueError as exc:
        return HTMLResponse(
            page(
                "Импорт пользователей",
                f'<div class="notice danger">{e(exc)}</div>{user_import_form()}',
            ),
            status_code=422,
        )
    return HTMLResponse(
        page(
            "Импорт пользователей",
            user_import_report(parsed, applied, imported=operation == "import")
            + user_import_form(),
        )
    )


@app.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(
    user_id: int,
    _: Annotated[str, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    user = await find_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    events = (
        await session.execute(
            select(BotEvent, BotUser)
            .join(BotUser, BotUser.id == BotEvent.user_id)
            .where(BotEvent.user_id == user.id)
            .order_by(desc(BotEvent.occurred_at))
            .limit(200)
        )
    ).all()
    orders_result = await session.execute(
        select(BotOrder)
        .where(BotOrder.bot_user_id == user.id)
        .order_by(desc(BotOrder.updated_at))
        .limit(100)
    )
    user_orders = orders_result.scalars().all()
    order_rows = "".join(user_order_row(order) for order in user_orders)

    return page(
        f"Пользователь #{user.id}",
        f"""
        <section class="profile">
          <dl>
            {field("Платформа", user.platform)}
            {field("Platform user id", user.platform_user_id)}
            {field("Chat id", user.chat_id)}
            {field("Username", user.username)}
            {field("Телефон", user.phone)}
            {field("Email", user.email)}
            {field("Статус", user.status)}
            {field("Реферал", user.referral)}
            {field("Комментарий", user.comment)}
            {field("Имя", user.full_name)}
            {field("Язык", user.language_code)}
            {field("Первый контакт", format_dt(user.first_seen_at))}
            {field("Последний контакт", format_dt(user.last_seen_at))}
            {field("Всего действий", str(user.total_actions))}
            {field("Последнее действие", user.last_action)}
            {field("Последнее сообщение", user.last_message_text)}
          </dl>
        </section>
        <h2>История действий</h2>
        {events_table(events)}
        <h2>Заказы</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Номер</th><th>Товары</th><th>Сумма</th><th>Статус</th><th>Обновлён</th>
              </tr>
            </thead>
            <tbody>{order_rows}</tbody>
          </table>
        </div>
        """,
    )


async def scalar(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    result = await session.execute(statement)
    return int(result.scalar_one() or 0)


def events_table(rows: list[tuple[BotEvent, BotUser]]) -> str:
    body = "".join(
        f"""
        <tr>
          <td>{format_dt(event.occurred_at)}</td>
          <td>{e(event.platform)}</td>
          <td><a href="/users/{user.id}">{e(client_label(user))}</a></td>
          <td>{e(event.event_type)}</td>
          <td>{e(event.action)}</td>
          <td>{e(event.message_text or event.callback_payload)}</td>
        </tr>
        """
        for event, user in rows
    )
    return f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Время</th><th>Платформа</th><th>Пользователь</th>
            <th>Тип</th><th>Действие</th><th>Текст / payload</th>
          </tr>
        </thead>
        <tbody>{body}</tbody>
      </table>
    </div>
    """


def page(title: str, body: str) -> str:
    page_meta = {
        "Статистика": ("Обзор", "Главные показатели и последние действия пользователей"),
        "Список клиентов": ("Клиенты", "Единая база клиентов из Telegram и MAX"),
        "Заказы": ("Заказы", "Заказы и связь с профилями пользователей"),
        "Чаты": ("Чаты", "Переписка с клиентами в Telegram и MAX"),
        "Рассылка по клиентам": ("Рассылки", "Создание и история сообщений для клиентов"),
        "Путь клиента": ("Путь клиента", "Хронология взаимодействий пользователей с ботами"),
        "Пользователи": ("Пользователи", "Аккаунты, активность и данные пользователей"),
        "Импорт пользователей": (
            "Импорт пользователей",
            "Безопасный перенос клиентской базы из старой админки",
        ),
        "Конструктор": ("Конструктор", "Сценарии сообщений и переходов Telegram/MAX"),
        "Настройки ботов": ("Настройки", "Тексты, уведомления и меню Telegram/MAX"),
    }
    default_meta = ("Пользователи", "Профиль, заказы и история взаимодействий пользователя")
    if title.startswith("Чат с "):
        default_meta = ("Чаты", "Переписка с клиентом и отправка сообщений из админки")
    elif title.startswith("Заказ №"):
        default_meta = ("Заказы", "Карточка заказа, привязка к клиенту и переход в чат")
    section, subtitle = page_meta.get(title, default_meta)
    navigation = "".join(
        nav_item(label, href, icon_name, section == label)
        for label, href, icon_name in (
            ("Обзор", "/", "grid"),
            ("Клиенты", "/clients", "users"),
            ("Заказы", "/orders", "bag"),
            ("Чаты", "/chats", "chat"),
            ("Рассылки", "/broadcasts", "mail"),
            ("Путь клиента", "/client-paths", "route"),
            ("Пользователи", "/users", "user"),
            ("Конструктор", "/bot-builder", "route"),
            ("Настройки", "/bot-settings", "settings"),
        )
    )
    return f"""
    <!doctype html>
    <html lang="ru">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{e(title)} | Michelangelo Admin</title>
        <style>
          :root {{ --bg: #fafbff; --panel: #fff; --ink: #151932; --muted: #737893; --line: #e5e8f4; --accent: #5364ff; --accent-soft: #f2f3ff; --success: #20a46b; --warning: #e59b2f; --danger: #e45d6d; --radius: 12px; --sidebar: 276px; }}
          * {{ box-sizing: border-box; }}
          html {{ min-width: 320px; }}
          body {{ margin: 0; font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); -webkit-font-smoothing: antialiased; }}
          body::before {{ content: ""; position: fixed; inset: 0 0 auto; height: 3px; z-index: 20; background: linear-gradient(90deg,#62be91,#74c99d 36%,#8bd1aa); }}
          a {{ color: var(--accent); text-decoration: none; }}
          .icon {{ width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: 0 0 auto; }}
          .sidebar {{ position: fixed; inset: 3px auto 0 0; width: var(--sidebar); background: #fff; border-right: 1px solid var(--line); z-index: 10; display: flex; flex-direction: column; }}
          .brand {{ height: 68px; display: flex; align-items: center; padding: 0 20px; border-bottom: 1px solid var(--line); color: var(--accent); font-size: 20px; font-weight: 800; letter-spacing: -.02em; }}
          .brand-mark {{ width: 34px; height: 34px; margin-right: 10px; border-radius: 10px; display: grid; place-items: center; color: white; background: linear-gradient(145deg,#6d79ff,#4c5cff); box-shadow: 0 6px 15px rgba(83,100,255,.25); }}
          .brand-mark .icon {{ width: 19px; }}
          .nav {{ padding: 20px 12px; display: grid; gap: 5px; }}
          .nav-label {{ padding: 0 12px 7px; color: #a0a5b9; font-size: 10px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }}
          .nav a {{ display: flex; align-items: center; gap: 13px; min-height: 44px; padding: 0 13px; color: #5e637d; border-radius: 9px; font-weight: 650; transition: .18s ease; }}
          .nav a:hover {{ color: var(--accent); background: #f7f7ff; }}
          .nav a.active {{ color: var(--accent); background: var(--accent-soft); }}
          .nav a.active::before {{ content: ""; width: 3px; height: 22px; margin-left: -13px; margin-right: -3px; border-radius: 0 4px 4px 0; background: var(--accent); }}
          .sidebar-foot {{ margin-top: auto; padding: 18px 24px 24px; border-top: 1px solid #f0f1f7; }}
          .sidebar-foot a {{ display: flex; align-items: center; gap: 10px; color: var(--accent); font-weight: 700; }}
          .workspace {{ min-height: 100vh; margin-left: var(--sidebar); }}
          .topbar {{ height: 68px; padding: 0 28px; background: #fff; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
          .breadcrumbs {{ display: flex; align-items: center; gap: 11px; min-width: 0; color: #7d829d; font-weight: 650; }}
          .breadcrumbs .home {{ color: #727893; display: grid; place-items: center; }}
          .breadcrumbs .separator {{ color: #bdc1d0; }}
          .breadcrumbs strong {{ color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
          .top-actions {{ display: flex; align-items: center; gap: 14px; }}
          .credit {{ color: var(--accent); font-weight: 750; }}
          .avatar {{ width: 36px; height: 36px; border-radius: 50%; background: #f1f2f9; color: #858aa3; display: grid; place-items: center; font-weight: 800; }}
          .mobile-menu {{ display: none; padding: 6px; color: #6d728c; background: none; border: 0; }}
          main {{ padding: 30px clamp(22px,4vw,64px) 60px; max-width: 1680px; margin: 0 auto; }}
          .page-heading {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 24px; }}
          h1 {{ margin: 0; font-size: 28px; line-height: 1.2; letter-spacing: -.025em; }}
          .page-heading p {{ margin: 7px 0 0; color: var(--muted); }}
          h2 {{ margin: 0 0 14px; font-size: 17px; letter-spacing: -.01em; }}
          .stats {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-bottom: 22px; }}
          .stat {{ position: relative; overflow: hidden; min-height: 128px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 21px 22px; box-shadow: 0 3px 12px rgba(36,44,92,.035); }}
          .stat::after {{ content: ""; position: absolute; width: 76px; height: 76px; right: -22px; bottom: -28px; border-radius: 50%; background: var(--accent-soft); }}
          .stat span {{ color: var(--muted); font-weight: 600; }}
          .stat strong {{ display: block; font-size: 32px; line-height: 1; margin-top: 15px; letter-spacing: -.04em; }}
          .grid {{ display: grid; grid-template-columns: minmax(290px, .7fr) minmax(500px, 1.5fr); gap: 20px; align-items: start; }}
          article, .profile {{ background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 22px; margin-bottom: 20px; box-shadow: 0 3px 12px rgba(36,44,92,.035); }}
          .table-wrap {{ overflow: auto; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: 0 3px 12px rgba(36,44,92,.035); }}
          table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: transparent; }}
          th, td {{ padding: 14px 15px; border-bottom: 1px solid #eef0f6; text-align: left; vertical-align: middle; white-space: nowrap; }}
          td {{ color: #4d526b; }}
          th {{ background: #fafaff; font-size: 11px; text-transform: uppercase; letter-spacing: .055em; color: #898ea5; font-weight: 750; }}
          th:first-child {{ border-top-left-radius: var(--radius); }}
          th:last-child {{ border-top-right-radius: var(--radius); }}
          tr:last-child td {{ border-bottom: 0; }}
          tbody tr {{ transition: background .15s ease; }}
          tbody tr:hover {{ background: #fbfbff; }}
          td a {{ font-weight: 700; }}
          .filters {{ display: flex; gap: 10px; margin-bottom: 16px; padding: 14px; flex-wrap: wrap; background: white; border: 1px solid var(--line); border-radius: var(--radius); }}
          input, select, textarea, button {{ padding: 10px 12px; border: 1px solid #daddE9; border-radius: 8px; background: white; color: var(--ink); font: inherit; outline: none; transition: border .16s,box-shadow .16s,background .16s; }}
          input:focus, select:focus, textarea:focus {{ border-color: #8f9aff; box-shadow: 0 0 0 3px rgba(83,100,255,.1); }}
          input, select {{ min-height: 42px; }}
          .filters input[name="q"] {{ flex: 1 1 300px; }}
          textarea {{ resize: vertical; width: 100%; }}
          button {{ min-height: 42px; background: var(--accent); color: white; cursor: pointer; border-color: var(--accent); font-weight: 700; box-shadow: 0 5px 12px rgba(83,100,255,.16); }}
          button:hover {{ background: #4657ee; }}
          button.secondary {{ background: var(--accent-soft); color: var(--accent); border-color: #dfe2ff; box-shadow: none; }}
          label {{ display: grid; gap: 7px; color: #5d6279; font-weight: 700; }}
          label input, label select, label textarea {{ font-weight: 400; }}
          .broadcast-form {{ display: grid; gap: 14px; max-width: 860px; }}
          .settings-form, .menu-list {{ display: grid; gap: 16px; }}
          .menu-editor {{ display: grid; grid-template-columns: 120px 130px minmax(180px,1fr) 90px; gap: 12px; align-items: end; }}
          .menu-editor .wide {{ grid-column: 1 / -1; }}
          .menu-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; }}
          .form-grid {{ display: grid; grid-template-columns: 220px 1fr; gap: 12px; }}
          .checkbox {{ display: flex; align-items: center; gap: 8px; }}
          .checkbox input {{ min-height: auto; }}
          .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
          dl {{ display: grid; grid-template-columns: 220px 1fr; gap: 0; margin: 0; }}
          dt, dd {{ padding: 11px 0; border-bottom: 1px solid #eff0f6; }}
          dt {{ color: #858aa0; font-weight: 600; }}
          dd {{ margin: 0; color: var(--ink); font-weight: 600; }}
          pre {{ overflow: auto; background: #f1f3f5; padding: 12px; border-radius: 14px; }}
          code {{ background: #eef2f5; padding: 2px 6px; border-radius: 8px; }}
          .muted {{ color: var(--muted); }}
          tbody tr[data-href] {{ cursor: pointer; }}
          .toolbar {{ margin-bottom: 18px; }}
          .button-link {{ display: inline-flex; align-items: center; gap: 8px; min-height: 42px; padding: 0 15px; border-radius: 8px; background: var(--accent); color: #fff; font-weight: 700; box-shadow: 0 5px 12px rgba(83,100,255,.16); }}
          .button-link:hover {{ background: #4657ee; }}
          .button-link.secondary {{ background: var(--accent-soft); color: var(--accent); box-shadow: none; }}
          .notice {{ margin: 0 0 16px; padding: 11px 14px; border-radius: 10px; font-weight: 650; }}
          .notice.success {{ color: var(--success); background: #ebfaf3; }}
          .notice.danger {{ color: var(--danger); background: #fff0f2; }}
          .chat {{ display: grid; gap: 16px; }}
          .chat-thread {{ display: grid; gap: 10px; max-height: 60vh; overflow: auto; padding: 4px; }}
          .bubble {{ max-width: min(620px, 82%); padding: 10px 13px; border-radius: 14px; background: #f4f5fb; }}
          .bubble.out {{ justify-self: end; background: var(--accent-soft); }}
          .bubble.failed {{ background: #fff0f2; }}
          .bubble-text {{ white-space: pre-wrap; word-break: break-word; }}
          .bubble-meta {{ margin-top: 5px; color: var(--muted); font-size: 12px; }}
          .bubble-media-list {{ display: grid; gap: 8px; margin-bottom: 8px; }}
          .bubble-media {{ display: block; max-width: 100%; max-height: 340px; border-radius: 10px; background: #e9ebf3; }}
          .bubble-file {{ display: inline-block; font-weight: 700; word-break: break-all; }}
          .chat-form {{ display: grid; gap: 10px; justify-items: start; width: 100%; }}
          .chat-form textarea {{ width: 100%; }}
          .chat-attach {{ display: inline-flex; align-items: center; gap: 8px; color: var(--accent); font-weight: 650; }}
          .chat-attach input {{ font-weight: 400; color: var(--muted); }}
          .badge {{ display: inline-flex; align-items: center; gap: 6px; min-height: 25px; padding: 3px 9px; border-radius: 999px; background: #f1f2f8; color: #656a80; font-size: 12px; font-weight: 700; }}
          .badge::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
          .badge.active, .badge.sent {{ color: var(--success); background: #ebfaf3; }}
          .badge.lead, .badge.draft {{ color: var(--warning); background: #fff7e8; }}
          .badge.blocked, .badge.failed, .badge.partial_failed {{ color: var(--danger); background: #fff0f2; }}
          @media (max-width: 1080px) {{ .stats {{ grid-template-columns: repeat(2,1fr); }} .grid {{ grid-template-columns: 1fr; }} }}
          @media (max-width: 760px) {{
            :root {{ --sidebar: 250px; }}
            .sidebar {{ transform: translateX(-100%); transition: transform .2s ease; box-shadow: 16px 0 40px rgba(28,34,70,.12); }}
            body.menu-open .sidebar {{ transform: translateX(0); }}
            .workspace {{ margin-left: 0; }} .mobile-menu {{ display: grid; }} .breadcrumbs .home, .breadcrumbs .separator:first-of-type {{ display: none; }}
            .topbar {{ padding: 0 16px; }} main {{ padding: 22px 16px 45px; }} .stats {{ grid-template-columns: 1fr; }}
            .page-heading {{ align-items: flex-start; }} h1 {{ font-size: 24px; }} .credit {{ display: none; }} .form-grid, .menu-editor, dl {{ grid-template-columns: 1fr; }}
            dt {{ padding-bottom: 2px; border-bottom: 0; }} dd {{ padding-top: 2px; }}
          }}
        </style>
      </head>
      <body>
        {svg_sprite()}
        <aside class="sidebar">
          <a class="brand" href="/"><span class="brand-mark">{icon("bot")}</span>Michelangelo</a>
          <nav class="nav"><div class="nav-label">Управление</div>{navigation}</nav>
          <div class="sidebar-foot"><a href="/docs">{icon("book")} Документация</a></div>
        </aside>
        <div class="workspace">
          <header class="topbar">
            <div class="breadcrumbs">
              <button class="mobile-menu" type="button" aria-label="Открыть меню" onclick="document.body.classList.toggle('menu-open')">{icon("menu")}</button>
              <a class="home" href="/">{icon("home")}</a><span class="separator">›</span>
              <span>Michelangelo Bots</span><span class="separator">›</span><strong>{e(section)}</strong>
            </div>
            <div class="top-actions"><span class="credit">0 ₽</span><span class="avatar">В</span></div>
          </header>
          <main>
            <div class="page-heading"><div><h1>{e(title)}</h1><p>{e(subtitle)}</p></div></div>
            {body}
          </main>
        </div>
        <script>
          document.addEventListener('click',e=>{{if(innerWidth<=760&&!e.target.closest('.sidebar')&&!e.target.closest('.mobile-menu'))document.body.classList.remove('menu-open')}});
          document.addEventListener('click',e=>{{
            const row=e.target.closest('tr[data-href]');
            if(!row||e.target.closest('a,button,input,select,textarea,form'))return;
            location.href=row.dataset.href;
          }});
          document.querySelectorAll('.chat-thread').forEach(el=>{{el.scrollTop=el.scrollHeight}});
        </script>
      </body>
    </html>
    """


def nav_item(label: str, href: str, icon_name: str, active: bool) -> str:
    active_class = ' class="active" aria-current="page"' if active else ""
    return f'<a href="{href}"{active_class}>{icon(icon_name)}<span>{e(label)}</span></a>'


def icon(name: str) -> str:
    return f'<svg class="icon" aria-hidden="true"><use href="#icon-{name}"></use></svg>'


def svg_sprite() -> str:
    return """
    <svg width="0" height="0" style="position:absolute"><defs>
      <symbol id="icon-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></symbol>
      <symbol id="icon-users" viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></symbol>
      <symbol id="icon-user" viewBox="0 0 24 24"><path d="M20 21a8 8 0 0 0-16 0"/><circle cx="12" cy="7" r="4"/></symbol>
      <symbol id="icon-bag" viewBox="0 0 24 24"><path d="M6 8h12l1 13H5L6 8Z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></symbol>
      <symbol id="icon-image" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></symbol>
      <symbol id="icon-chat" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2v10Z"/></symbol>
      <symbol id="icon-mail" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></symbol>
      <symbol id="icon-route" viewBox="0 0 24 24"><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19h3a4 4 0 0 0 4-4V9a4 4 0 0 1 3-4"/></symbol>
      <symbol id="icon-home" viewBox="0 0 24 24"><path d="m3 11 9-8 9 8v9a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1v-9Z"/></symbol>
      <symbol id="icon-book" viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4H6.5A2.5 2.5 0 0 0 4 6.5v13Z"/><path d="M4 19.5V6.5"/></symbol>
      <symbol id="icon-menu" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></symbol>
      <symbol id="icon-bot" viewBox="0 0 24 24"><rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M9 13h.01M15 13h.01M8 17h8"/></symbol>
      <symbol id="icon-settings" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></symbol>
      <symbol id="icon-upload" viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5M5 20h14"/></symbol>
    </defs></svg>
    """


def stat_card(label: str, value: int) -> str:
    return f'<div class="stat"><span>{e(label)}</span><strong>{value}</strong></div>'


def menu_editor_fields(item: BotMenuItem | None = None) -> str:
    platform = item.platform if item else "all"
    kind = item.kind if item else "message"
    title = item.title if item else ""
    body = item.body if item else ""
    url = item.url if item else ""
    position = item.position if item else 100
    active = "checked" if item is None or item.active else ""
    return f"""
      <label>Платформа<select name="platform">
        <option value="all" {selected(platform, 'all')}>Оба бота</option>
        <option value="telegram" {selected(platform, 'telegram')}>Telegram</option>
        <option value="max" {selected(platform, 'max')}>MAX</option>
      </select></label>
      <label>Тип<select name="kind">
        <option value="message" {selected(kind, 'message')}>Сообщение</option>
        <option value="link" {selected(kind, 'link')}>Ссылка</option>
      </select></label>
      <label>Название<input name="title" value="{e(title)}" required maxlength="255"></label>
      <label>Порядок<input name="position" type="number" value="{position}"></label>
      <label class="wide">Текст после нажатия<textarea name="body" rows="5">{e(body)}</textarea></label>
      <label class="wide">URL для кнопки-ссылки<input name="url" value="{e(url)}" placeholder="https://..."></label>
      <label class="checkbox"><input type="checkbox" name="active" {active}><span>Включена</span></label>
    """


def menu_item_editor(item: BotMenuItem) -> str:
    return f"""
      <form class="menu-editor menu-card" method="post" action="/bot-settings/menu/{item.id}">
        {menu_editor_fields(item)}
        <div class="actions wide">
          <button name="mode" value="save">Сохранить</button>
          <button class="secondary" name="mode" value="delete" formnovalidate>Удалить</button>
        </div>
      </form>
    """


def validate_menu_item(platform: str, kind: str, title: str, url: str) -> None:
    if platform not in {"all", "telegram", "max"}:
        raise HTTPException(status_code=422, detail="Invalid platform")
    if kind not in {"message", "link"}:
        raise HTTPException(status_code=422, detail="Invalid menu item kind")
    if not title.strip():
        raise HTTPException(status_code=422, detail="Title is required")
    if kind == "link" and not url.strip().startswith(("https://", "http://")):
        raise HTTPException(status_code=422, detail="Link URL must start with http:// or https://")


def user_import_form() -> str:
    return """
    <article>
      <h2>Загрузить базу из старой админки</h2>
      <p class="muted">Поддерживаются CSV, TSV и XLSX до 15 МБ и 50 000 строк.
      Сначала используйте «Проверить файл»: база при этом не изменяется.</p>
      <p><a class="button-link secondary" href="/users/import/template">Скачать CSV-шаблон</a></p>
      <form class="broadcast-form" method="post" action="/users/import"
            enctype="multipart/form-data">
        <label>Файл пользователей
          <input type="file" name="user_file" accept=".csv,.tsv,.txt,.xlsx" required>
        </label>
        <div class="form-grid">
          <label>Платформа по умолчанию
            <select name="default_platform">
              <option value="">Определить из файла</option>
              <option value="telegram">Telegram</option>
              <option value="max">MAX</option>
            </select>
          </label>
          <label>Если пользователь уже существует
            <select name="existing_mode">
              <option value="merge">Дополнить только пустые поля</option>
              <option value="overwrite">Перезаписать данными из файла</option>
              <option value="skip">Не изменять</option>
            </select>
          </label>
        </div>
        <label class="checkbox">
          <input type="checkbox" name="id_is_platform_user_id" value="1">
          <span>Колонка «ID» содержит именно Telegram/MAX User ID</span>
        </label>
        <p class="muted">Флажок для колонки «ID» включайте только если это ID клиента
        в мессенджере, а не внутренний номер записи старой админки. Обязательны
        Platform User ID либо отдельные Telegram ID/MAX ID. Колонки можно называть
        по-русски или по-английски.</p>
        <div class="actions">
          <button name="operation" value="preview">Проверить файл</button>
          <button name="operation" value="import">Импортировать валидные строки</button>
        </div>
      </form>
    </article>
    <article>
      <h2>Что переносится</h2>
      <p class="muted">Мессенджер и User ID, chat_id, ник, имя, ФИО, телефон,
      email, статус, реферал, комментарий, даты регистрации/активности и счётчик
      действий. Дубли определяются по паре «мессенджер + User ID».</p>
      <div class="notice danger">Важно: если используется новый бот с другим токеном,
      Telegram/MAX не позволят написать старым пользователям, пока они сами не запустят
      нового бота. При сохранении прежнего бота и токена перенесённые chat_id остаются рабочими.</div>
    </article>
    """


def user_import_report(
    parsed: UserImportParseResult,
    applied: UserImportApplyResult,
    *,
    imported: bool,
) -> str:
    action = "Импорт завершён" if imported else "Предварительная проверка завершена"
    action_hint = (
        "Валидные пользователи записаны в базу."
        if imported
        else "База не изменялась. Для переноса загрузите файл ещё раз и нажмите «Импортировать»."
    )
    errors = "".join(
        f"<tr><td>{issue.row}</td><td>{e(issue.message)}</td></tr>"
        for issue in parsed.errors[:200]
    )
    warnings = "".join(
        f"<tr><td>{issue.row}</td><td>{e(issue.message)}</td></tr>"
        for issue in parsed.warnings[:200]
    )
    sample = "".join(
        f"""
        <tr>
          <td>{record.row}</td><td>{platform_badge(record.platform)}</td>
          <td>{e(record.platform_user_id)}</td><td>{e(record.chat_id)}</td>
          <td>{e(record.username)}</td><td>{e(record.full_name)}</td>
          <td>{e(record.phone)}</td><td>{e(record.email)}</td><td>{e(record.status or 'active')}</td>
        </tr>
        """
        for record in parsed.records[:100]
    )
    issue_sections = ""
    if errors:
        issue_sections += f"""
        <article><h2>Ошибки строк — пропущено: {len(parsed.errors)}</h2>
          <div class="table-wrap"><table><thead><tr><th>Строка</th><th>Ошибка</th></tr></thead>
          <tbody>{errors}</tbody></table></div>
        </article>"""
    if warnings:
        issue_sections += f"""
        <article><h2>Предупреждения: {len(parsed.warnings)}</h2>
          <div class="table-wrap"><table><thead><tr><th>Строка</th><th>Предупреждение</th></tr></thead>
          <tbody>{warnings}</tbody></table></div>
        </article>"""
    return f"""
    <div class="notice success">{action}. {action_hint}</div>
    <section class="stats">
      {stat_card("Строк в файле", parsed.total_rows)}
      {stat_card("Валидных пользователей", len(parsed.records))}
      {stat_card("Ошибок", len(parsed.errors))}
      {stat_card("Будет/создано", applied.created)}
      {stat_card("Будет/обновлено", applied.updated)}
      {stat_card("Дубликатов в файле", parsed.duplicate_rows)}
    </section>
    <article>
      <h2>Результат для существующих записей</h2>
      <p>Без изменений: {applied.unchanged}. Пропущено настройкой: {applied.skipped_existing}.
      Пустых строк: {parsed.blank_rows}.</p>
    </article>
    {issue_sections}
    <article><h2>Предпросмотр валидных записей</h2>
      <p class="muted">Показаны первые {min(100, len(parsed.records))} записей.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Строка</th><th>Платформа</th><th>User ID</th><th>Chat ID</th>
        <th>Ник</th><th>ФИО</th><th>Телефон</th><th>Email</th><th>Статус</th></tr></thead>
        <tbody>{sample}</tbody>
      </table></div>
    </article>
    """


def user_row(user: BotUser) -> str:
    return f"""
    <tr data-href="/users/{user.id}">
      <td><a href="/users/{user.id}">{user.id}</a></td>
      <td>{platform_badge(user.platform)}</td>
      <td>{e(user.platform_user_id)}</td>
      <td>{e(user.username)}</td>
      <td>{e(user.full_name)}</td>
      <td>{user.total_actions}</td>
      <td>{e(user.last_action)}</td>
      <td>{format_dt(user.last_seen_at)}</td>
    </tr>
    """


def client_row(user: BotUser) -> str:
    return f"""
    <tr data-href="/users/{user.id}">
      <td><a href="/users/{user.id}">{user.id}</a></td>
      <td>{status_badge(user.status)}</td>
      <td>{e(user.referral)}</td>
      <td>{e(user.comment)}</td>
      <td>{e(user.full_name)}</td>
      <td>{e(user.phone)}</td>
      <td>{e(user.email)}</td>
      <td>{format_dt(user.first_seen_at)}</td>
      <td>{e(user.username)}</td>
      <td>{platform_badge(user.platform)}</td>
    </tr>
    """


def broadcast_row(broadcast: BotBroadcast) -> str:
    action = ""
    if broadcast.status in {"draft", "partial_failed"}:
        action = f"""
        <form method="post" action="/broadcasts/{broadcast.id}/send">
          <button class="secondary">Отправить</button>
        </form>
        """
    return f"""
    <tr>
      <td>{broadcast.id}</td>
      <td>{e(broadcast.name)}</td>
      <td>{format_dt(broadcast.sent_at)}</td>
      <td>{status_badge(broadcast.status)}</td>
      <td>{format_dt(broadcast.created_at)}</td>
      <td>{broadcast.success_count}/{broadcast.total_recipients}</td>
      <td>{broadcast_media_summary(broadcast)}{e(broadcast.last_error)}</td>
      <td>{action}</td>
    </tr>
    """


def client_path_row(event: BotEvent, user: BotUser) -> str:
    return f"""
    <tr data-href="/users/{user.id}">
      <td>{event.id}</td>
      <td>{e(event.event_type)} / {e(event.action)}</td>
      <td><a href="/users/{user.id}">{e(client_label(user))}</a></td>
      <td>{e(user.last_action)}</td>
      <td>{format_dt(event.occurred_at)}</td>
    </tr>
    """


def chat_row(user: BotUser, last_outgoing_at: datetime | None) -> str:
    return f"""
    <tr data-href="/chats/{user.id}">
      <td><a href="/chats/{user.id}">{e(client_label(user))}</a></td>
      <td>{platform_badge(user.platform)}</td>
      <td>{e(user.last_message_text)}</td>
      <td>{format_dt(last_outgoing_at)}</td>
      <td>{format_dt(user.last_seen_at)}</td>
    </tr>
    """


def chat_thread_html(thread: list[ChatItem]) -> str:
    if not thread:
        return '<p class="muted">Сообщений пока нет.</p>'

    bubbles = []
    for item in thread:
        outgoing = item.direction == "out"
        author = (item.author or "администратор") if outgoing else "клиент"
        classes = "bubble out" if outgoing else "bubble in"
        meta = f"{e(author)} · {format_dt(item.at)}"
        title = ""
        if item.status == "failed":
            classes += " failed"
            meta += " · не доставлено"
            title = f' title="{e(item.error)}"'
        text_html = f'<div class="bubble-text">{e(item.text)}</div>' if item.text else ""
        bubbles.append(
            f'<div class="{classes}"{title}>'
            f"{chat_attachments_html(item.attachments)}"
            f"{text_html}"
            f'<div class="bubble-meta">{meta}</div></div>'
        )
    return "".join(bubbles)


def chat_attachments_html(attachments: tuple[ChatAttachment, ...]) -> str:
    """Фото и видео в пузырьке: картинка открывается по клику, видео играет в ленте."""

    if not attachments:
        return ""

    blocks: list[str] = []
    for attachment in attachments:
        url = e(attachment.url)
        label = e(attachment.name) or "Вложение"
        if attachment.kind == "video":
            blocks.append(
                f'<video class="bubble-media" src="{url}" controls preload="metadata"></video>'
            )
        elif attachment.kind == "photo":
            blocks.append(
                f'<a href="{url}" target="_blank" rel="noopener">'
                f'<img class="bubble-media" src="{url}" alt="{label}" loading="lazy"></a>'
            )
        else:
            blocks.append(
                f'<a class="bubble-file" href="{url}" target="_blank" rel="noopener">{label}</a>'
            )
    joined = "".join(blocks)
    return f'<div class="bubble-media-list">{joined}</div>'


def order_row(order: BotOrder, user: BotUser | None) -> str:
    user_link = ""
    if user:
        user_link = f'<a href="/users/{user.id}">{e(client_label(user))}</a>'
    return f"""
    <tr data-href="/orders/{order.id}">
      <td><a href="/orders/{order.id}">{e(order.external_order_number)}</a></td>
      <td>{order_items_summary(order)}</td>
      <td>{e(order.total_amount)} {e(order.currency)}</td>
      <td>{e(order.customer_name)}</td>
      <td>{e(order.customer_phone)}</td>
      <td>{e(order.customer_email)}</td>
      <td>{order_binding(order)}</td>
      <td>{user_link}</td>
      <td>{format_dt(order.updated_at)}</td>
    </tr>
    """


BIND_SOURCE_LABELS = {
    "miniapp": "миниапп",
    "manual": "вручную",
    "phone": "по телефону",
    "email": "по email",
}


def order_binding(order: BotOrder) -> str:
    platform_user_id = order.platform_user_id or order.telegram_user_id
    if not platform_user_id:
        return '<span class="muted">не привязан</span>'
    source = BIND_SOURCE_LABELS.get(order.bind_source or "", order.bind_source or "")
    suffix = f'<div class="muted">{e(source)}</div>' if source else ""
    return f"{e(order.platform)} {e(platform_user_id)}{suffix}"


def user_order_row(order: BotOrder) -> str:
    return f"""
    <tr data-href="/orders/{order.id}">
      <td><a href="/orders/{order.id}">{e(order.external_order_number)}</a></td>
      <td>{order_items_summary(order)}</td>
      <td>{e(order.total_amount)} {e(order.currency)}</td>
      <td>{e(order.status)}</td>
      <td>{format_dt(order.updated_at)}</td>
    </tr>
    """


def order_status_notification_row(notification: OrderStatusNotification) -> str:
    result = "доставлено" if notification.delivered_at else "повторная отправка"
    return f"""
    <tr>
      <td>{e(notification.status)}</td>
      <td>{status_badge(result)}</td>
      <td>{notification.attempt_count or 0}</td>
      <td>{format_dt(notification.last_attempt_at)}</td>
      <td>{e(notification.error)}</td>
    </tr>
    """


def order_items_summary(order: BotOrder) -> str:
    payload = order.raw_payload or {}
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return ""

    rows = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        title = first_value(item.get("title"), item.get("name"), item.get("product_title"))
        amount = first_value(item.get("amount"), item.get("quantity"), item.get("qty"))
        if title and amount:
            rows.append(f"{e(title)} x {e(amount)}")
        elif title:
            rows.append(e(title))

    if len(items) > 5:
        rows.append(f"+ ещё {len(items) - 5}")
    return "<br>".join(rows)


async def upsert_readyscript_order(
    session: AsyncSession,
    payload: ReadyScriptOrderPayload,
    settings: Settings,
) -> BotOrder:
    raw_payload = payload.model_dump(mode="json", exclude_none=True)
    external_order_id = first_value(payload.order_id, payload.id)
    if not external_order_id:
        raise HTTPException(status_code=422, detail="order_id or id is required")

    telegram_user_id = extract_telegram_user_id(payload, settings)
    bot_user = await find_bot_user_by_telegram_id(session, telegram_user_id)
    customer = payload.user or {}
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

    order.external_order_number = first_value(payload.order_num, payload.number)
    order.platform = "telegram" if telegram_user_id else None
    order.telegram_user_id = telegram_user_id
    order.bot_user_id = bot_user.id if bot_user else None
    order.status = payload.status
    order.total_amount = first_value(payload.total, payload.amount)
    order.currency = payload.currency
    order.customer_name = first_value(
        payload.customer_name,
        customer.get("name"),
        customer.get("full_name"),
    )
    order.customer_phone = first_value(payload.customer_phone, customer.get("phone"))
    order.customer_email = first_value(payload.customer_email, customer.get("email"))
    order.raw_payload = raw_payload
    order.updated_at = now

    if bot_user:
        bot_user.phone = order.customer_phone or bot_user.phone
        bot_user.email = order.customer_email or bot_user.email
        bot_user.full_name = order.customer_name or bot_user.full_name

    await session.commit()
    await session.refresh(order)
    return order


def extract_telegram_user_id(
    payload: ReadyScriptOrderPayload,
    settings: Settings,
) -> str | None:
    if payload.telegram_init_data:
        try:
            init_data = verify_telegram_init_data(
                payload.telegram_init_data,
                settings.telegram_bot_token,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        user = init_data.get("user") or {}
        return str(user.get("id")) if user.get("id") is not None else None

    if payload.telegram_user_id is not None:
        return str(payload.telegram_user_id)
    return None


async def find_bot_user_by_telegram_id(
    session: AsyncSession,
    telegram_user_id: str | None,
) -> BotUser | None:
    if not telegram_user_id:
        return None
    result = await session.execute(
        select(BotUser).where(
            BotUser.platform == "telegram",
            BotUser.platform_user_id == telegram_user_id,
        )
    )
    return result.scalar_one_or_none()


def first_value(*values: object) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value)
    return None


async def save_broadcast_uploads(
    uploads: list[UploadFile],
    uploads_dir: Path,
) -> list[dict[str, str]]:
    return await save_media_uploads(uploads, uploads_dir, "broadcasts")


async def save_media_uploads(
    uploads: list[UploadFile],
    uploads_dir: Path,
    subdir: str,
) -> list[dict[str, str]]:
    """Сохраняет фото/видео в uploads_dir и описывает их для отправки и показа."""

    saved_files: list[dict[str, str]] = []
    target_dir = uploads_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    for upload in uploads:
        if not upload.filename:
            continue
        content_type = upload.content_type or "application/octet-stream"
        if not (content_type.startswith("image/") or content_type.startswith("video/")):
            continue

        original_name = Path(upload.filename).name
        suffix = Path(original_name).suffix
        stored_name = f"{uuid4().hex}{suffix}"
        stored_path = target_dir / stored_name
        content = await upload.read()
        stored_path.write_bytes(content)
        saved_files.append(
            {
                "filename": original_name,
                "path": str(stored_path),
                "content_type": content_type,
                "kind": media_kind(content_type),
            }
        )
    return saved_files


def resolve_media_type(selected_type: str, media_files: list[dict[str, str]]) -> str:
    if len(media_files) > 1:
        return "media_group"
    if len(media_files) == 1:
        return media_files[0]["kind"]
    return selected_type


def media_kind(content_type: str) -> str:
    if content_type.startswith("video/"):
        return "video"
    return "photo"


def broadcast_media_summary(broadcast: BotBroadcast) -> str:
    items = broadcast.media_files or []
    if not items:
        return ""
    names = ", ".join(e(item.get("filename")) for item in items)
    return f'<div class="muted">Файлы: {names}</div>'


def client_label(user: BotUser) -> str:
    return user.username or user.full_name or user.phone or user.email or user.platform_user_id


def status_badge(value: str | None) -> str:
    normalized = (value or "unknown").lower().replace(" ", "_")
    return f'<span class="badge {e(normalized)}">{e(value or "—")}</span>'


def platform_badge(value: str | None) -> str:
    label = {"telegram": "Telegram", "max": "MAX"}.get(value or "", value or "—")
    return f'<span class="badge {e(value)}">{e(label)}</span>'


def field(label: str, value: object) -> str:
    return f"<dt>{e(label)}</dt><dd>{e(value)}</dd>"


def selected(current: str | None, expected: str) -> str:
    return "selected" if current == expected else ""


def format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def e(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def main() -> None:
    uvicorn.run("michelangelo_bots.admin:app", host="0.0.0.0", port=8000)
