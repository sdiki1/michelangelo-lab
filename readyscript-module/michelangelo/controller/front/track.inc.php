<?php
namespace Michelangelo\Controller\Front;

use Michelangelo\Model\Identity;

/**
 * Принимает подписанный initData и сохраняет проверенный ID в PHP-сессии.
 */
class Track extends \RS\Controller\Front
{
    function actionIndex()
    {
        $this->setSiteResponseType();
        $config = \RS\Config\Loader::byModule('michelangelo');
        if (empty($config['enabled'])) {
            return $this->jsonResponse(['ok' => false, 'error' => 'disabled'], 503);
        }

        $raw = file_get_contents('php://input');
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            return $this->jsonResponse(['ok' => false, 'error' => 'malformed_json'], 400);
        }

        $platform = Identity::normalizePlatform(isset($data['platform']) ? $data['platform'] : null);
        $token_key = $platform === Identity::PLATFORM_TELEGRAM
            ? 'telegram_bot_token'
            : 'max_bot_token';
        $identity = Identity::verify(
            $platform,
            isset($data['init_data']) ? $data['init_data'] : '',
            $platform ? $config[$token_key] : '',
            isset($config['init_data_max_age']) ? $config['init_data_max_age'] : 86400
        );

        if (!$identity || !Identity::store($identity)) {
            return $this->jsonResponse(['ok' => false, 'error' => 'invalid_init_data'], 422);
        }

        return $this->jsonResponse([
            'ok' => true,
            'platform' => $identity['platform'],
            'platform_user_id' => $identity['platform_user_id'],
        ]);
    }

    protected function jsonResponse(array $data, $code = 200)
    {
        http_response_code($code);
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode($data, JSON_UNESCAPED_UNICODE);
        exit;
    }
}
