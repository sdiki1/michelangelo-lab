from michelangelo_bots.content import (
    START_TEXT,
    Action,
    back_to_main_buttons,
    main_menu_buttons,
    render_start_text,
    text_for_action,
)


def test_main_menu_has_required_buttons() -> None:
    buttons = main_menu_buttons("https://example.com/app")

    assert [button.title for button in buttons] == [
        "Миниапп",
        "О нас",
        "Доставка кукол бабочек",
        "Наши контакты",
        "Как отслеживать доставку",
    ]
    assert buttons[0].url == "https://example.com/app"


def test_back_to_main_button() -> None:
    buttons = back_to_main_buttons()

    assert len(buttons) == 1
    assert buttons[0].title == "В главное меню"
    assert buttons[0].action is Action.MAIN_MENU


def test_start_text_contains_contacts_and_delivery_info() -> None:
    assert "Привет, {{name}}!" in START_TEXT
    assert "@michelangelo_nabor" in START_TEXT
    assert "Отправляем 1-2 раза в неделю" in START_TEXT
    assert text_for_action(Action.MAIN_MENU) == START_TEXT


def test_render_start_text_substitutes_name() -> None:
    assert "Привет, Иван!" in render_start_text("Иван")
    assert "Привет, дорогой друг!" in render_start_text("")
