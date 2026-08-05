import pytest

from michelangelo_bots.config import Settings


def test_settings_accepts_platform_specific_miniapp_without_fallback() -> None:
    settings = Settings(
        TELEGRAM_MINIAPP_URL="https://t.me/test_bot/app",
        MAX_MINIAPP_URL="https://max.example/app",
        _env_file=None,
    )

    assert str(settings.telegram_miniapp_url) == "https://t.me/test_bot/app"
    assert str(settings.max_miniapp_url) == "https://max.example/app"


def test_settings_raises_clear_error_when_miniapp_url_is_missing() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(RuntimeError, match="TELEGRAM_MINIAPP_URL"):
        _ = settings.telegram_miniapp_url
