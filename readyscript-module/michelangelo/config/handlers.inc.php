<?php
namespace Michelangelo\Config;

use Michelangelo\Model\Identity;
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
     * Записывает проверенный ID в новый заказ. Ручные значения администратора
     * и уже существующую привязку обработчик не перезаписывает.
     */
    public static function ormBeforeWriteShopOrder($params)
    {
        $order = self::extractOrder($params);
        if (!$order) {
            return $params;
        }

        self::migrateLegacyIdentity($order);

        $identity = Identity::current();
        if (!$identity || empty($identity['verified'])) {
            return $params;
        }

        $field = Identity::orderField($identity['platform']);
        if ($field && empty($order[$field])) {
            $order[$field] = $identity['platform_user_id'];
            $order['ml_bind_source'] = 'miniapp_verified';
        }

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
        if (empty($config['enabled']) || \RS\Router\Manager::obj()->isAdminZone()) {
            return $params;
        }

        $app = \RS\Application\Application::getInstance();
        $app->addJsVar('michelangeloIdentity', [
            'url' => \RS\Router\Manager::obj()->getUrl('michelangelo-front-track'),
        ]);
        $app->addJs('%michelangelo%/miniapp.js');
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
