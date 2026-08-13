<?php
namespace Michelangelo\Model;

use Michelangelo\Model\Orm\Outbox;

/**
 * Клиент админ-панели Michelangelo.
 *
 * Подпись запроса (та же схема, что и на стороне Python):
 *
 *     base      = "{timestamp}.{raw_body}"
 *     signature = hex(hmac_sha256(secret, base))
 *
 * Заголовки: X-ML-Timestamp, X-ML-Signature: sha256=<hex>
 *
 * Подписывается ровно то тело, которое уходит в сокет. Любая пересборка JSON
 * после подписи ломает проверку на приёмнике.
 */
class Api
{
    const ENDPOINT_ORDERS = 'orders';
    const ENDPOINT_EVENTS = 'events';
    const ENDPOINT_IDENTITY = 'identity';

    /** @var \Michelangelo\Config\File */
    protected $config;

    function __construct()
    {
        $this->config = \RS\Config\Loader::byModule($this);
    }

    function isEnabled()
    {
        return !empty($this->config['enabled'])
            && !empty($this->config['admin_url'])
            && !empty($this->config['secret']);
    }

    /**
     * Кладёт запрос в очередь и пытается отправить немедленно.
     *
     * Возвращает true, если доставлено сразу. Иначе запись остаётся в очереди
     * и будет добита cron-ом.
     */
    function enqueue($endpoint, array $payload, $send_now = true)
    {
        if (!$this->isEnabled()) {
            return false;
        }

        $item = new Outbox();
        $item['endpoint'] = $endpoint;
        $item['payload'] = json_encode($payload, JSON_UNESCAPED_UNICODE);
        $item['status'] = Outbox::STATUS_PENDING;
        $item->insert();

        if (!$send_now) {
            return false;
        }
        return $this->deliver($item);
    }

    /**
     * Отправляет одну запись очереди. Обновляет её статус.
     */
    function deliver(Outbox $item)
    {
        $result = $this->send($item['endpoint'], $item['payload']);
        $item['attempts'] = (int)$item['attempts'] + 1;

        if ($result['ok']) {
            $item['status'] = Outbox::STATUS_SENT;
            $item['last_error'] = '';
            $item->update();
            return true;
        }

        $max_attempts = (int)$this->config['outbox_max_attempts'] ?: 10;
        $item['last_error'] = mb_substr((string)$result['error'], 0, 1000);

        if ($item['attempts'] >= $max_attempts) {
            $item['status'] = Outbox::STATUS_FAILED;
        } else {
            $item['status'] = Outbox::STATUS_PENDING;
            // Экспоненциальная выдержка: 1, 2, 4, 8 ... минут, но не дольше часа.
            $delay = min(3600, 60 * pow(2, (int)$item['attempts'] - 1));
            $item['next_try'] = date('Y-m-d H:i:s', time() + $delay);
        }
        $item->update();
        return false;
    }

    /**
     * Досылает накопившуюся очередь. Вызывается по cron.
     */
    function flushOutbox()
    {
        if (!$this->isEnabled()) {
            return ['sent' => 0, 'failed' => 0];
        }

        $batch_size = (int)$this->config['outbox_batch_size'] ?: 100;
        $api = new \RS\Orm\Request();
        $items = $api->from(new Outbox())
            ->where(['status' => Outbox::STATUS_PENDING])
            ->where('next_try <= NOW()')
            ->orderby('id ASC')
            ->limit($batch_size)
            ->objects();

        $sent = 0;
        $failed = 0;
        foreach ($items as $item) {
            if ($this->deliver($item)) {
                $sent++;
            } else {
                $failed++;
            }
        }
        return ['sent' => $sent, 'failed' => $failed];
    }

    /**
     * Синхронный подписанный POST. $body — уже готовая JSON-строка.
     *
     * @return array{ok: bool, code: int, error: string, body: string}
     */
    function send($endpoint, $body)
    {
        $url = rtrim($this->config['admin_url'], '/') . '/api/rs/' . $endpoint;
        $timestamp = time();
        $signature = self::sign($this->config['secret'], $timestamp, $body);

        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => (int)$this->config['request_timeout'] ?: 5,
            CURLOPT_CONNECTTIMEOUT => 3,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'X-ML-Timestamp: ' . $timestamp,
                'X-ML-Signature: ' . $signature,
                'Content-Length: ' . strlen($body),
            ],
        ]);

        $response = curl_exec($ch);
        $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $curl_error = curl_error($ch);
        curl_close($ch);

        if ($response === false) {
            return ['ok' => false, 'code' => 0, 'error' => $curl_error, 'body' => ''];
        }
        if ($code < 200 || $code >= 300) {
            return [
                'ok' => false,
                'code' => $code,
                'error' => 'HTTP ' . $code . ': ' . mb_substr((string)$response, 0, 500),
                'body' => (string)$response,
            ];
        }
        return ['ok' => true, 'code' => $code, 'error' => '', 'body' => (string)$response];
    }

    /**
     * Подпись запроса. Вынесена отдельно, чтобы её можно было проверить тестом.
     */
    public static function sign($secret, $timestamp, $body)
    {
        return 'sha256=' . hash_hmac('sha256', $timestamp . '.' . $body, $secret);
    }
}
