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
            'require_promo_code' => new Type\Integer([
                'description' => 'Требовать промокод для оформления заказа',
                'hint' => 'Заказ на витрине и через checkout API нельзя создать, пока к корзине не применён действующий скидочный купон.',
                'checkboxview' => [1, 0],
                'default' => 1,
            ]),
            'promo_required_message' => new Type\Varchar([
                'maxLength' => 255,
                'description' => 'Сообщение, если промокод не введён',
                'default' => 'Для оформления заказа необходимо ввести действующий промокод.',
            ]),
            'promo_prompt_text' => new Type\Varchar([
                'maxLength' => 255,
                'description' => 'Текст подсказки о промокоде на сайте',
                'hint' => 'Показывается рядом с полем промокода и кнопкой оформления заказа.',
                'default' => 'Введите промокод — без него оформить заказ нельзя.',
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
            'require_promo_code' => 1,
            'promo_required_message' => 'Для оформления заказа необходимо ввести действующий промокод.',
            'promo_prompt_text' => 'Введите промокод — без него оформить заказ нельзя.',
        ];
    }
}
