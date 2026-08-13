<?php
namespace Michelangelo\Config;

/**
 * Установка модуля.
 *
 * Таблицу michelangelo_outbox создавать вручную не нужно: ReadyScript
 * синхронизирует схему из описания ORM-объектов (см. model/orm/outbox.inc.php).
 * Поля заказа ml_* добавляются событием ormInitShopOrder и появляются в
 * таблице shop_order при первом же обновлении структуры.
 */
class Install extends \RS\Module\AbstractInstall
{
    function install()
    {
        // Секрет подписи генерируем сразу, чтобы модуль нельзя было
        // случайно оставить с пустым (то есть выключенным) секретом.
        $config = \RS\Config\Loader::byModule($this);
        if (empty($config['secret'])) {
            $config['secret'] = bin2hex(random_bytes(32));
            $config->update();
        }

        $this->installAdminMenu();
        return true;
    }
}
