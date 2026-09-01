/**
 * Показывает на витрине настраиваемое требование ввести промокод.
 * MutationObserver повторяет вставку после AJAX-перерисовки корзины/checkout.
 */
(function () {
    'use strict';

    var settings = (window.global && window.global.michelangeloPromoRequirement)
        || window.michelangeloPromoRequirement
        || {};
    var text = String(settings.text || '').replace(/^\s+|\s+$/g, '');
    var scheduled = false;

    if (!text) {
        return;
    }

    function getCouponInputs() {
        return document.querySelectorAll(
            '#coupon_input:not([disabled]),'
            + 'input[name="coupon"]:not([type="hidden"]):not([disabled]),'
            + 'input[name$="[coupon]"]:not([type="hidden"]):not([disabled])'
        );
    }

    function closest(element, selector) {
        while (element && element.nodeType === 1) {
            if (element.matches && element.matches(selector)) {
                return element;
            }
            element = element.parentElement;
        }
        return null;
    }

    function getCouponLabel(input) {
        if (input.labels && input.labels.length) {
            return input.labels[0];
        }
        if (!input.id) {
            return null;
        }
        var labels = document.getElementsByTagName('label');
        for (var i = 0; i < labels.length; i++) {
            if (labels[i].getAttribute('for') === input.id) {
                return labels[i];
            }
        }
        return null;
    }

    function isCouponApplied(inputs) {
        if (document.querySelector('.promocode-form input[disabled], input[data-coupon-applied]')) {
            return true;
        }

        if (inputs.length) {
            for (var i = 0; i < inputs.length; i++) {
                var zone = closest(inputs[i], '.checkout-block, .cart-aside, [data-coupon], .coupon');
                if (zone && zone.querySelector('.rs-remove, [data-remove-coupon]')) {
                    return true;
                }
            }
            return false;
        }

        return settings.applied === true || settings.applied === 1;
    }

    function makePrompt(modifier) {
        var prompt = document.createElement('div');
        prompt.className = 'michelangelo-promo-required ' + modifier;
        prompt.setAttribute('role', 'note');
        prompt.textContent = text;
        return prompt;
    }

    function insertBeforeOnce(anchor, modifier) {
        if (!anchor || !anchor.parentNode) {
            return;
        }
        var previous = anchor.previousElementSibling;
        if (previous && previous.classList.contains(modifier)) {
            return;
        }
        anchor.parentNode.insertBefore(makePrompt(modifier), anchor);
    }

    function removePrompts() {
        var prompts = document.querySelectorAll('.michelangelo-promo-required');
        for (var i = 0; i < prompts.length; i++) {
            if (prompts[i].parentNode) {
                prompts[i].parentNode.removeChild(prompts[i]);
            }
        }
    }

    function renderPrompts() {
        var inputs = getCouponInputs();
        if (isCouponApplied(inputs)) {
            removePrompts();
            return;
        }

        for (var i = 0; i < inputs.length; i++) {
            var form = closest(inputs[i], '.promocode-form') || inputs[i];
            var label = getCouponLabel(inputs[i]);
            var anchor = label && label.parentNode ? label : form;
            insertBeforeOnce(anchor, 'michelangelo-promo-required--field');
        }

        var buttons = document.querySelectorAll(
            '.rs-go-checkout, .rs-checkout_submitButton,'
            + ' .rs-checkout_form button[type="submit"],'
            + ' form[action*="checkout"] button[type="submit"]'
        );
        for (var j = 0; j < buttons.length; j++) {
            insertBeforeOnce(buttons[j], 'michelangelo-promo-required--checkout');
        }
    }

    function scheduleRender() {
        if (scheduled) {
            return;
        }
        scheduled = true;
        var schedule = window.requestAnimationFrame || function (callback) {
            return window.setTimeout(callback, 0);
        };
        schedule(function () {
            scheduled = false;
            renderPrompts();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', scheduleRender);
    } else {
        scheduleRender();
    }

    if (window.MutationObserver) {
        var observer = new MutationObserver(scheduleRender);
        observer.observe(document.documentElement, { childList: true, subtree: true });
    }
}());
