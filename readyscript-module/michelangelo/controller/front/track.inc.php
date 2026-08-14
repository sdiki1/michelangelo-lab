<?php
namespace Michelangelo\Controller\Front;

use Michelangelo\Model\Identity;
use Michelangelo\Model\Diagnostic;

/**
 * Принимает подписанный initData и сохраняет проверенный ID в PHP-сессии.
 */
class Track extends \RS\Controller\Front
{
    function actionIndex()
    {
        $config = \RS\Config\Loader::byModule('michelangelo');
        if (empty($config['enabled'])) {
            return $this->jsonResponse(['ok' => false, 'error' => 'disabled'], 503);
        }

        $raw = file_get_contents('php://input');
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            Diagnostic::write('track.malformed_json', [
                'session' => Diagnostic::sessionFingerprint(),
                'body_length' => strlen((string)$raw),
            ]);
            return $this->jsonResponse(['ok' => false, 'error' => 'malformed_json'], 400);
        }

        if (isset($data['action']) && $data['action'] === 'diagnostic') {
            Diagnostic::write('browser.probe', [
                'session' => Diagnostic::sessionFingerprint(),
                'path' => isset($data['path']) ? $data['path'] : null,
                'telegram_object' => !empty($data['telegram_object']),
                'telegram_init_data_length' => isset($data['telegram_init_data_length'])
                    ? (int)$data['telegram_init_data_length']
                    : 0,
                'telegram_platform' => isset($data['telegram_platform'])
                    ? substr((string)$data['telegram_platform'], 0, 32)
                    : null,
                'telegram_version' => isset($data['telegram_version'])
                    ? substr((string)$data['telegram_version'], 0, 32)
                    : null,
                'max_object' => !empty($data['max_object']),
                'hash_has_telegram' => !empty($data['hash_has_telegram']),
                'hash_has_max' => !empty($data['hash_has_max']),
                'referrer' => isset($data['referrer'])
                    ? substr((string)$data['referrer'], 0, 255)
                    : null,
                'user_agent' => isset($data['user_agent'])
                    ? substr((string)$data['user_agent'], 0, 255)
                    : null,
            ]);
            return $this->jsonResponse(['ok' => true, 'diagnostic' => true]);
        }

        $platform = Identity::normalizePlatform(isset($data['platform']) ? $data['platform'] : null);
        $token_key = $platform === Identity::PLATFORM_TELEGRAM
            ? 'telegram_bot_token'
            : 'max_bot_token';
        $init_data = isset($data['init_data']) ? (string)$data['init_data'] : '';
        Diagnostic::write('track.received', [
            'session' => Diagnostic::sessionFingerprint(),
            'host' => isset($_SERVER['HTTP_HOST']) ? $_SERVER['HTTP_HOST'] : null,
            'platform' => $platform,
            'init_data_length' => strlen($init_data),
            'token_configured' => $platform && !empty($config[$token_key]),
            'cookie_present' => isset($_COOKIE[session_name()]),
        ]);
        $identity = Identity::verify(
            $platform,
            $init_data,
            $platform ? $config[$token_key] : '',
            isset($config['init_data_max_age']) ? $config['init_data_max_age'] : 86400
        );

        if (!$identity || !Identity::store($identity)) {
            Diagnostic::write('track.rejected', [
                'session' => Diagnostic::sessionFingerprint(),
                'platform' => $platform,
                'init_data_length' => strlen($init_data),
                'token_configured' => $platform && !empty($config[$token_key]),
                'verify_error' => Identity::lastVerifyError(),
            ]);
            return $this->jsonResponse(['ok' => false, 'error' => 'invalid_init_data'], 422);
        }

        Diagnostic::write('track.accepted', [
            'session' => Diagnostic::sessionFingerprint(),
            'platform' => $identity['platform'],
            'user_id' => Diagnostic::maskedUserId($identity['platform_user_id']),
        ]);

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
