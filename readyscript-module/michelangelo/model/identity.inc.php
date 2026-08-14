<?php
namespace Michelangelo\Model;

/**
 * Проверенная личность посетителя мини-приложения.
 */
class Identity
{
    const SESSION_KEY = 'michelangelo_identity';
    const PLATFORM_TELEGRAM = 'telegram';
    const PLATFORM_MAX = 'max';

    /** @var string|null Безопасный код последней ошибки проверки. */
    protected static $last_verify_error;

    public static function verify($platform, $init_data, $bot_token, $max_age = 86400)
    {
        self::$last_verify_error = null;
        $platform = self::normalizePlatform($platform);
        $init_data = (string)$init_data;
        $bot_token = trim((string)$bot_token);
        if (!$platform || $init_data === '' || $bot_token === '') {
            self::$last_verify_error = 'missing_input';
            return null;
        }

        $parsed = self::parseInitData($init_data);
        if (!$parsed || empty($parsed['hash'])) {
            self::$last_verify_error = 'malformed_or_missing_hash';
            return null;
        }

        $received_hash = strtolower((string)$parsed['hash']);
        unset($parsed['hash']);

        // В современном Telegram initData может присутствовать Ed25519-поле
        // signature. При классической проверке с bot token из строки данных
        // исключается только hash; signature остаётся и участвует в HMAC.

        ksort($parsed, SORT_STRING);
        $pairs = [];
        foreach ($parsed as $key => $value) {
            if (is_array($value)) {
                self::$last_verify_error = 'non_scalar_parameter';
                return null;
            }
            $pairs[] = $key . '=' . $value;
        }
        $check_string = implode("\n", $pairs);

        // У Telegram и MAX на данный момент одна схема WebAppData:
        // secret = HMAC_SHA256(key="WebAppData", data=bot_token).
        $secret = hash_hmac('sha256', $bot_token, 'WebAppData', true);
        $calculated_hash = hash_hmac('sha256', $check_string, $secret);
        if (!hash_equals($calculated_hash, $received_hash)) {
            self::$last_verify_error = 'hash_mismatch';
            return null;
        }

        $auth_date = isset($parsed['auth_date']) ? (int)$parsed['auth_date'] : 0;
        if (!$auth_date || $auth_date > time() + 300) {
            self::$last_verify_error = 'invalid_auth_date';
            return null;
        }
        if ((int)$max_age > 0 && time() - $auth_date > (int)$max_age) {
            self::$last_verify_error = 'expired';
            return null;
        }

        $user = isset($parsed['user']) ? json_decode($parsed['user'], true) : null;
        if (!is_array($user) || !isset($user['id'])) {
            self::$last_verify_error = 'missing_user';
            return null;
        }
        $user_id = trim((string)$user['id']);
        if ($user_id === '') {
            self::$last_verify_error = 'empty_user_id';
            return null;
        }

        return [
            'platform' => $platform,
            'platform_user_id' => $user_id,
            'username' => self::stringValue($user, 'username'),
            'first_name' => self::stringValue($user, 'first_name'),
            'last_name' => self::stringValue($user, 'last_name'),
            'verified' => true,
        ];
    }

    public static function lastVerifyError()
    {
        return self::$last_verify_error;
    }

    public static function store(array $identity)
    {
        $platform = self::normalizePlatform(isset($identity['platform']) ? $identity['platform'] : null);
        $user_id = isset($identity['platform_user_id'])
            ? trim((string)$identity['platform_user_id'])
            : '';
        if (!$platform || $user_id === '' || empty($identity['verified'])) {
            return false;
        }

        $_SESSION[self::SESSION_KEY] = [
            'platform' => $platform,
            'platform_user_id' => $user_id,
            'username' => self::stringValue($identity, 'username'),
            'first_name' => self::stringValue($identity, 'first_name'),
            'last_name' => self::stringValue($identity, 'last_name'),
            'verified' => true,
        ];
        return true;
    }

    public static function current()
    {
        return isset($_SESSION[self::SESSION_KEY]) ? $_SESSION[self::SESSION_KEY] : null;
    }

    public static function normalizePlatform($platform)
    {
        $platform = strtolower(trim((string)$platform));
        return in_array($platform, [self::PLATFORM_TELEGRAM, self::PLATFORM_MAX], true)
            ? $platform
            : null;
    }

    public static function orderField($platform)
    {
        $platform = self::normalizePlatform($platform);
        if ($platform === self::PLATFORM_TELEGRAM) {
            return 'telegram_user_id';
        }
        if ($platform === self::PLATFORM_MAX) {
            return 'max_user_id';
        }
        return null;
    }

    protected static function parseInitData($init_data)
    {
        $result = [];
        foreach (explode('&', (string)$init_data) as $part) {
            $pair = explode('=', $part, 2);
            $key = rawurldecode($pair[0]);
            if ($key === '' || array_key_exists($key, $result)) {
                return null;
            }
            $result[$key] = isset($pair[1]) ? rawurldecode($pair[1]) : '';
        }
        return $result;
    }

    protected static function stringValue(array $source, $key)
    {
        if (!isset($source[$key])) {
            return null;
        }
        $value = trim((string)$source[$key]);
        return $value === '' ? null : $value;
    }
}
