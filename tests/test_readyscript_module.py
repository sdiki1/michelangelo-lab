from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "readyscript-module" / "michelangelo"
MINIAPP = MODULE / "view" / "js" / "miniapp-2.2.1.js"
PROMO_PROMPT = MODULE / "view" / "js" / "promo-required-2.3.2.js"


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


def test_promo_code_is_required_by_default_and_message_is_configurable() -> None:
    config = (MODULE / "config" / "file.inc.php").read_text()
    module_xml = (MODULE / "config" / "module.xml").read_text()

    assert "'require_promo_code'" in config
    assert "'promo_required_message'" in config
    assert "'promo_prompt_text'" in config
    assert "'require_promo_code' => 1" in config
    assert "<require_promo_code>1</require_promo_code>" in module_xml
    assert "<version>2.4.0.0</version>" in module_xml


def test_checkout_requires_an_applied_coupon_not_just_request_text() -> None:
    handlers = (MODULE / "config" / "handlers.inc.php").read_text()

    assert "'ml_promo_gate' => new Type\\Integer" in handlers
    assert "'condition' => ['step' => 'confirm']" in handlers
    assert "[__CLASS__, 'checkRequiredPromoCode']" in handlers
    assert "$cart->getCouponItems()" in handlers
    assert "$_POST" not in handlers
    assert "$order->getCart()" in handlers


def test_configurable_promo_prompt_is_added_to_cart_and_checkout() -> None:
    handlers = (MODULE / "config" / "handlers.inc.php").read_text()
    script = PROMO_PROMPT.read_text()

    assert "michelangeloPromoRequirement" in handlers
    assert "promo-required-2.3.2.css" in handlers
    assert "promo-required-2.3.2.js" in handlers
    assert "promo_prompt_text" in handlers
    assert "#coupon_input" in script
    assert 'input[name="coupon"]' in script
    assert ".rs-go-checkout" in script
    assert ".rs-checkout_submitButton" in script
    assert "prompt.textContent = text" in script
    assert "MutationObserver" in script
    assert "getCouponLabel" in script
    assert "var anchor = label && label.parentNode ? label : form" in script


def test_customer_cancellation_uses_readyscript_delivery_contract() -> None:
    cancel_method = (
        MODULE / "model" / "externalapi" / "michelangelo" / "cancelorder.inc.php"
    ).read_text()

    assert "class CancelOrder extends AbstractAuthorizedMethod" in cancel_method
    assert "hash_equals" in cancel_method
    assert "InterfaceDeliveryOrder" in cancel_method
    assert "deleteDeliveryOrder($delivery_order)" in cancel_method
    assert "UserStatus::STATUS_CANCELLED" in cancel_method
    assert "$order->update()" in cancel_method
    assert "deleteDeliveryOrder($delivery_order)" in cancel_method
    delivery_call = cancel_method.index("$deleted_count = $this->deleteDeliveryOrders($order)")
    status_change = cancel_method.index("$order['status'] = reset($cancelled_statuses)")
    assert delivery_call < status_change
