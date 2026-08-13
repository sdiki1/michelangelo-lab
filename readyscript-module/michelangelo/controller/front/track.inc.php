<?php
namespace Michelangelo\Controller\Front;

use Michelangelo\Model\Api;
use Michelangelo\Model\Identity;

/**
 * Приёмник данных от JS миниаппа: POST /michelangelo/track/
 *
 * Два действия:
 *   action=identity — витрину открыли из миниаппа, запоминаем кто;
 *   action=events   — пачка действий пользователя.
 *
 * Эндпоинт публичный (его дёргает браузер покупателя), поэтому он ничего не
 * решает на доверии: initData уезжает в админ-панель, и подпись проверяется там.
 */
class Track extends \RS\Controller\Front
{
    function actionIndex()
    {
        $this->setSiteResponseType();

        $config = \RS\Config\Loader::byModule('michelangelo');
        if (empty($config['enabled'])) {
            return $this->jsonResponse(['ok' => false, 'error' => 'disabled']);
        }

        $raw = file_get_contents('php://input');
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            return $this->jsonResponse(['ok' => false, 'error' => 'malformed json'], 400);
        }

        $action = isset($data['action']) ? (string)$data['action'] : '';
        switch ($action) {
            case 'identity':
                return $this->handleIdentity($data);
            case 'events':
                return $this->handleEvents($data, $config);
            default:
                return $this->jsonResponse(['ok' => false, 'error' => 'unknown action'], 400);
        }
    }

    /**
     * Запоминает личность и подтверждает её в админ-панели.
     */
    protected function handleIdentity(array $data)
    {
        $platform = Identity::normalizePlatform(isset($data['platform']) ? $data['platform'] : null);
        if (!$platform) {
            return $this->jsonResponse(['ok' => false, 'error' => 'unknown platform'], 400);
        }

        $init_data = isset($data['init_data']) ? (string)$data['init_data'] : null;
        $identity = [
            'platform' => $platform,
            'platform_user_id' => isset($data['platform_user_id']) ? $data['platform_user_id'] : null,
            'init_data' => $init_data,
            'username' => isset($data['username']) ? $data['username'] : null,
            'first_name' => isset($data['first_name']) ? $data['first_name'] : null,
            'last_name' => isset($data['last_name']) ? $data['last_name'] : null,
            'verified' => false,
        ];

        // Telegram присылает подписанный initData — разбираем его для показа,
        // но достоверным считаем только ответ админ-панели.
        if ($platform === Identity::PLATFORM_TELEGRAM && $init_data) {
            $parsed = Identity::parseTelegramInitData($init_data);
            foreach ($parsed as $key => $value) {
                if ($value !== null) {
                    $identity[$key] = $value;
                }
            }
        }

        if (!Identity::store($identity)) {
            return $this->jsonResponse(['ok' => false, 'error' => 'identity is incomplete'], 400);
        }

        // Просим админ-панель проверить подпись и завести пользователя.
        $api = new Api();
        if ($api->isEnabled()) {
            $payload = Identity::toPayload();
            $result = $api->send(Api::ENDPOINT_IDENTITY, json_encode(
                $payload,
                JSON_UNESCAPED_UNICODE
            ));
            if ($result['ok']) {
                $answer = json_decode($result['body'], true);
                if (!empty($answer['platform_user_id'])) {
                    $identity['platform_user_id'] = (string)$answer['platform_user_id'];
                    $identity['platform'] = $answer['platform'];
                    $identity['verified'] = (isset($answer['bind_source']) ? $answer['bind_source'] : '') === 'miniapp';
                    Identity::store($identity);
                }
            }
        }

        $current = Identity::current();
        return $this->jsonResponse([
            'ok' => true,
            'platform' => $current['platform'],
            'platform_user_id' => $current['platform_user_id'],
            'verified' => $current['verified'],
        ]);
    }

    /**
     * Пересылает действия пользователя в админ-панель через очередь.
     */
    protected function handleEvents(array $data, $config)
    {
        if (empty($config['track_site_events'])) {
            return $this->jsonResponse(['ok' => true, 'skipped' => 'tracking is off']);
        }

        $identity = Identity::current();
        if (!$identity && !empty($config['track_only_miniapp'])) {
            return $this->jsonResponse(['ok' => true, 'skipped' => 'not a miniapp session']);
        }

        $events = [];
        $incoming = isset($data['events']) && is_array($data['events']) ? $data['events'] : [];
        foreach (array_slice($incoming, 0, 200) as $event) {
            if (!is_array($event) || empty($event['action'])) {
                continue;
            }
            $events[] = array_filter([
                // event_id делает повторную доставку безопасной.
                'event_id' => !empty($event['event_id'])
                    ? (string)$event['event_id']
                    : self::generateEventId(),
                'action' => (string)$event['action'],
                'path' => isset($event['path']) ? (string)$event['path'] : null,
                'title' => isset($event['title']) ? (string)$event['title'] : null,
                'product_id' => isset($event['product_id']) ? (string)$event['product_id'] : null,
                'product_title' => isset($event['product_title'])
                    ? (string)$event['product_title']
                    : null,
                'referrer' => isset($event['referrer']) ? (string)$event['referrer'] : null,
                'session_id' => session_id(),
                'occurred_at' => isset($event['occurred_at'])
                    ? (string)$event['occurred_at']
                    : date('c'),
            ], function ($value) {
                return $value !== null && $value !== '';
            });
        }

        if (!$events) {
            return $this->jsonResponse(['ok' => true, 'received' => 0]);
        }

        $api = new Api();
        $payload = array_filter([
            'identity' => Identity::toPayload(),
            'events' => $events,
        ]);
        // Отправляем через очередь: витрина не должна ждать сеть ради аналитики.
        $api->enqueue(Api::ENDPOINT_EVENTS, $payload, false);

        return $this->jsonResponse(['ok' => true, 'received' => count($events)]);
    }

    protected static function generateEventId()
    {
        return bin2hex(random_bytes(16));
    }

    protected function jsonResponse(array $data, $code = 200)
    {
        http_response_code($code);
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode($data, JSON_UNESCAPED_UNICODE);
        exit;
    }
}
