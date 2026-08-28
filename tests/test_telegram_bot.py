from michelangelo_bots.content import Action, MenuButton
from michelangelo_bots.telegram_bot import telegram_keyboard


def test_telegram_keyboard_builds_callback_and_web_app_buttons() -> None:
    keyboard = telegram_keyboard(
        [
            MenuButton("Миниапп", url="https://example.com/app"),
            MenuButton("О нас", action=Action.ABOUT),
        ]
    )

    assert keyboard.inline_keyboard[0][0].text == "Миниапп"
    assert keyboard.inline_keyboard[0][0].url is None
    assert keyboard.inline_keyboard[0][0].web_app is not None
    assert keyboard.inline_keyboard[0][0].web_app.url == "https://example.com/app"
    assert keyboard.inline_keyboard[1][0].text == "О нас"
    assert keyboard.inline_keyboard[1][0].callback_data == "about"


def test_telegram_keyboard_uses_url_for_telegram_direct_link() -> None:
    keyboard = telegram_keyboard(
        [
            MenuButton("Миниапп", url="https://t.me/test_bot/app"),
        ]
    )

    assert keyboard.inline_keyboard[0][0].url == "https://t.me/test_bot/app"
    assert keyboard.inline_keyboard[0][0].web_app is None


def test_configured_external_link_is_not_forced_into_web_app() -> None:
    keyboard = telegram_keyboard(
        [MenuButton("Документы", url="https://example.com/docs", web_app=False)]
    )

    assert keyboard.inline_keyboard[0][0].url == "https://example.com/docs"
    assert keyboard.inline_keyboard[0][0].web_app is None
