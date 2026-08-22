from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "readyscript-module" / "michelangelo"
MINIAPP = MODULE / "view" / "js" / "miniapp-2.2.1.js"


def test_external_messenger_sdks_do_not_block_readyscript_head() -> None:
    handlers = (MODULE / "config" / "handlers.inc.php").read_text()

    assert "https://telegram.org/js/telegram-web-app.js" not in handlers
    assert "https://st.max.ru/js/max-web-app.js" not in handlers
    assert "$app->addJs('%michelangelo%/miniapp-2.2.1.js')" in handlers


def test_miniapp_loads_only_detected_sdk_after_page_load() -> None:
    script = MINIAPP.read_text()

    assert "window.addEventListener('load', start" in script
    assert "script.async = true" in script
    assert "https://telegram.org/js/telegram-web-app.js" in script
    assert "https://st.max.ru/js/max-web-app.js" in script
    assert "if (!activePlatform" in script


def test_miniapp_signals_both_native_bridges_without_sdk() -> None:
    script = MINIAPP.read_text()

    assert "TelegramWebviewProxy.postEvent('web_app_ready'" in script
    assert "WebViewHandler.postEvent('WebAppReady'" in script
    assert "signalPlatformReady(platform)" in script


def test_diagnostics_cannot_block_storefront_on_log_lock() -> None:
    diagnostic = (MODULE / "model" / "diagnostic.inc.php").read_text()

    assert "LOCK_EX | LOCK_NB" in diagnostic
    assert "FILE_APPEND | LOCK_EX" not in diagnostic


def test_miniapp_recovers_identity_after_navigation_and_busts_webview_cache() -> None:
    handlers = (MODULE / "config" / "handlers.inc.php").read_text()
    script = MINIAPP.read_text()

    assert "miniapp-2.2.1.js" in handlers
    assert "location.search" in script
    assert "location.hash" in script
    assert "__telegram__initParams" in script
    assert "michelangelo_launch_params" in script
    assert "window.TelegramWebviewProxy" in script
