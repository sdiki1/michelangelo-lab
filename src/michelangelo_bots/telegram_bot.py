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

from michelangelo_bots.config import get_settings
from michelangelo_bots.content import (
    Action,
    MenuButton,
    back_to_main_buttons,
    main_menu_buttons,
    render_start_text,
    text_for_action,
)
from michelangelo_bots.db import init_db
from michelangelo_bots.tracking import track_telegram_callback, track_telegram_message

logger = logging.getLogger(__name__)
router = Router(name="michelangelo")


def telegram_keyboard(buttons: Sequence[MenuButton]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for button in buttons:
        if button.url:
            rows.append([telegram_miniapp_button(button.title, button.url)])
            continue

        rows.append(
            [
                InlineKeyboardButton(
                    text=button.title,
                    callback_data=button.action.value if button.action else None,
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
    await message.answer(
        render_start_text(telegram_display_name(message)),
        reply_markup=telegram_keyboard(main_menu_buttons(str(settings.telegram_miniapp_url))),
    )


@router.callback_query(F.data.in_({action.value for action in Action}))
async def handle_menu_callback(callback: CallbackQuery) -> None:
    action = Action(callback.data)
    settings = get_settings()
    await track_telegram_callback(callback, action=action.value)
    keyboard = (
        telegram_keyboard(main_menu_buttons(str(settings.telegram_miniapp_url)))
        if action is Action.MAIN_MENU
        else telegram_keyboard(back_to_main_buttons())
    )

    if callback.message:
        text = (
            render_start_text(telegram_display_name(callback))
            if action is Action.MAIN_MENU
            else text_for_action(action)
        )
        await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.message()
async def handle_unknown_message(message: Message) -> None:
    settings = get_settings()
    await track_telegram_message(message, action="unknown_message")
    await message.answer(
        render_start_text(telegram_display_name(message)),
        reply_markup=telegram_keyboard(main_menu_buttons(str(settings.telegram_miniapp_url))),
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
