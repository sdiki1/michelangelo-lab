from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from michelangelo_bots import bot_flow, max_bot, telegram_bot
from michelangelo_bots.bot_flow import Flow, FlowButton, FlowNode, encoded_flow
from michelangelo_bots.config import Settings
from michelangelo_bots.content import Action, MenuButton


def scenario():
    return Flow(
        start="hello",
        nodes=[
            FlowNode(
                id="hello",
                title="Приветствие",
                text="Привет, {{name}}!",
                buttons=[
                    FlowButton(title="Доставка", target="delivery"),
                ],
            ),
            FlowNode(
                id="delivery",
                title="Доставка",
                text="Доставляем СДЭК",
                buttons=[
                    FlowButton(title="Назад", target="hello"),
                ],
            ),
        ],
    )


def test_graph_allows_cycles_but_rejects_dangling_edges_and_duplicate_ids():
    graph = scenario().model_dump()
    Flow.model_validate(graph)
    graph["nodes"][1]["buttons"][0]["target"] = "deleted"
    with pytest.raises(ValidationError):
        Flow.model_validate(graph)
    graph = scenario().model_dump()
    graph["nodes"].append(graph["nodes"][0])
    with pytest.raises(ValidationError):
        Flow.model_validate(graph)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "   "},
        {"photo": "../../.env"},
        {"x": float("nan")},
        {"id": "a" * 65},
        {"buttons": [{"title": "X", "url": "javascript:alert(1)"}]},
        {"buttons": [{"title": "X", "target": "hello", "url": "https://example.com"}]},
    ],
)
def test_invalid_node_is_rejected(change):
    node = scenario().nodes[0].model_dump()
    node.update(change)
    with pytest.raises(ValidationError):
        FlowNode.model_validate(node)


@pytest.mark.asyncio
async def test_migration_preserves_configured_content_and_miniapp(monkeypatch):
    monkeypatch.setattr(
        bot_flow,
        "menu_buttons",
        AsyncMock(
            return_value=[
                MenuButton("Магазин", url="https://example.com/app", web_app=True),
                MenuButton("Заказать", action="config:42"),
                MenuButton("Доставка", action=Action.DELIVERY),
            ]
        ),
    )
    monkeypatch.setattr(bot_flow, "menu_response", AsyncMock(side_effect=["Мой текст", None]))
    monkeypatch.setattr(bot_flow, "setting", AsyncMock(return_value="Моё приветствие"))
    graph = await bot_flow.migrated_flow(AsyncMock(), "max", "https://example.com/app")
    assert graph.nodes[0].text == "Моё приветствие"
    assert graph.nodes[0].buttons[0].web_app
    assert graph.nodes[1].id == "config_42"
    assert graph.nodes[1].text == "Мой текст"
    assert graph.nodes[1].buttons[0].target == graph.start
    assert graph.nodes[2].id == "delivery"


@pytest.mark.asyncio
async def test_runtime_uses_only_published_graph_and_old_callbacks_work():
    session = AsyncMock()
    graph = scenario()
    graph.nodes.append(FlowNode(id="config_42", title="Старое меню", text="Перенесено"))
    session.get.return_value = SimpleNamespace(value=encoded_flow(graph))
    assert (await bot_flow.flow_message(session, "telegram", "main_menu")).id == "hello"
    assert (await bot_flow.flow_message(session, "telegram", "flow:delivery")).id == "delivery"
    assert (await bot_flow.flow_message(session, "telegram", "config:42")).id == "config_42"
    assert (await bot_flow.flow_message(session, "telegram", "flow:deleted")).id == "hello"
    assert session.get.call_args.args[1] == "flow_telegram_live"


@pytest.mark.asyncio
async def test_telegram_photo_caption_and_long_text_keep_buttons(monkeypatch, tmp_path):
    node = scenario().nodes[0]
    node.photo = "a" * 32 + ".png"
    monkeypatch.setattr(telegram_bot, "flow_message", AsyncMock(return_value=node))
    monkeypatch.setattr(telegram_bot, "get_settings", lambda: SimpleNamespace(uploads_dir=tmp_path))
    message = AsyncMock()
    await telegram_bot.send_flow_message(message, "main_menu", "Анна")
    assert message.answer_photo.call_args.kwargs["caption"] == "Привет, Анна!"
    keyboard = message.answer_photo.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "flow:delivery"
    message.answer.assert_not_called()
    message.reset_mock()
    node.text = "Текст " * 300
    await telegram_bot.send_flow_message(message, "main_menu", "Анна")
    assert "reply_markup" not in message.answer_photo.call_args.kwargs
    assert message.answer.call_args.kwargs["reply_markup"] == keyboard


@pytest.mark.asyncio
async def test_max_flow_sends_uploaded_photo_and_transition(monkeypatch, tmp_path):
    node = scenario().nodes[0]
    node.photo = "b" * 32 + ".png"
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / node.photo).write_bytes(b"test")
    monkeypatch.setattr(max_bot, "flow_message", AsyncMock(return_value=node))
    monkeypatch.setattr(max_bot, "track_max_interaction", AsyncMock())
    client = AsyncMock()
    client.upload_attachment.return_value = {"type": "image", "payload": {"token": "photo"}}
    settings = Settings(_env_file=None, UPLOADS_DIR=tmp_path)
    await max_bot.MaxBot(client, settings).handle_update(
        {
            "callback": {
                "callback_id": "cb",
                "payload": "flow:hello",
                "message": {"recipient": {"chat_id": 123}},
            }
        }
    )
    client.answer_callback.assert_awaited_once_with("cb")
    attachments = client.send_message.call_args.kwargs["attachments"]
    assert attachments[0]["payload"]["buttons"][0][0]["payload"] == "flow:delivery"
    assert attachments[1]["type"] == "image"


@pytest.mark.asyncio
async def test_max_terminal_message_has_no_empty_keyboard(monkeypatch):
    node = FlowNode(id="end", title="Конец", text="Спасибо!")
    monkeypatch.setattr(max_bot, "flow_message", AsyncMock(return_value=node))
    monkeypatch.setattr(max_bot, "track_max_interaction", AsyncMock())
    client = AsyncMock()
    await max_bot.MaxBot(client, Settings(_env_file=None)).handle_update(
        {
            "callback": {
                "callback_id": "cb",
                "payload": "flow:end",
                "message": {"recipient": {"chat_id": 123}},
            }
        }
    )
    assert client.send_message.call_args.kwargs["attachments"] == []
