<?php
namespace Michelangelo\Config;

use RS\Orm\Type;

class File extends \RS\Orm\ConfigObject
{
    function _init()
    {
        parent::_init()->append([
            'enabled' => new Type\Integer([
                'description' => 'Записывать ID мессенджера в заказы',
                'checkboxview' => [1, 0],
                'default' => 1,
            ]),
            'telegram_bot_token' => new Type\Varchar([
                'maxLength' => 255,
                'description' => 'Токен Telegram-бота',
                'hint' => 'Нужен только для серверной проверки Telegram initData',
                'default' => '',
            ]),
            'max_bot_token' => new Type\Varchar([
                'maxLength' => 255,
                'description' => 'Токен MAX-бота',
                'hint' => 'Нужен только для серверной проверки MAX initData',
                'default' => '',
            ]),
            'init_data_max_age' => new Type\Integer([
                'description' => 'Максимальный возраст initData, секунд',
                'default' => 86400,
            ]),
            'diagnostic_log' => new Type\Integer([
                'description' => 'Включить диагностический журнал привязки',
                'hint' => 'Файл: /storage/logs/michelangelo.log. Выключите после диагностики.',
                'checkboxview' => [1, 0],
                'default' => 0,
            ]),
        ]);
    }

    public static function getDefaultValues()
    {
        return parent::getDefaultValues() + [
            'enabled' => 1,
            'telegram_bot_token' => '',
            'max_bot_token' => '',
            'init_data_max_age' => 86400,
            'diagnostic_log' => 0,
        ];
    }
}
