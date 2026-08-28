import asyncio
import logging
from collections.abc import Sequence
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from michelangelo_bots.bot_configuration import (
    menu_buttons,
    menu_response,
    render_start_template,
    setting,
)
from michelangelo_bots.config import get_settings
from michelangelo_bots.content import (
    Action,
    MenuButton,
    back_to_main_buttons,
    text_for_action,
)
from michelangelo_bots.db import get_session_factory, init_db
from michelangelo_bots.inbound_notifications import notify_admins_about_telegram_message
from michelangelo_bots.order_notifications import parse_recipient_ids
from michelangelo_bots.tracking import track_telegram_callback, track_telegram_message

logger = logging.getLogger(__name__)
router = Router(name="michelangelo")


def telegram_keyboard(buttons: Sequence[MenuButton]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for button in buttons:
        if button.url:
            rows.append(
                [
                    telegram_miniapp_button(button.title, button.url)
                    if button.web_app
                    else InlineKeyboardButton(text=button.title, url=button.url)
                ]
            )
            continue

        rows.append(
            [
                InlineKeyboardButton(
                    text=button.title,
                    callback_data=(
                        button.action.value if isinstance(button.action, Action) else button.action
                    ),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def telegram_miniapp_button(title: str, url: str) -> InlineKeyboardButton:
    if is_telegram_direct_link(url):
        return InlineKeyboardButton(text=title, url=url)
    return InlineKeyboardButton(text=title, web_app=WebAppInfo(url=url))


def is_telegram_direct_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"t.me", "telegram.me"}


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    settings = get_settings()
    await track_telegram_message(message, action=Action.MAIN_MENU.value)
    async with get_session_factory()() as session:
        buttons = await menu_buttons(session, "telegram", str(settings.telegram_miniapp_url))
        start_text = await setting(session, "start_text")
    await message.answer(
        render_start_template(start_text, telegram_display_name(message)),
        reply_markup=telegram_keyboard(buttons),
    )


@router.callback_query(F.data.in_({action.value for action in Action}))
async def handle_menu_callback(callback: CallbackQuery) -> None:
    action = Action(callback.data)
    settings = get_settings()
    await track_telegram_callback(callback, action=action.value)
    if action is Action.MAIN_MENU:
        async with get_session_factory()() as session:
            buttons = await menu_buttons(session, "telegram", str(settings.telegram_miniapp_url))
            start_text = await setting(session, "start_text")
        keyboard = telegram_keyboard(buttons)
    else:
        keyboard = telegram_keyboard(back_to_main_buttons())

    if callback.message:
        text = (
            render_start_template(start_text, telegram_display_name(callback))
            if action is Action.MAIN_MENU
            else text_for_action(action)
        )
        await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("config:"))
async def handle_configured_menu_callback(callback: CallbackQuery) -> None:
    action = callback.data or ""
    await track_telegram_callback(callback, action=action)
    async with get_session_factory()() as session:
        body = await menu_response(session, action, "telegram")
    if body is not None and callback.message:
        await callback.message.answer(body, reply_markup=telegram_keyboard(back_to_main_buttons()))
    await callback.answer()


@router.message()
async def handle_unknown_message(message: Message) -> None:
    settings = get_settings()
    user = await track_telegram_message(message, action="unknown_message")
    if str(message.chat.id) in parse_recipient_ids(
        settings.order_notification_telegram_chat_ids
    ):
        return
    await notify_admins_about_telegram_message(message, user, settings)
    async with get_session_factory()() as session:
        ack = await setting(session, "incoming_ack_text")
    await message.answer(
        ack,
    )


def telegram_display_name(message_or_callback: Message | CallbackQuery) -> str | None:
    user = message_or_callback.from_user
    if user is None:
        return None
    return user.full_name or user.username


async def run() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        me = await bot.get_me()
        logger.info(
            "Starting Telegram bot polling: id=%s username=@%s name=%s",
            me.id,
            me.username,
            me.full_name,
        )
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
