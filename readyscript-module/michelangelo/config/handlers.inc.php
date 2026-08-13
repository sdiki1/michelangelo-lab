<?php
namespace Michelangelo\Config;

use Michelangelo\Model\Api;
use Michelangelo\Model\Identity;
use RS\Orm\Type;

/**
 * Подписки модуля на события ReadyScript.
 *
 * Имя метода-обработчика совпадает с именем события: bind('ormInitShopOrder')
 * вызовет статический ormInitShopOrder().
 */
class Handlers extends \RS\Event\HandlerAbstract
{
    function init()
    {
        $this
            // Свои поля в заказе: чей это Telegram/MAX и откуда мы это узнали.
            ->bind('ormInitShopOrder')
            // Новый заказ забирает личность из сессии миниаппа.
            ->bind('ormBeforeWriteShopOrder')
            // Заказ создан или изменён — уводим в админ-панель.
            ->bind('ormAfterWriteShopOrder')
            // Скрипт миниаппа на витрине.
            ->bind('controllerBeforewrap')
            // Маршрут приёмника событий с фронта.
            ->bind('getRoute')
            // Досылка накопившейся очереди.
            ->bind('cron');
    }

    /**
     * Добавляет в заказ поля привязки к мессенджеру.
     */
    public static function ormInitShopOrder(\Shop\Model\Orm\Order $order)
    {
        $order->getPropertyIterator()->append([
            'ml_platform' => new Type\Varchar([
                'maxLength' => 32,
                'description' => 'Мессенджер',
                'listFromArray' => [[
                    '' => 'не привязан',
                    Identity::PLATFORM_TELEGRAM => 'Telegram',
                    Identity::PLATFORM_MAX => 'MAX',
                ]],
                'visible' => true,
            ]),
            'ml_platform_user_id' => new Type\Varchar([
                'maxLength' => 128,
                'description' => 'ID пользователя в мессенджере',
                'visible' => true,
            ]),
            'ml_bind_source' => new Type\Varchar([
                'maxLength' => 32,
                'description' => 'Источник привязки',
                'visible' => false,
            ]),
            'ml_init_data' => new Type\Text([
                'description' => 'Telegram initData (для проверки подписи в админ-панели)',
                'visible' => false,
            ]),
        ]);
    }

    /**
     * Штампует заказ личностью из сессии миниаппа.
     *
     * Срабатывает до записи, поэтому привязка попадает в заказ сразу при
     * оформлении. Уже проставленную привязку не трогаем — админ мог указать
     * её вручную, и перезаписывать его правку сессией нельзя.
     */
    public static function ormBeforeWriteShopOrder($params)
    {
        $order = self::extractOrder($params);
        if (!$order || !empty($order['ml_platform_user_id'])) {
            return $params;
        }

        $identity = Identity::current();
        if (!$identity) {
            return $params;
        }

        $order['ml_platform'] = $identity['platform'];
        $order['ml_platform_user_id'] = $identity['platform_user_id'];
        $order['ml_bind_source'] = 'miniapp';
        if (!empty($identity['init_data'])) {
            $order['ml_init_data'] = $identity['init_data'];
        }
        return $params;
    }

    /**
     * Заказ сохранён — ставим вебхук в очередь.
     *
     * Приёмник делает upsert по external_order_id, поэтому повторная отправка
     * того же заказа безопасна: отдельно различать создание и изменение не нужно.
     */
    public static function ormAfterWriteShopOrder($params)
    {
        $order = self::extractOrder($params);
        if (!$order) {
            return $params;
        }

        $api = new Api();
        if (!$api->isEnabled()) {
            return $params;
        }

        $api->enqueue(Api::ENDPOINT_ORDERS, self::buildOrderPayload($order));
        return $params;
    }

