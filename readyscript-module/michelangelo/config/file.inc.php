<?php
namespace Michelangelo\Config;

use RS\Orm\Type;

/**
 * Настройки модуля «Связка с админ-панелью Michelangelo».
 *
 * Доступны в админке ReadyScript: Модули → Michelangelo → Настройки.
 */
class File extends \RS\Orm\ConfigObject
{
    function _init()
    {
        $this->getPropertyIterator()->append([
            'enabled' => new Type\Integer([
                'description' => 'Включить отправку данных в админ-панель',
                'checkboxview' => [1, 0],
                'default' => 1,
            ]),
            'admin_url' => new Type\Varchar([
                'maxLength' => 255,
                'description' => 'Адрес админ-панели',
                'hint' => 'Например: https://admin.michelangelo-lab.ru',
                'default' => 'https://admin.michelangelo-lab.ru',
            ]),
            'secret' => new Type\Varchar([
                'maxLength' => 255,
                'description' => 'Общий секрет подписи (RS_MODULE_SECRET)',
                'hint' => 'Должен совпадать со значением RS_MODULE_SECRET в .env админ-панели',
                'default' => '',
            ]),
            'track_site_events' => new Type\Integer([
                'description' => 'Собирать действия пользователей на витрине',
                'checkboxview' => [1, 0],
                'default' => 1,
            ]),
            'track_only_miniapp' => new Type\Integer([
                'description' => 'Собирать действия только в миниаппе',
                'hint' => 'Если выключено, события шлются и для обычных посетителей сайта',
                'checkboxview' => [1, 0],
                'default' => 1,
            ]),
            'request_timeout' => new Type\Integer([
                'description' => 'Таймаут запроса, сек.',
                'default' => 5,
            ]),
            'outbox_batch_size' => new Type\Integer([
                'description' => 'Сколько записей очереди отправлять за один прогон cron',
                'default' => 100,
            ]),
            'outbox_max_attempts' => new Type\Integer([
                'description' => 'Максимум попыток доставки одной записи',
                'default' => 10,
            ]),
        ]);
    }

    public static function getDefaultValues()
    {
        return [
            'enabled' => 1,
            'admin_url' => 'https://admin.michelangelo-lab.ru',
            'secret' => '',
            'track_site_events' => 1,
            'track_only_miniapp' => 1,
            'request_timeout' => 5,
            'outbox_batch_size' => 100,
            'outbox_max_attempts' => 10,
        ];
    }
}
