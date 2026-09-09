import asyncio
import logging
from collections.abc import Sequence
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from michelangelo_bots.bot_configuration import (
    menu_buttons,
    menu_response,
    order_values,
    render_start_template,
    render_template,
    setting,
)
from michelangelo_bots.bot_flow import flow_buttons, flow_message, photo_path
from michelangelo_bots.config import get_settings
from michelangelo_bots.content import (
    Action,
    MenuButton,
    back_to_main_buttons,
    text_for_action,
)
from michelangelo_bots.db import get_session_factory, init_db
from michelangelo_bots.inbound_notifications import notify_admins_about_telegram_message
from michelangelo_bots.order_cancellation import (
    CancellationResult,
    cancel_customer_order,
    get_customer_order,
)
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


async def send_flow_message(message: Message, action: str, name: str | None) -> bool:
    async with get_session_factory()() as session:
        node = await flow_message(session, "telegram", action)
    if node is None:
        return False
    keyboard = telegram_keyboard(flow_buttons(node)) if node.buttons else None
    text = render_start_template(node.text, name)
    if node.photo:
        photo = FSInputFile(photo_path(get_settings().uploads_dir, node.photo))
        if len(text.encode("utf-16-le")) // 2 <= 1024:
            await message.answer_photo(photo, caption=text or None, reply_markup=keyboard)
            return True
        await message.answer_photo(photo)
    # Personalized names can push a message beyond the platform limit.
    while len(text.encode("utf-16-le")) // 2 > 4096:
        chunk = text[:2000]
        await message.answer(chunk)
        text = text[len(chunk) :]
    await message.answer(text, reply_markup=keyboard)
    return True


@router.callback_query(F.data.startswith("flow:"))
async def handle_flow_callback(callback: CallbackQuery) -> None:
    await track_telegram_callback(callback, action=callback.data or "flow")
    await callback.answer()
    if callback.message:
        await send_flow_message(
            callback.message, callback.data or "main_menu", telegram_display_name(callback)
        )


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    settings = get_settings()
    await track_telegram_message(message, action=Action.MAIN_MENU.value)
    if await send_flow_message(message, "main_menu", telegram_display_name(message)):
        return
    async with get_session_factory()() as session:
        buttons = await menu_buttons(session, "telegram", str(settings.telegram_miniapp_url))
        start_text = await setting(session, "start_text")
    await message.answer(
        render_start_template(start_text, telegram_display_name(message)),
        reply_markup=telegram_keyboard(buttons),
    )


@router.callback_query(F.data.startswith("order_cancel:"))
async def handle_order_cancel_request(callback: CallbackQuery) -> None:
    order_id = callback_order_id(callback.data)
    user = await track_telegram_callback(callback, action=callback.data or "order_cancel")
    if order_id is None or user is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    order = await get_customer_order(
        order_id,
        bot_user_id=user.id,
        platform="telegram",
    )
    if order is None:
        await callback.answer("Этот заказ недоступен", show_alert=True)
        return
    async with get_session_factory()() as session:
        template = await setting(session, "customer_cancel_confirm_text")
        confirm_button_text = await setting(session, "customer_cancel_confirm_button_text")
        abort_button_text = await setting(session, "customer_cancel_abort_button_text")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=confirm_button_text,
            callback_data=f"order_cancel_confirm:{order.id}",
        ),
        InlineKeyboardButton(
            text=abort_button_text,
            callback_data=f"order_cancel_abort:{order.id}",
        ),
    ]])
    if callback.message:
        await callback.message.answer(
            render_template(template, order_values(order, user)),
            reply_markup=keyboard,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("order_cancel_confirm:"))
async def handle_order_cancel_confirm(callback: CallbackQuery) -> None:
    order_id = callback_order_id(callback.data)
    user = await track_telegram_callback(callback, action=callback.data or "order_cancel_confirm")
    if order_id is None or user is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await callback.answer("Отменяем заказ…")
    result = await cancel_customer_order(
        order_id,
        bot_user_id=user.id,
        platform="telegram",
        platform_user_id=user.platform_user_id,
        settings=get_settings(),
    )
    async with get_session_factory()() as session:
        text, manager_url = await cancellation_result_message(session, result, "telegram")
    keyboard = None
    if result.outcome != "cancelled" and manager_url:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✉️ Связаться с менеджером", url=manager_url)
        ]])
    if callback.message:
        await callback.message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("order_cancel_abort:"))
async def handle_order_cancel_abort(callback: CallbackQuery) -> None:
    await track_telegram_callback(callback, action=callback.data or "order_cancel_abort")
    if callback.message:
        await callback.message.answer("Заказ не отменён.")
    await callback.answer()


@router.callback_query(F.data.in_({action.value for action in Action}))
async def handle_menu_callback(callback: CallbackQuery) -> None:
    action = Action(callback.data)
    settings = get_settings()
    await track_telegram_callback(callback, action=action.value)
    if callback.message and await send_flow_message(
        callback.message, action.value, telegram_display_name(callback)
    ):
        await callback.answer()
        return
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
    if callback.message and await send_flow_message(
        callback.message, action, telegram_display_name(callback)
    ):
        await callback.answer()
        return
    async with get_session_factory()() as session:
        body = await menu_response(session, action, "telegram")
    if body is not None and callback.message:
        await callback.message.answer(body, reply_markup=telegram_keyboard(back_to_main_buttons()))
    await callback.answer()


@router.message()
async def handle_unknown_message(message: Message) -> None:
    settings = get_settings()
    user = await track_telegram_message(message, action="unknown_message")
    if str(message.chat.id) in parse_recipient_ids(settings.order_notification_telegram_chat_ids):
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


def callback_order_id(payload: str | None) -> int | None:
    try:
        return int((payload or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


async def cancellation_result_message(session, result: CancellationResult, platform: str):
    if result.outcome == "cancelled":
        key = "customer_cancel_success_text"
    elif result.outcome == "too_late":
        key = "customer_cancel_too_late_text"
    elif result.outcome == "processing":
        return "Запрос на отмену этого заказа уже обрабатывается.", ""
    else:
        key = "customer_cancel_failure_text"
    template = await setting(session, key)
    manager_key = "telegram_manager_url" if platform == "telegram" else "max_manager_url"
    return render_template(template, {"order_number": result.order_number}), await setting(
        session, manager_key
    )


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
