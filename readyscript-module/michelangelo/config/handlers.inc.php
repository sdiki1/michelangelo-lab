<?php
namespace Michelangelo\Config;

use Michelangelo\Model\Identity;
use Michelangelo\Model\Diagnostic;
use RS\Orm\Type;

/**
 * Связывает сессию мини-приложения с оформляемым заказом.
 */
class Handlers extends \RS\Event\HandlerAbstract
{
    function init()
    {
        $this
            ->bind('orm.init.shop-order')
            ->bind('orm.beforewrite.shop-order')
            ->bind('orm.afterwrite.shop-order')
            ->bind('controller.beforewrap')
            ->bind('getroute');
    }

    /**
     * Поля хранятся непосредственно в shop_order и доступны во внешнем API.
     */
    public static function ormInitShopOrder(\Shop\Model\Orm\Order $order)
    {
        $order->getPropertyIterator()->append([
            'telegram_user_id' => new Type\Varchar([
                'maxLength' => 128,
                'description' => 'Telegram user ID',
                'infoVisible' => true,
                'appVisible' => true,
            ]),
            'max_user_id' => new Type\Varchar([
                'maxLength' => 128,
                'description' => 'MAX user ID',
                'infoVisible' => true,
                'appVisible' => true,
            ]),
            'ml_bind_source' => new Type\Varchar([
                'maxLength' => 32,
                'description' => 'Источник привязки мессенджера',
                'visible' => false,
                'appVisible' => true,
            ]),
            // Служебное runtime-поле участвует в штатной валидации заказа.
            // condition нужен для пошагового checkout/API и позволяет
            // ReadyScript удалить этот checker при редактировании в админке.
            'ml_promo_gate' => new Type\Integer([
                'description' => 'Проверка обязательного промокода',
                'runtime' => true,
                'visible' => false,
                'appVisible' => false,
                'meVisible' => false,
                'condition' => ['step' => 'confirm'],
                'Checker' => [[__CLASS__, 'checkRequiredPromoCode'], ''],
            ]),

            // Старые поля оставлены на один переходный период. Они позволяют
            // без потери данных обновить уже установленную версию модуля.
            'ml_platform' => new Type\Varchar([
                'maxLength' => 32,
                'description' => 'Мессенджер (устаревшее поле)',
                'visible' => false,
                'appVisible' => false,
            ]),
            'ml_platform_user_id' => new Type\Varchar([
                'maxLength' => 128,
                'description' => 'ID в мессенджере (устаревшее поле)',
                'visible' => false,
                'appVisible' => false,
            ]),
            'ml_init_data' => new Type\Text([
                'description' => 'initData (устаревшее поле)',
                'visible' => false,
                'appVisible' => false,
            ]),
        ]);
    }

    /**
     * Пропускает заказ только с реально применённым действующим купоном.
     * Проверка непустого POST-поля недостаточна: его легко подделать, а
     * ReadyScript сам не добавляет в корзину неизвестный или истёкший купон.
     */
    public static function checkRequiredPromoCode(\Shop\Model\Orm\Order $order, $value, $error)
    {
        $config = \RS\Config\Loader::byModule('michelangelo');
        if (empty($config['require_promo_code'])) {
            return true;
        }

        $cart = $order->getCart();
        if ($cart && count($cart->getCouponItems()) > 0) {
            return true;
        }

        $message = trim((string)$config['promo_required_message']);
        return $message !== ''
            ? $message
            : 'Для оформления заказа необходимо ввести действующий промокод.';
    }

    /**
     * Записывает проверенный ID в новый заказ. Ручные значения администратора
     * и уже существующую привязку обработчик не перезаписывает.
     */
    public static function ormBeforeWriteShopOrder($params)
    {
        $order = self::extractOrder($params);
        if (!$order) {
            Diagnostic::write('order.before.no_order', [
                'session' => Diagnostic::sessionFingerprint(),
                'params_type' => gettype($params),
            ]);
            return $params;
        }

        self::migrateLegacyIdentity($order);

        $identity = Identity::current();
        Diagnostic::write('order.before', [
            'order_id' => isset($order['id']) ? $order['id'] : null,
            'session' => Diagnostic::sessionFingerprint(),
            'identity_present' => (bool)$identity,
            'identity_verified' => $identity && !empty($identity['verified']),
            'platform' => $identity ? $identity['platform'] : null,
            'telegram_before' => Diagnostic::maskedUserId($order['telegram_user_id']),
            'max_before' => Diagnostic::maskedUserId($order['max_user_id']),
        ]);
        if (!$identity || empty($identity['verified'])) {
            return $params;
        }

        $field = Identity::orderField($identity['platform']);
        if ($field && empty($order[$field])) {
            $order[$field] = $identity['platform_user_id'];
            $order['ml_bind_source'] = 'miniapp_verified';
            Diagnostic::write('order.identity_stamped', [
                'order_id' => isset($order['id']) ? $order['id'] : null,
                'session' => Diagnostic::sessionFingerprint(),
                'field' => $field,
                'user_id' => Diagnostic::maskedUserId($identity['platform_user_id']),
            ]);
        }

        return $params;
    }

