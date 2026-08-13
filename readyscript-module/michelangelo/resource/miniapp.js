/**
 * Передаёт подписанный WebApp initData в ReadyScript-сессию до оформления заказа.
 */
(function () {
    'use strict';

    var settings = window.michelangeloIdentity || {};
    var endpoint = settings.url || '/michelangelo/track/';
    var identity = detectIdentity();
    if (!identity) {
        return;
    }

    fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(identity),
        credentials: 'same-origin',
        keepalive: true
    })['catch'](function () {
        // Ошибка привязки не должна ломать витрину или оформление заказа.
    });

    function detectIdentity() {
        var telegram = window.Telegram && window.Telegram.WebApp;
        if (telegram && telegram.initData) {
            return { platform: 'telegram', init_data: telegram.initData };
        }

        // Официальный MAX Bridge публикует window.WebApp. Старые алиасы
        // поддержаны только для совместимости с ранними версиями интеграции.
        var max = window.WebApp
            || (window.MAX && (window.MAX.WebApp || window.MAX));
        if (max && max.initData && max.initDataUnsafe && max.initDataUnsafe.user) {
            return { platform: 'max', init_data: max.initData };
        }

        // Fallback не зависит от наличия Bridge SDK: оба клиента передают
        // подписанные данные в URL fragment при открытии мини-приложения.
        var launch = parseFragment(location.hash);
        if (launch.tgWebAppData) {
            return { platform: 'telegram', init_data: launch.tgWebAppData };
        }
        if (launch.WebAppData) {
            return { platform: 'max', init_data: launch.WebAppData };
        }

        return null;
    }


    function parseFragment(fragment) {
        var result = {};
        var source = String(fragment || '').replace(/^#/, '');
        source.split('&').forEach(function (part) {
            var separator = part.indexOf('=');
            if (separator < 0) {
                return;
            }
            var key = decodeURIComponent(part.slice(0, separator));
            var value = decodeURIComponent(part.slice(separator + 1));
            result[key] = value;
        });
        return result;
    }
})();
