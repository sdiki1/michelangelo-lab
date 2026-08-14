/**
 * Передаёт подписанный WebApp initData в ReadyScript-сессию до оформления заказа.
 */
(function () {
    'use strict';

    // ReadyScript 6 складывает addJsVar в window.global.
    var settings = (window.global && window.global.michelangeloIdentity)
        || window.michelangeloIdentity
        || {};
    var endpoint = settings.url || '/michelangelo/track/';
    var identity = detectIdentity();
    if (!identity) {
        sendDiagnosticProbe();
        return;
    }

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
        return response;
    })['catch'](function (error) {
        if (settings.debug && window.console) {
            console.warn('Michelangelo identity request failed', error);
        }
    });

    function detectIdentity() {
        var telegram = window.Telegram && window.Telegram.WebApp;
        if (telegram && telegram.initData) {
            return { platform: 'telegram', init_data: telegram.initData };
        }

        var max = window.WebApp
            || (window.MAX && (window.MAX.WebApp || window.MAX));
        if (max && max.initData && max.initDataUnsafe && max.initDataUnsafe.user) {
            return { platform: 'max', init_data: max.initData };
        }

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

    function sendDiagnosticProbe() {
        if (!settings.debug || !window.fetch) {
            return;
        }
        var launch = parseFragment(location.hash);
        fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({
                action: 'diagnostic',
                path: location.pathname,
                telegram_object: Boolean(window.Telegram && window.Telegram.WebApp),
                telegram_init_data_length: window.Telegram && window.Telegram.WebApp
                    ? String(window.Telegram.WebApp.initData || '').length
                    : 0,
                telegram_platform: window.Telegram && window.Telegram.WebApp
                    ? window.Telegram.WebApp.platform
                    : null,
                telegram_version: window.Telegram && window.Telegram.WebApp
                    ? window.Telegram.WebApp.version
                    : null,
                max_object: Boolean(window.WebApp || window.MAX),
                hash_has_telegram: Boolean(launch.tgWebAppData),
                hash_has_max: Boolean(launch.WebAppData),
                referrer: document.referrer || null,
                user_agent: navigator.userAgent || null
            })
        })['catch'](function () {});
    }
})();
