<?php
namespace Michelangelo\Model;

/**
 * Минимальный файловый журнал цепочки miniapp -> session -> order.
 * Секреты и полный initData сюда передавать нельзя.
 */
class Diagnostic
{
    public static function enabled()
    {
        try {
            $config = \RS\Config\Loader::byModule('michelangelo');
            return !empty($config['diagnostic_log']);
        } catch (\Exception $e) {
            return false;
        }
    }

    public static function write($event, array $context = [])
    {
        if (!self::enabled()) {
            return;
        }

        $safe = [];
        foreach ($context as $key => $value) {
            if (in_array($key, ['token', 'bot_token', 'init_data'], true)) {
                continue;
            }
            if (is_bool($value)) {
                $safe[$key] = $value ? 1 : 0;
            } elseif (is_scalar($value) || $value === null) {
                $safe[$key] = $value;
            }
        }

        $line = date('c')
            . ' event=' . preg_replace('/[^a-z0-9_.-]/i', '_', (string)$event)
            . ' ' . json_encode($safe, JSON_UNESCAPED_UNICODE)
            . PHP_EOL;
        $root = dirname(dirname(dirname(__DIR__)));
        $file = $root . '/storage/logs/michelangelo.log';
        if (@file_put_contents($file, $line, FILE_APPEND | LOCK_EX) === false) {
            error_log('[michelangelo] ' . trim($line));
        }
    }

    public static function sessionFingerprint()
    {
        $id = session_id();
        return $id === '' ? 'none' : substr(hash('sha256', $id), 0, 12);
    }

    public static function maskedUserId($value)
    {
        $value = trim((string)$value);
        if ($value === '') {
            return 'none';
        }
        return '***' . substr($value, -4) . ':' . substr(hash('sha256', $value), 0, 8);
    }
}