    public static function ormAfterWriteShopOrder($params)
    {
        $order = self::extractOrder($params);
        Diagnostic::write('order.after', [
            'order_found' => (bool)$order,
            'order_id' => $order && isset($order['id']) ? $order['id'] : null,
            'session' => Diagnostic::sessionFingerprint(),
            'write_flag' => is_array($params) && isset($params['flag']) ? $params['flag'] : null,
            'telegram_saved' => $order ? Diagnostic::maskedUserId($order['telegram_user_id']) : null,
            'max_saved' => $order ? Diagnostic::maskedUserId($order['max_user_id']) : null,
            'bind_source' => $order && isset($order['ml_bind_source'])
                ? $order['ml_bind_source']
                : null,
        ]);
        return $params;
    }

    /**
     * Переносит значение, записанное предыдущей версией модуля.
     */
    protected static function migrateLegacyIdentity(\Shop\Model\Orm\Order $order)
    {
        if (empty($order['ml_platform']) || empty($order['ml_platform_user_id'])) {
            return;
        }

        $field = Identity::orderField($order['ml_platform']);
        if ($field && empty($order[$field])) {
            $order[$field] = (string)$order['ml_platform_user_id'];
            if (empty($order['ml_bind_source'])) {
                $order['ml_bind_source'] = 'legacy';
            }
        }
    }

    public static function controllerBeforeWrap($params)
    {
        $config = \RS\Config\Loader::byModule('michelangelo');
        if (\RS\Router\Manager::obj()->isAdminZone()) {
            return $params;
        }

        $app = \RS\Application\Application::getInstance();
        if (!empty($config['enabled'])) {
            // Внешние SDK здесь намеренно не подключаются: синхронный script в
            // HEAD блокирует всю витрину, если CDN мессенджера недоступен.
            // miniapp-2.2.1.js читает initData из URL и после window.load
            // асинхронно загружает только SDK фактической платформы.
            $app->addJsVar('michelangeloIdentity', [
                'url' => \RS\Router\Manager::obj()->getUrl('michelangelo-front-track'),
                'debug' => !empty($config['diagnostic_log']),
            ]);
            // Версия включена в имя файла: Telegram/MAX WebView агрессивно
            // кэшируют JavaScript и иначе могут исполнять старый код.
            $app->addJs('%michelangelo%/miniapp-2.2.1.js');
        }

        if (!empty($config['require_promo_code'])) {
            $prompt = trim((string)$config['promo_prompt_text']);
            if ($prompt === '') {
                $prompt = 'Введите промокод — без него оформить заказ нельзя.';
            }

            $cart = \Shop\Model\Cart::currentCart();
            $app->addJsVar('michelangeloPromoRequirement', [
                'text' => $prompt,
                'applied' => $cart && count($cart->getCouponItems()) > 0,
            ]);
            $app->addCss('%michelangelo%/promo-required-2.3.2.css');
            $app->addJs('%michelangelo%/promo-required-2.3.2.js');
        }
        return $params;
    }

    public static function getRoute($routes)
    {
        $routes[] = new \RS\Router\Route(
            'michelangelo-front-track',
            ['/michelangelo/track/'],
            null,
            'Michelangelo: привязка пользователя мини-приложения'
        );
        return $routes;
    }

    protected static function extractOrder($params)
    {
        if ($params instanceof \Shop\Model\Orm\Order) {
            return $params;
        }
        if (is_array($params)) {
            foreach (['orm', 'order', 'object'] as $key) {
                if (isset($params[$key]) && $params[$key] instanceof \Shop\Model\Orm\Order) {
                    return $params[$key];
                }
            }
        }
        return null;
    }
}
