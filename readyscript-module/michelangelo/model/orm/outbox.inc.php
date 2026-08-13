<?php
namespace Michelangelo\Model\Orm;

use RS\Orm\Type;

/**
 * Очередь исходящих вебхуков.
 *
 * Заказ нельзя терять из-за того, что админ-панель на секунду недоступна,
 * поэтому запись сначала попадает сюда, а потом отправляется — сразу же либо
 * следующим прогоном cron. Таблица создаётся автоматически при установке
 * модуля: ReadyScript синхронизирует схему из описания ORM-объекта.
 */
class Outbox extends \RS\Orm\OrmObject
{
    protected static $table = 'michelangelo_outbox';

    const STATUS_PENDING = 'pending';
    const STATUS_SENT = 'sent';
    const STATUS_FAILED = 'failed';

    function _init()
    {
        parent::_init()->append([
            'id' => new Type\Integer([
                'visible' => false,
            ]),
            'endpoint' => new Type\Varchar([
                'maxLength' => 64,
                'description' => 'Канал доставки',
                'hint' => 'orders | events | identity',
            ]),
            'payload' => new Type\Text([
                'description' => 'Тело запроса (JSON)',
            ]),
            'status' => new Type\Enum([
                self::STATUS_PENDING,
                self::STATUS_SENT,
                self::STATUS_FAILED,
            ], [
                'description' => 'Статус',
                'default' => self::STATUS_PENDING,
            ]),
            'attempts' => new Type\Integer([
                'description' => 'Попыток доставки',
                'default' => 0,
            ]),
            'last_error' => new Type\Text([
                'description' => 'Последняя ошибка',
            ]),
            'dateof' => new Type\Datetime([
                'description' => 'Создано',
            ]),
            'next_try' => new Type\Datetime([
                'description' => 'Следующая попытка',
            ]),
        ]);
    }

    function beforeWrite($flag)
    {
        if ($flag == self::INSERT) {
            $this->dateof = date('Y-m-d H:i:s');
            if (empty($this['next_try'])) {
                $this->next_try = date('Y-m-d H:i:s');
            }
        }
        return parent::beforeWrite($flag);
    }
}
