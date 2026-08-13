<?php
namespace Michelangelo\Model;

/**
 * Платформенная личность посетителя: кто открыл витрину из миниаппа.
 *
 * ВАЖНО про доверие. initData подписан токеном бота, а токен живёт только в
 * админ-панели — значит, здесь мы initData не проверяем, а лишь разбираем для
 * показа в интерфейсе. Достоверным считается только то, что вернула
 * админ-панель после проверки подписи (см. Api::ENDPOINT_IDENTITY и поле
 * verified). Поэтому заказ уезжает вместе с сырым init_data: приёмник
 * перепроверит подпись сам и решит, чему верить.
 */
class Identity
{
    const SESSION_KEY = 'michelangelo_identity';

    const PLATFORM_TELEGRAM = 'telegram';
    const PLATFORM_MAX = 'max';

    /**
     * Сохраняет личность в сессии.
     *
     * @param array $identity platform, platform_user_id, init_data, username,
     *                        first_name, last_name, verified
     */
    public static function store(array $identity)
    {
        $pick = function ($key) use ($identity) {
            return isset($identity[$key]) ? $identity[$key] : null;
        };

        $clean = [
            'platform' => self::normalizePlatform($pick('platform')),
            'platform_user_id' => self::scalarString($pick('platform_user_id')),
            'init_data' => $pick('init_data') !== null ? (string)$pick('init_data') : null,
            'username' => self::scalarString($pick('username')),
            'first_name' => self::scalarString($pick('first_name')),
            'last_name' => self::scalarString($pick('last_name')),
            'verified' => !empty($identity['verified']),
        ];

        if (!$clean['platform'] || (!$clean['platform_user_id'] && !$clean['init_data'])) {
            return false;
        }

        $_SESSION[self::SESSION_KEY] = $clean;
        return true;
    }

    /**
     * @return array|null личность текущей сессии
     */
    public static function current()
    {
        return isset($_SESSION[self::SESSION_KEY]) ? $_SESSION[self::SESSION_KEY] : null;
    }

    public static function forget()
    {
        unset($_SESSION[self::SESSION_KEY]);
    }

    /**
     * Личность в том виде, в каком её ждёт админ-панель.
     */
    public static function toPayload($identity = null)
    {
        $identity = $identity ?: self::current();
        if (!$identity) {
            return null;
        }
        $payload = [
            'platform' => $identity['platform'],
            'platform_user_id' => $identity['platform_user_id'],
            'username' => $identity['username'],
            'first_name' => $identity['first_name'],
            'last_name' => $identity['last_name'],
        ];
        // init_data — единственное, что приёмник умеет проверить криптографически.
        if (!empty($identity['init_data'])) {
            $payload['init_data'] = $identity['init_data'];
        }
        return array_filter($payload, function ($value) {
            return $value !== null && $value !== '';
        });
    }

    /**
     * Разбирает Telegram initData БЕЗ проверки подписи — только для показа.
     *
     * @return array{platform_user_id: ?string, username: ?string,
     *               first_name: ?string, last_name: ?string}
     */
    public static function parseTelegramInitData($init_data)
    {
        $result = [
            'platform_user_id' => null,
            'username' => null,
            'first_name' => null,
            'last_name' => null,
        ];
        if (!$init_data) {
            return $result;
        }

        parse_str((string)$init_data, $parsed);
        if (empty($parsed['user'])) {
            return $result;
        }

        $user = json_decode($parsed['user'], true);
        if (!is_array($user)) {
            return $result;
        }

        $result['platform_user_id'] = isset($user['id']) ? (string)$user['id'] : null;
        $result['username'] = isset($user['username']) ? (string)$user['username'] : null;
        $result['first_name'] = isset($user['first_name']) ? (string)$user['first_name'] : null;
        $result['last_name'] = isset($user['last_name']) ? (string)$user['last_name'] : null;
        return $result;
    }

    public static function normalizePlatform($platform)
    {
        $platform = strtolower(trim((string)$platform));
        return in_array($platform, [self::PLATFORM_TELEGRAM, self::PLATFORM_MAX], true)
            ? $platform
            : null;
    }

    protected static function scalarString($value)
    {
        if ($value === null) {
            return null;
        }
        $value = trim((string)$value);
        return $value === '' ? null : $value;
    }
}