    /**
     * Собирает тело вебхука по заказу.
     */
    public static function buildOrderPayload(\Shop\Model\Orm\Order $order)
    {
        $identity = null;
        if (!empty($order['ml_platform'])) {
            $identity = array_filter([
                'platform' => $order['ml_platform'],
                'platform_user_id' => $order['ml_platform_user_id'],
                'init_data' => $order['ml_init_data'],
            ], function ($value) {
                return $value !== null && $value !== '';
            });
        }

        return array_filter([
            'event' => 'order.updated',
            'order_id' => self::field($order, ['id']),
            'order_num' => self::field($order, ['order_num', 'number']),
            'status' => self::field($order, ['status', 'status_title']),
            'total' => self::field($order, ['totalcost', 'total_cost', 'total']),
            'currency' => self::field($order, ['currency_stitle', 'currency']),
            'customer_name' => self::field($order, ['user_fio', 'contact_person']),
            'customer_phone' => self::field($order, ['user_phone', 'phone']),
            'customer_email' => self::field($order, ['user_email', 'e_mail', 'email']),
            'identity' => $identity,
            'items' => self::buildOrderItems($order),
            'occurred_at' => date('c'),
        ], function ($value) {
            return $value !== null && $value !== '' && $value !== [];
        });
    }

    /**
     * Первое непустое из перечисленных полей заказа.
     *
     * Имена свойств ORM и полей REST API у ReadyScript местами расходятся
     * (totalcost против total_cost), поэтому перебираем известные варианты.
     */
    protected static function field(\Shop\Model\Orm\Order $order, array $names)
    {
        foreach ($names as $name) {
            if (!isset($order[$name])) {
                continue;
            }
            $value = trim((string)$order[$name]);
            if ($value !== '') {
                return $value;
            }
        }
        return null;
    }

    protected static function buildOrderItems(\Shop\Model\Orm\Order $order)
    {
        $items = [];
        try {
            $cart = $order->getCart();
            if (!$cart) {
                return $items;
            }
            foreach ($cart->getCartItems() as $item) {
                $items[] = array_filter([
                    'title' => isset($item['title']) ? (string)$item['title'] : null,
                    'amount' => isset($item['amount']) ? (string)$item['amount'] : null,
                    'price' => isset($item['price']) ? (string)$item['price'] : null,
                ]);
            }
        } catch (\Exception $e) {
            // Состав заказа — приятное дополнение, а не повод потерять вебхук.
            return [];
        }
        return $items;
    }

    /**
     * Подключает JS миниаппа на витрине.
     */
    public static function controllerBeforewrap($params)
    {
        $config = \RS\Config\Loader::byModule('michelangelo');
        if (empty($config['enabled']) || empty($config['track_site_events'])) {
            return $params;
        }

        $app = \RS\Application\Application::getInstance();
        $app->addJsVar('michelangeloTrack', [
            'url' => \RS\Router\Manager::obj()->getUrl('michelangelo-front-track'),
            'onlyMiniapp' => !empty($config['track_only_miniapp']),
        ]);
        $app->addJs('%michelangelo%/miniapp.js');
        return $params;
    }

    /**
     * Маршрут приёмника событий с фронта.
     */
    public static function getRoute(array $routes)
    {
        $routes[] = new \RS\Router\Route('michelangelo-front-track', [
            'title' => 'Michelangelo: приём событий витрины',
        ], '/michelangelo/track/');
        return $routes;
    }

    /**
     * Досылает очередь вебхуков.
     */
    public static function cron()
    {
        $api = new Api();
        return $api->flushOutbox();
    }

    /**
     * Событие может прийти как объект заказа, так и обёрткой с параметрами.
     */
    protected static function extractOrder($params)
    {
        if ($params instanceof \Shop\Model\Orm\Order) {
            return $params;
        }
        if (is_array($params)) {
            foreach (['order', 'object', 'orm'] as $key) {
                if (isset($params[$key]) && $params[$key] instanceof \Shop\Model\Orm\Order) {
                    return $params[$key];
                }
            }
        }
        return null;
    }
}
