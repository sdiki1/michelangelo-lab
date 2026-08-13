<?php
namespace Michelangelo\Config;

/**
 * Синхронизирует не только ORM модуля, но и расширенный нами shop_order.
 */
class Install extends \RS\Module\AbstractInstall
{
    function update()
    {
        if (!parent::update()) {
            return false;
        }

        $order = new \Shop\Model\Orm\Order();
        if (!$order->dbUpdate()) {
            return false;
        }

        // Переносим данные из версии 1.x. Повторный запуск безопасен:
        // обработчик заполняет только пустое новое поле.
        $orders = \RS\Orm\Request::make()
            ->from($order)
            ->where("ml_platform_user_id IS NOT NULL AND ml_platform_user_id != ''")
            ->objects();
        foreach ($orders as $legacy_order) {
            $legacy_order->update();
        }

        return true;
    }
}
