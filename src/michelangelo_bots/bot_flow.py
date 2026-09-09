"""Validated message graphs shared by the editor and both messenger runtimes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from michelangelo_bots.bot_configuration import menu_buttons, menu_response, setting
from michelangelo_bots.content import Action, MenuButton, text_for_action
from michelangelo_bots.db import BotSetting


class FlowButton(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=128)
    target: str | None = None
    url: str | None = None
    web_app: bool = False

    @model_validator(mode="after")
    def destination(self):
        if bool(self.target) == bool(self.url):
            raise ValueError("У кнопки должен быть один переход или одна ссылка")
        if self.url:
            url = urlsplit(self.url)
            if url.scheme != "https" or not url.hostname or any(c.isspace() for c in self.url):
                raise ValueError("Укажите полную HTTPS-ссылку, например https://example.com")
        return self


class FlowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    title: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=4000)
    photo: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}\.(jpg|png)$")
    x: float = Field(default=100, ge=0, le=10000, allow_inf_nan=False)
    y: float = Field(default=100, ge=0, le=10000, allow_inf_nan=False)
    buttons: list[FlowButton] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def content(self):
        if not self.text.strip() and not self.photo:
            raise ValueError("Добавьте текст или фото в сообщение")
        return self


class Flow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    nodes: list[FlowNode] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def links(self):
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes) or self.start not in ids:
            raise ValueError("Проверьте стартовый блок и уникальность блоков")
        if any(b.target and b.target not in ids for n in self.nodes for b in n.buttons):
            raise ValueError("Одна из кнопок ведёт на удалённое сообщение")
        return self


Platform = Literal["telegram", "max"]


def flow_key(platform: str, version: str = "live") -> str:
    return f"flow_{platform}_{version}"


async def migrated_flow(session: AsyncSession, platform: str, miniapp_url: str) -> Flow:
    buttons = await menu_buttons(session, platform, miniapp_url)
    nodes = [
        FlowNode(
            id="main_menu",
            title="Главное меню",
            text=await setting(session, "start_text"),
            x=80,
            y=180,
        )
    ]
    for index, button in enumerate(buttons):
        if button.url:
            nodes[0].buttons.append(
                FlowButton(title=button.title, url=button.url, web_app=button.web_app)
            )
            continue
        action = str(button.action)
        node_id = action.replace(":", "_")
        body = await menu_response(session, action, platform)
        if body is None:
            body = text_for_action(Action(action))
        nodes[0].buttons.append(FlowButton(title=button.title, target=node_id))
        if node_id not in {n.id for n in nodes}:
            nodes.append(
                FlowNode(
                    id=node_id,
                    title=button.title,
                    text=body or "Раздел",
                    x=500,
                    y=40 + index * 260,
                    buttons=[FlowButton(title="В главное меню", target="main_menu")],
                )
            )
    return Flow(start="main_menu", nodes=nodes)


async def flow_message(session: AsyncSession, platform: str, action: str) -> FlowNode | None:
    row = await session.get(BotSetting, flow_key(platform))
    if row is None:
        return None
    flow = Flow.model_validate_json(row.value)
    target = (
        flow.start
        if action == "main_menu"
        else action.removeprefix("flow:").replace("config:", "config_")
    )
    node = next((node for node in flow.nodes if node.id == target), None)
    # Old keyboards remain useful after a block is removed.
    return node or next(node for node in flow.nodes if node.id == flow.start)


def flow_buttons(node: FlowNode) -> list[MenuButton]:
    return [
        MenuButton(
            b.title, action=f"flow:{b.target}" if b.target else None, url=b.url, web_app=b.web_app
        )
        for b in node.buttons
    ]


def photo_path(uploads_dir: Path, photo: str) -> Path:
    # Validate even when called outside a request model.
    import re

    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png)", photo):
        raise ValueError("Некорректное имя фото")
    return uploads_dir / "flows" / photo


def encoded_flow(flow: Flow) -> str:
    return json.dumps(flow.model_dump(), ensure_ascii=False)


async def ensure_flows(session: AsyncSession, settings) -> None:
    """One-time import; restarting services never overwrites an edited scenario."""
    from sqlalchemy.dialects.postgresql import insert

    for platform in ("telegram", "max"):
        live = await session.get(BotSetting, flow_key(platform))
        draft = await session.get(BotSetting, flow_key(platform, "draft"))
        if live is not None and draft is not None:
            continue
        url = settings.telegram_miniapp_url if platform == "telegram" else settings.max_miniapp_url
        value = (
            live.value if live else encoded_flow(await migrated_flow(session, platform, str(url)))
        )
        for version in ("live", "draft"):
            await session.execute(
                insert(BotSetting)
                .values(key=flow_key(platform, version), value=value)
                .on_conflict_do_nothing(index_elements=["key"])
            )
    await session.commit()
