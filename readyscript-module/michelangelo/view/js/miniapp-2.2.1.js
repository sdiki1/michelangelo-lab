/**
 * Передаёт подписанный WebApp initData в ReadyScript-сессию.
 *
 * Внешние SDK загружаются только после полной загрузки страницы. Поэтому
 * недоступность telegram.org или st.max.ru никогда не блокирует витрину.
 */
(function () {
    'use strict';

    var settings = (window.global && window.global.michelangeloIdentity)
        || window.michelangeloIdentity
        || {};
    var endpoint = settings.url || '/michelangelo/track/';
    var launch = mergeLaunchParams(
        loadStoredLaunchParams(),
        parseLaunchParams(location.search),
        parseLaunchParams(location.hash)
    );
    storeLaunchParams(launch);
    var platform = detectPlatform(launch);
    var identitySent = false;
    var diagnosticSent = false;

    signalPlatformReady(platform);
    trySendIdentity();
    schedulePlatformSdk(platform);

    function trySendIdentity() {
        if (identitySent) {
            return true;
        }
        var identity = detectIdentity(launch);
        if (!identity) {
            return false;
        }
        identitySent = true;
        fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(identity),
            credentials: 'same-origin',
            keepalive: true
        }).then(function (response) {
            if (settings.debug && !response.ok && window.console) {
                console.warn('Michelangelo identity rejected, HTTP ' + response.status);
            }
        })['catch'](function (error) {
            if (settings.debug && window.console) {
                console.warn('Michelangelo identity request failed', error);
            }
        });
        return true;
    }

    function detectIdentity(params) {
        var telegram = window.Telegram && window.Telegram.WebApp;
        if (telegram && telegram.initData) {
            return { platform: 'telegram', init_data: telegram.initData };
        }

        var max = getMaxWebApp();
        if (max && max.initData) {
            return { platform: 'max', init_data: max.initData };
        }

        if (params.tgWebAppData) {
            return { platform: 'telegram', init_data: params.tgWebAppData };
        }
        if (params.WebAppData) {
            return { platform: 'max', init_data: params.WebAppData };
        }
        return null;
    }

    function detectPlatform(params) {
        var telegram = window.Telegram && window.Telegram.WebApp;
        if (telegram && (telegram.initData || telegram.platform)) {
            return 'telegram';
        }
        var max = getMaxWebApp();
        if (max && (max.initData || max.platform)) {
            return 'max';
        }
        if (params.tgWebAppData || params.tgWebAppPlatform || params.tgWebAppVersion) {
            return 'telegram';
        }
        if (params.WebAppData || params.WebAppPlatform || params.WebAppVersion) {
            return 'max';
        }
        if (window.TelegramWebviewProxy) {
            return 'telegram';
        }
        if (window.WebViewHandler) {
            return 'max';
        }
        return null;
    }

    function getMaxWebApp() {
        return window.WebApp
            || (window.MAX && (window.MAX.WebApp || window.MAX));
    }

    function signalPlatformReady(activePlatform) {
        try {
            if (activePlatform === 'telegram') {
                var telegram = window.Telegram && window.Telegram.WebApp;
                if (telegram && typeof telegram.ready === 'function') {
                    telegram.ready();
                    return;
                }
                signalTelegramReadyWithoutSdk();
            } else if (activePlatform === 'max') {
                var max = getMaxWebApp();
                if (max && typeof max.ready === 'function') {
                    max.ready();
                    return;
                }
                signalMaxReadyWithoutSdk();
            }
        } catch (error) {
            debugWarn('Messenger WebApp.ready failed', error);
        }
    }

    // Минимальные ready-сообщения повторяют транспорт официальных SDK. Они
    // нужны до обращения к CDN, чтобы нативный экран загрузки не зависал.
    function signalTelegramReadyWithoutSdk() {
        if (window.TelegramWebviewProxy
            && typeof window.TelegramWebviewProxy.postEvent === 'function') {
            window.TelegramWebviewProxy.postEvent('web_app_ready', JSON.stringify(''));
        } else if (window.external && typeof window.external.notify === 'function') {
            window.external.notify(JSON.stringify({
                eventType: 'web_app_ready',
                eventData: ''
            }));
        } else if (window.parent && window.parent !== window) {
            window.parent.postMessage(JSON.stringify({
                eventType: 'web_app_ready',
                eventData: ''
            }), '*');
        }
    }

    function signalMaxReadyWithoutSdk() {
        if (window.WebViewHandler && typeof window.WebViewHandler.postEvent === 'function') {
            window.WebViewHandler.postEvent('WebAppReady', JSON.stringify({}));
        } else if (window.parent && window.parent !== window) {
            window.parent.postMessage(JSON.stringify({ type: 'WebAppReady' }), '*');
        }
    }

    function schedulePlatformSdk(activePlatform) {
        if (!activePlatform || platformSdkAvailable(activePlatform)) {
            afterSdkAttempt(activePlatform);
            return;
        }

        var start = function () {
            loadPlatformSdk(activePlatform);
        };
        if (document.readyState === 'complete') {
            window.setTimeout(start, 0);
        } else {
            window.addEventListener('load', start, { once: true });
        }
    }

    function platformSdkAvailable(activePlatform) {
        if (activePlatform === 'telegram') {
            return Boolean(window.Telegram && window.Telegram.WebApp);
        }
        return Boolean(getMaxWebApp());
    }

    function loadPlatformSdk(activePlatform) {
        var script = document.createElement('script');
        var finished = false;
        var timer;
        script.async = true;
        script.src = activePlatform === 'telegram'
            ? 'https://telegram.org/js/telegram-web-app.js?63'
            : 'https://st.max.ru/js/max-web-app.js';
        script.onload = function () {
            finish();
        };
        script.onerror = function () {
            debugWarn('Messenger SDK failed to load: ' + activePlatform);
            finish();
        };
        timer = window.setTimeout(function () {
            debugWarn('Messenger SDK load timeout: ' + activePlatform);
            if (script.parentNode) {
                script.parentNode.removeChild(script);
            }
            finish();
        }, 5000);
        (document.head || document.documentElement).appendChild(script);

        function finish() {
            if (finished) {
                return;
            }
            finished = true;
            window.clearTimeout(timer);
            afterSdkAttempt(activePlatform);
        }
    }

    function afterSdkAttempt(activePlatform) {
        signalPlatformReady(activePlatform);
        if (!trySendIdentity()) {
            sendDiagnosticProbe();
        }
    }

    function parseLaunchParams(value) {
        var result = {};
        var source = String(value || '').replace(/^[#?]/, '');
        source.split('&').forEach(function (part) {
            var separator = part.indexOf('=');
            if (separator < 0) {
                return;
            }
            try {
                var key = urlSafeDecode(part.slice(0, separator));
                var decodedValue = urlSafeDecode(part.slice(separator + 1));
                if (isLaunchKey(key)) {
                    result[key] = decodedValue;
                }
            } catch (error) {
                debugWarn('Invalid miniapp launch parameters', error);
            }
        });
        return result;
    }

    function urlSafeDecode(value) {
        return decodeURIComponent(String(value || '').replace(/\+/g, '%20'));
    }

    function isLaunchKey(key) {
        return [
            'tgWebAppData',
            'tgWebAppPlatform',
            'tgWebAppVersion',
            'WebAppData',
            'WebAppPlatform',
            'WebAppVersion',
            'WebAppDeviceName'
        ].indexOf(key) !== -1;
    }

    function mergeLaunchParams() {
        var result = {};
        Array.prototype.slice.call(arguments).forEach(function (source) {
            Object.keys(source || {}).forEach(function (key) {
                if (isLaunchKey(key) && source[key] !== null && source[key] !== '') {
                    result[key] = source[key];
                }
            });
        });
        return result;
    }

    function loadStoredLaunchParams() {
        var result = {};
        try {
            result = mergeLaunchParams(
                JSON.parse(window.sessionStorage.getItem('michelangelo_launch_params') || '{}'),
                JSON.parse(window.sessionStorage.getItem('__telegram__initParams') || '{}')
            );
            [
                'WebAppData',
                'WebAppPlatform',
                'WebAppVersion',
                'WebAppDeviceName'
            ].forEach(function (key) {
                var value = window.sessionStorage.getItem(key);
                if (value) {
                    result[key] = value;
                }
            });
        } catch (error) {
            debugWarn('Mini App sessionStorage is unavailable', error);
        }
        return result;
    }

    function storeLaunchParams(params) {
        if (!params.tgWebAppData && !params.WebAppData) {
            return;
        }
        try {
            window.sessionStorage.setItem(
                'michelangelo_launch_params',
                JSON.stringify(params)
            );
        } catch (error) {
            debugWarn('Mini App launch parameters were not cached', error);
        }
    }

    function sendDiagnosticProbe() {
        // Обычный браузер не должен делать лишний POST на каждой странице.
        if (!platform || diagnosticSent || !settings.debug || !window.fetch) {
            return;
        }
        diagnosticSent = true;
        var telegram = window.Telegram && window.Telegram.WebApp;
        var max = getMaxWebApp();
        fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            keepalive: true,
            body: JSON.stringify({
                action: 'diagnostic',
                path: location.pathname,
                detected_platform: platform,
                telegram_object: Boolean(telegram),
                telegram_init_data_length: telegram
                    ? String(telegram.initData || '').length
                    : 0,
                telegram_platform: telegram ? telegram.platform : null,
                telegram_version: telegram ? telegram.version : null,
                max_object: Boolean(max),
                max_init_data_length: max ? String(max.initData || '').length : 0,
                max_platform: max ? max.platform : null,
                max_version: max ? max.version : null,
                hash_has_telegram: Boolean(launch.tgWebAppData),
                hash_has_max: Boolean(launch.WebAppData),
                referrer: document.referrer || null,
                user_agent: navigator.userAgent || null
            })
        })['catch'](function () {});
    }

    function debugWarn(message, error) {
        if (settings.debug && window.console) {
            console.warn(message, error || '');
        }
    }
})();
