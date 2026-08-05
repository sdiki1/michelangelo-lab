import html
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.broadcasting import send_broadcast
from michelangelo_bots.config import Settings, get_settings
from michelangelo_bots.db import (
    BotBroadcast,
    BotEvent,
    BotOrder,
    BotUser,
    datetime_now,
    get_session,
    init_db,
)
from michelangelo_bots.telegram_webapp import verify_telegram_init_data
from michelangelo_bots.tracking import find_user

security = HTTPBasic()
app = FastAPI(title="Michelangelo Bot Admin")


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


@app.on_event("startup")
async def startup() -> None:
    await init_db()


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
          {stat_card("Действий", total_events)}
          {stat_card("Заказов", total_orders)}
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
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
    )


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
                <th>Telegram ID</th><th>Пользователь</th><th>Обновлён</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
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
    return f"""
    <!doctype html>
    <html lang="ru">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{e(title)} | Michelangelo Admin</title>
        <style>
          :root {{ --bg: #f4f6f8; --panel: #ffffff; --ink: #1f2933; --muted: #667085; --line: #d7dde3; --head: #132238; --accent: #0b63ce; --radius: 16px; }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }}
          header {{ background: var(--head); color: white; padding: 14px 22px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; border-bottom-left-radius: 22px; border-bottom-right-radius: 22px; }}
          header a {{ color: white; text-decoration: none; font-weight: 700; padding: 9px 12px; border-radius: 999px; background: rgba(255,255,255,.08); }}
          header a:hover {{ background: rgba(255,255,255,.16); }}
          main {{ padding: 24px; max-width: 1480px; margin: 0 auto; }}
          h1 {{ margin: 0 0 20px; font-size: 26px; }}
          h2 {{ margin: 0 0 12px; font-size: 18px; }}
          a {{ color: var(--accent); }}
          .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 20px; }}
          .stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; box-shadow: 0 8px 22px rgba(18,34,56,.05); }}
          .stat span {{ color: var(--muted); }}
          .stat strong {{ display: block; font-size: 30px; margin-top: 4px; }}
          .grid {{ display: grid; grid-template-columns: minmax(280px, 420px) 1fr; gap: 20px; align-items: start; }}
          article, .profile {{ background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; margin-bottom: 20px; box-shadow: 0 8px 22px rgba(18,34,56,.05); }}
          .table-wrap {{ overflow: auto; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: 0 8px 22px rgba(18,34,56,.05); }}
          table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: transparent; }}
          th, td {{ padding: 11px 13px; border-bottom: 1px solid #e5e9ee; text-align: left; vertical-align: top; }}
          th {{ background: #eef2f5; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #344054; }}
          th:first-child {{ border-top-left-radius: var(--radius); }}
          th:last-child {{ border-top-right-radius: var(--radius); }}
          tr:last-child td {{ border-bottom: 0; }}
          .filters {{ display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }}
          input, select, textarea, button {{ padding: 10px 12px; border: 1px solid #bdc7d1; border-radius: 12px; background: white; font: inherit; }}
          input, select {{ min-height: 42px; }}
          textarea {{ resize: vertical; width: 100%; }}
          button {{ background: var(--head); color: white; cursor: pointer; border-color: var(--head); font-weight: 700; }}
          button.secondary {{ background: #eef2f5; color: var(--ink); border-color: var(--line); }}
          label {{ display: grid; gap: 6px; font-weight: 700; }}
          label input, label select, label textarea {{ font-weight: 400; }}
          .broadcast-form {{ display: grid; gap: 14px; max-width: 860px; }}
          .form-grid {{ display: grid; grid-template-columns: 220px 1fr; gap: 12px; }}
          .checkbox {{ display: flex; align-items: center; gap: 8px; }}
          .checkbox input {{ min-height: auto; }}
          .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
          dl {{ display: grid; grid-template-columns: 220px 1fr; gap: 8px 16px; margin: 0; }}
          dt {{ font-weight: 700; }}
          dd {{ margin: 0; }}
          pre {{ overflow: auto; background: #f1f3f5; padding: 12px; border-radius: 14px; }}
          code {{ background: #eef2f5; padding: 2px 6px; border-radius: 8px; }}
          .muted {{ color: var(--muted); }}
          @media (max-width: 900px) {{ .stats, .grid, .form-grid {{ grid-template-columns: 1fr; }} main {{ padding: 16px; }} }}
        </style>
      </head>
      <body>
        <header>
          <a href="/">Статистика</a>
          <a href="/clients">Клиенты</a>
          <a href="/orders">Заказы</a>
          <a href="/broadcasts">Рассылки</a>
          <a href="/client-paths">Путь клиента</a>
          <a href="/users">Пользователи</a>
        </header>
        <main>
          <h1>{e(title)}</h1>
          {body}
        </main>
      </body>
    </html>
    """


def stat_card(label: str, value: int) -> str:
    return f'<div class="stat"><span>{e(label)}</span><strong>{value}</strong></div>'


def user_row(user: BotUser) -> str:
    return f"""
    <tr>
      <td><a href="/users/{user.id}">{user.id}</a></td>
      <td>{e(user.platform)}</td>
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
    <tr>
      <td><a href="/users/{user.id}">{user.id}</a></td>
      <td>{e(user.status)}</td>
      <td>{e(user.referral)}</td>
      <td>{e(user.comment)}</td>
      <td>{e(user.full_name)}</td>
      <td>{e(user.phone)}</td>
      <td>{e(user.email)}</td>
      <td>{format_dt(user.first_seen_at)}</td>
      <td>{e(user.username)}</td>
      <td>{e(user.platform)}</td>
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
      <td>{e(broadcast.status)}</td>
      <td>{format_dt(broadcast.created_at)}</td>
      <td>{broadcast.success_count}/{broadcast.total_recipients}</td>
      <td>{broadcast_media_summary(broadcast)}{e(broadcast.last_error)}</td>
      <td>{action}</td>
    </tr>
    """


def client_path_row(event: BotEvent, user: BotUser) -> str:
    return f"""
    <tr>
      <td>{event.id}</td>
      <td>{e(event.event_type)} / {e(event.action)}</td>
      <td><a href="/users/{user.id}">{e(client_label(user))}</a></td>
      <td>{e(user.last_action)}</td>
      <td>{format_dt(event.occurred_at)}</td>
    </tr>
    """


def order_row(order: BotOrder, user: BotUser | None) -> str:
    user_link = ""
    if user:
        user_link = f'<a href="/users/{user.id}">{e(client_label(user))}</a>'
    return f"""
    <tr>
      <td>{e(order.external_order_number)}</td>
      <td>{order_items_summary(order)}</td>
      <td>{e(order.total_amount)} {e(order.currency)}</td>
      <td>{e(order.customer_name)}</td>
      <td>{e(order.customer_phone)}</td>
      <td>{e(order.customer_email)}</td>
      <td>{e(order.telegram_user_id)}</td>
      <td>{user_link}</td>
      <td>{format_dt(order.updated_at)}</td>
    </tr>
    """


def user_order_row(order: BotOrder) -> str:
    return f"""
    <tr>
      <td>{e(order.external_order_number)}</td>
      <td>{order_items_summary(order)}</td>
      <td>{e(order.total_amount)} {e(order.currency)}</td>
      <td>{e(order.status)}</td>
      <td>{format_dt(order.updated_at)}</td>
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
    saved_files: list[dict[str, str]] = []
    target_dir = uploads_dir / "broadcasts"
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
