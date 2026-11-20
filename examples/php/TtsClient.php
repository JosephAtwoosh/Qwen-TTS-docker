<?php
/**
 * One-turn client for Qwen-TTS-docker.
 *
 * No dependencies beyond ext-curl. The studio stores the returned WAV with
 * store_media() and attaches it with shot_audio_set(); this class only talks
 * HTTP.
 *
 *   $tts = new TtsClient('http://127.0.0.1:18000', $key, 120);
 *   if (!$tts->isReady()) { ... }
 *   $r = $tts->synthesize('I say, that is a very large cabbage.', 'henry', 1942, 5.7);
 *   file_put_contents($tmp, $r['wav']);
 */
declare(strict_types=1);

final class TtsException extends RuntimeException
{
    private int $httpStatus;

    public function __construct(string $message, int $httpStatus = 0)
    {
        parent::__construct($message);
        $this->httpStatus = $httpStatus;
    }

    public function httpStatus(): int
    {
        return $this->httpStatus;
    }
}

final class TtsClient
{
    private string $baseUrl;
    private string $apiKey;
    private int $timeout;
    private int $connectTimeout;

    public function __construct(
        string $baseUrl,
        string $apiKey = '',
        int $timeout = 120,
        int $connectTimeout = 10
    ) {
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->apiKey = $apiKey;
        $this->timeout = max(1, $timeout);
        $this->connectTimeout = max(1, $connectTimeout);
    }

    /** GET /health; returns the decoded body. */
    public function health(): array
    {
        [$status, $raw] = $this->request('GET', '/health');
        if ($status !== 200) {
            throw new TtsException('health failed: HTTP ' . $status, $status);
        }
        return $this->decode($raw);
    }

    /** True when the model is loaded and the API can synthesize. */
    public function isReady(): bool
    {
        try {
            $health = $this->health();
        } catch (TtsException) {
            return false;
        }
        return ($health['status'] ?? '') === 'ok';
    }

    /** GET /v1/voices; returns the list. */
    public function voices(): array
    {
        [$status, $raw] = $this->request('GET', '/v1/voices');
        if ($status !== 200) {
            throw new TtsException('voices failed: HTTP ' . $status, $status);
        }
        $body = $this->decode($raw);
        return is_array($body['voices'] ?? null) ? $body['voices'] : [];
    }

    /**
     * POST /v1/tts for exactly one line.
     *
     * @return array{wav:string,seconds:float,voice:string,sample_rate:int,headers:array<string,string>}
     */
    public function synthesize(
        string $text,
        string $voice,
        int $seed = 1942,
        ?float $targetDuration = null
    ): array {
        $text = trim($text);
        if ($text === '') {
            throw new TtsException('empty text');
        }
        $body = [
            'voice' => $voice,
            'text' => $text,
            'seed' => $seed,
        ];
        if ($targetDuration !== null && $targetDuration > 0) {
            // Accepted once /v1/tts supports duration fitting; harmless before.
            $body['target_duration_s'] = $targetDuration;
        }

        [$status, $raw, $headers] = $this->request('POST', '/v1/tts', $body);
        if ($status !== 200) {
            $detail = $this->errorDetail($raw);
            throw new TtsException('tts failed: HTTP ' . $status . ($detail !== '' ? ' — ' . $detail : ''), $status);
        }
        if (substr($raw, 0, 4) !== 'RIFF') {
            throw new TtsException('tts returned a non-WAV body (' . strlen($raw) . ' bytes)');
        }

        return [
            'wav' => $raw,
            'seconds' => (float) ($headers['x-duration-seconds'] ?? 0.0),
            'voice' => $headers['x-voice'] ?? $voice,
            'sample_rate' => (int) ($headers['x-sample-rate'] ?? 0),
            'headers' => $headers,
        ];
    }

    /**
     * @return array{0:int,1:string,2:array<string,string>}
     */
    private function request(string $method, string $path, ?array $body = null): array
    {
        $ch = curl_init($this->baseUrl . $path);
        if ($ch === false) {
            throw new TtsException('could not initialise curl');
        }

        $requestHeaders = ['Accept: application/json'];
        if ($this->apiKey !== '') {
            $requestHeaders[] = 'Authorization: Bearer ' . $this->apiKey;
        }

        $options = [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => $this->connectTimeout,
            CURLOPT_TIMEOUT => $this->timeout,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_HTTPHEADER => $requestHeaders,
            CURLOPT_HEADERFUNCTION => static function ($handle, string $line) use (&$responseHeaders): int {
                $length = strlen($line);
                $parts = explode(':', $line, 2);
                if (count($parts) === 2) {
                    $responseHeaders[strtolower(trim($parts[0]))] = trim($parts[1]);
                }
                return $length;
            },
        ];

        if ($method === 'POST') {
            $json = json_encode($body ?? [], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
            if ($json === false) {
                throw new TtsException('could not encode request JSON');
            }
            $requestHeaders[] = 'Content-Type: application/json';
            $options[CURLOPT_HTTPHEADER] = $requestHeaders;
            $options[CURLOPT_POST] = true;
            $options[CURLOPT_POSTFIELDS] = $json;
        }

        $responseHeaders = [];
        curl_setopt_array($ch, $options);
        $raw = curl_exec($ch);
        $errno = curl_errno($ch);
        $error = curl_error($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        // No curl_close(): it has had no effect since PHP 8.0 and is deprecated
        // in 8.5; the handle is released when it goes out of scope.

        if ($raw === false || $errno !== 0) {
            throw new TtsException('transport error: ' . ($error !== '' ? $error : 'curl ' . $errno));
        }
        return [$status, (string) $raw, $responseHeaders];
    }

    private function decode(string $raw): array
    {
        $decoded = json_decode($raw, true);
        if (!is_array($decoded)) {
            throw new TtsException('server returned invalid JSON');
        }
        return $decoded;
    }

    private function errorDetail(string $raw): string
    {
        $decoded = json_decode($raw, true);
        if (is_array($decoded)) {
            $detail = $decoded['detail'] ?? $decoded['error'] ?? '';
            if (is_string($detail)) {
                return $detail;
            }
        }
        return '';
    }
}
