<?php
namespace Michelangelo\Model\ExternalApi\Michelangelo;

use ExternalApi\Model\AbstractMethods\AbstractAuthorizedMethod;
use ExternalApi\Model\Exception as ApiException;
use Shop\Model\DeliveryType\InterfaceDeliveryOrder;
use Shop\Model\Orm\Order;
use Shop\Model\Orm\UserStatus;
use Shop\Model\UserStatusApi;

/**
 * Отменяет принадлежащий клиенту заказ и, если он уже передан в службу
 * доставки, сначала удаляет заявку через штатный объект доставки ReadyScript.
 */
class CancelOrder extends AbstractAuthorizedMethod
{
    const RIGHT_CANCEL = 1;

    public function getRightTitles()
    {
        return [
            self::RIGHT_CANCEL => t('Отмена клиентского заказа и заявки на доставку'),
        ];
    }

    /**
     * @param string $token OAuth-токен сервисного пользователя
     * @param integer $order_id ID заказа ReadyScript
     * @param string $platform telegram или max
     * @param string $platform_user_id ID клиента в мессенджере
     * @return array
     */
    public function process($token, $order_id, $platform, $platform_user_id)
    {
        $order = new Order((int)$order_id);
        if (!$order['id']) {
            throw new ApiException(t('Заказ не найден'), ApiException::ERROR_OBJECT_NOT_FOUND);
        }

        $platform = strtolower(trim((string)$platform));
        $identity_field = $platform === 'telegram'
            ? 'telegram_user_id'
            : ($platform === 'max' ? 'max_user_id' : null);
        if (!$identity_field || trim((string)$platform_user_id) === '') {
            throw new ApiException(t('Некорректный мессенджер или ID клиента'), ApiException::ERROR_WRONG_PARAM_VALUE);
        }
        if (!hash_equals(trim((string)$order[$identity_field]), trim((string)$platform_user_id))) {
            throw new ApiException(t('Заказ не принадлежит данному клиенту'), ApiException::ERROR_METHOD_ACCESS_DENIED);
        }

        $status = $order->getStatus();
        $status_type = (string)$status['type'];
        $status_copy_type = (string)$status['copy_type'];
        if ($status_type === UserStatus::STATUS_CANCELLED || $status_copy_type === UserStatus::STATUS_CANCELLED) {
            return ['response' => [
                'success' => true,
                'already_cancelled' => true,
                'delivery_orders_deleted' => 0,
            ]];
        }
        if ($status_type === UserStatus::STATUS_SUCCESS || $status_copy_type === UserStatus::STATUS_SUCCESS) {
            throw new ApiException(t('Выполненный заказ уже нельзя отменить'), ApiException::ERROR_WRONG_PARAM_VALUE);
        }

        $deleted_count = $this->deleteDeliveryOrders($order);
        $cancelled_statuses = UserStatusApi::getStatusesIdByType(UserStatus::STATUS_CANCELLED);
        if (!$cancelled_statuses) {
            throw new ApiException(t('В ReadyScript не настроен статус отменённого заказа'), ApiException::ERROR_WRITE_ERROR);
        }

        $order['status'] = reset($cancelled_statuses);
        $order['cancellation_by_user'] = 1;
        $order['cancellation_by_user_date'] = date('Y-m-d H:i:s');
        if (!$order->update()) {
            $errors = implode('; ', (array)$order->getErrors());
            throw new ApiException(
                t('Не удалось изменить статус заказа%0', [$errors ? ': '.$errors : '']),
                ApiException::ERROR_WRITE_ERROR
            );
        }

        return ['response' => [
            'success' => true,
            'already_cancelled' => false,
            'delivery_orders_deleted' => $deleted_count,
            'status_id' => $order['status'],
        ]];
    }

    private function deleteDeliveryOrders(Order $order)
    {
        $delivery = $order->getDelivery();
        if (!$delivery['id']) {
            return 0;
        }

        $delivery_type = $delivery->getTypeObject();
        if (!($delivery_type instanceof InterfaceDeliveryOrder)) {
            // Устаревший класс CDEK использует снятое с поддержки XML API и не
            // даёт безопасного программного контракта удаления.
            if ($delivery_type->getShortName() === 'cdek'
                && isset($order->getExtraInfo()['cdek_order_id'])) {
                throw new ApiException(
                    t('Для автоматической отмены требуется актуальный тип доставки СДЭК'),
                    ApiException::ERROR_WRITE_ERROR
                );
            }
            return 0;
        }
        if (!$delivery_type->canDeleteDeliveryOrder()) {
            throw new ApiException(t('Служба доставки не поддерживает отмену заявки'), ApiException::ERROR_WRITE_ERROR);
        }

        $deleted_count = 0;
        foreach ($delivery_type->getDeliveryOrderList($order) as $delivery_order) {
            // Для CDEK 2.0 этот вызов выполняет DELETE заказа в API СДЭК и
            // только после успешного ответа удаляет локальную заявку.
            $delivery_type->deleteDeliveryOrder($delivery_order);
            $deleted_count++;
        }
        return $deleted_count;
    }
}
