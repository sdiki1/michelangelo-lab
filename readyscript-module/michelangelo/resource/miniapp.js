/**
 * Michelangelo: сбор действий на витрине, открытой в миниаппе.
 *
 * Что делает:
 *   1. определяет, что страницу открыли из Telegram или MAX;
 *   2. один раз за сессию отправляет личность на /michelangelo/track/;
 *   3. копит действия пользователя и отправляет их пачками.
 *
 * Аналитика не должна мешать покупателю: всё уходит фоном, ошибки сети
 * гасятся молча, отправка пачкой — не чаще раза в 5 секунд.
 */
(function () {
    'use strict';

    var settings = window.michelangeloTrack || {};
    var endpoint = settings.url || '/michelangelo/track/';
    var onlyMiniapp = settings.onlyMiniapp !== false;

    var FLUSH_INTERVAL_MS = 5000;
    var MAX_BATCH = 50;
    var IDENTITY_SENT_KEY = 'michelangelo_identity_sent';

    var queue = [];
    var flushTimer = null;

    function detectPlatform() {
        var tg = window.Telegram && window.Telegram.WebApp;
        if (tg && tg.initData) {
            return {
                platform: 'telegram',
                init_data: tg.initData,
                platform_user_id: tg.initDataUnsafe
                    && tg.initDataUnsafe.user
                    && String(tg.initDataUnsafe.user.id)
            };
        }

        // У MAX свой объект WebApp; подписанного initData он не даёт,
        // поэтому такой id считается предварительным и проверяется отдельно.
        var max = window.MAX && (window.MAX.WebApp || window.MAX);
        var maxUser = max && (max.initDataUnsafe && max.initDataUnsafe.user || max.user);
        if (maxUser && maxUser.id) {
            return {
                platform: 'max',
                platform_user_id: String(maxUser.id),
                username: maxUser.username,
                first_name: maxUser.first_name,
                last_name: maxUser.last_name
            };
        }
        return null;
    }

    function post(body) {
        var payload = JSON.stringify(body);

        // sendBeacon переживает закрытие вкладки — важно для последней пачки.
        if (navigator.sendBeacon) {
            try {
                var blob = new Blob([payload], { type: 'application/json' });
                if (navigator.sendBeacon(endpoint, blob)) {
                    return;
                }
            } catch (error) {
                // падаем в fetch ниже
            }
        }

        if (window.fetch) {
            fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: payload,
                credentials: 'same-origin',
                keepalive: true
            })['catch'](function () { /* аналитика не ломает страницу */ });
        }
    }

    function sendIdentity(identity) {
        try {
            if (window.sessionStorage
                && sessionStorage.getItem(IDENTITY_SENT_KEY) === identity.platform) {
                return;
            }
        } catch (error) {
            // приватный режим — просто отправим ещё раз
        }

        var body = { action: 'identity' };
        for (var key in identity) {
            if (Object.prototype.hasOwnProperty.call(identity, key) && identity[key]) {
                body[key] = identity[key];
            }
        }
        post(body);

        try {
            if (window.sessionStorage) {
                sessionStorage.setItem(IDENTITY_SENT_KEY, identity.platform);
            }
        } catch (error) { /* не критично */ }
    }

    function eventId() {
        if (window.crypto && crypto.randomUUID) {
            return crypto.randomUUID();
        }
        return String(Date.now()) + '-' + Math.random().toString(36).slice(2, 12);
    }

    function flush() {
        flushTimer = null;
        if (!queue.length) {
            return;
        }
        var batch = queue.splice(0, MAX_BATCH);
        post({ action: 'events', events: batch });
    }

    function scheduleFlush() {
        if (flushTimer === null) {
            flushTimer = window.setTimeout(flush, FLUSH_INTERVAL_MS);
        }
    }

    function track(action, extra) {
        var event = {
            event_id: eventId(),
            action: action,
            path: location.pathname + location.search,
            title: document.title,
            referrer: document.referrer || undefined,
            occurred_at: new Date().toISOString()
        };
        if (extra) {
            for (var key in extra) {
                if (Object.prototype.hasOwnProperty.call(extra, key) && extra[key]) {
                    event[key] = extra[key];
                }
            }
        }
        queue.push(event);
        scheduleFlush();
    }

    /**
     * Товар текущей страницы — из микроразметки, которую RS уже отдаёт.
     */
    function currentProduct() {
        var node = document.querySelector('[itemtype*="schema.org/Product"]');
        if (!node) {
            return null;
        }
        var titleNode = node.querySelector('[itemprop="name"]');
        var idNode = node.querySelector('[itemprop="sku"], [itemprop="productID"]');
        return {
            product_id: idNode && (idNode.getAttribute('content') || idNode.textContent.trim()),
            product_title: titleNode
                && (titleNode.getAttribute('content') || titleNode.textContent.trim())
        };
    }

    function bindInteractions() {
        document.addEventListener('click', function (domEvent) {
            var target = domEvent.target.closest
                && domEvent.target.closest('[data-ml-action], .add-to-cart, [href*="/cart"]');
            if (!target) {
                return;
            }
            var action = target.getAttribute('data-ml-action')
                || (target.classList.contains('add-to-cart') ? 'add_to_cart' : 'open_cart');
            track(action, currentProduct());
        }, true);

        // Последняя пачка при уходе со страницы.
        window.addEventListener('pagehide', flush);
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'hidden') {
                flush();
            }
        });
    }

    function start() {
        var identity = detectPlatform();
        if (!identity && onlyMiniapp) {
            return;
        }
        if (identity) {
            sendIdentity(identity);
        }

        track('page_view', currentProduct());
        bindInteractions();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    // Ручная разметка событий из шаблонов: michelangelo.track('checkout_step', {...})
    window.michelangelo = { track: track, flush: flush };
})();
