<?php
declare(strict_types=1);
/* Private implementation. Public wrappers define QID_ROUTE as a literal
 * 'form', 'submit', 'download', or 'download-windows', then require this file by a fixed path.
 * Never derive an include path or QID_ROUTE from visitor parameters.
 * TLS must be reflected by the server's HTTPS variable, not forwarded headers.
 */
ini_set('display_errors', '0');
ini_set('log_errors', '0');
const QID_COOKIE = 'qid_request_session';
const QID_VERSION = '0.1.0-beta.2';
const QID_DOWNLOADS = [
    'download' => ['QuickID3-0.1.0-beta.2-macOS-arm64.dmg', 'application/x-apple-diskimage'],
    'download-windows' => ['QuickID3-0.1.0-beta.2-Windows-x64.zip', 'application/zip'],
];
const QID_UNAVAILABLE = 'Requests are temporarily unavailable. Please try again later.';
$deadline = hrtime(true) / 1e9 + 18.0;

function qid_headers(): void {
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: strict-origin-when-cross-origin');
    header('X-Frame-Options: DENY');
    header("Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
}

function qid_escape(string $value): string {
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE | ENT_HTML5, 'UTF-8');
}

function qid_html(string $document, int $status = 200): never {
    http_response_code($status);
    header('Content-Type: text/html; charset=utf-8');
    header('Content-Length: ' . strlen($document));
    if ($status === 429) {
        header('Retry-After: 600');
    }
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'HEAD') {
        echo $document;
    }
    exit;
}

function qid_failure(int $status, string $message): never {
    $download = defined('QID_ROUTE') && isset(QID_DOWNLOADS[QID_ROUTE]);
    $title = $download ? 'Downloads' : 'Feature requests';
    $link = $download ? '/downloads.html' : '/feature-requests.html';
    $label = $download ? 'Return to downloads' : 'Return to request form';
    qid_html('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        . '<title>' . $title . ' — Quick·ID3</title><link rel="stylesheet" href="/assets/dsm-tokens.css"><link rel="stylesheet" href="/style.css"></head>'
        . '<body><main class="wrap" style="padding-block:64px"><h1>' . $title . '</h1><p style="margin-block:32px">'
        . qid_escape($message) . '</p><a class="button" href="' . $link . '">' . $label . '</a></main></body></html>', $status);
}

function qid_bridge(string $operation, string $session, ?array $fields = null): array {
    global $deadline;
    $input = ['op' => $operation, 'client_ip' => $_SERVER['REMOTE_ADDR'] ?? '', 'session' => $session];
    if ($fields !== null) {
        $input['fields'] = $fields;
    }
    $encoded = json_encode($input, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE);
    if (hrtime(true) / 1e9 >= $deadline) {
        throw new RuntimeException('Bridge unavailable');
    }
    $pipes = [];
    $process = @proc_open(['/usr/bin/python3', '-I', '-B', __DIR__ . '/request_bridge.py'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['file', '/dev/null', 'w']],
        $pipes, __DIR__, ['PATH' => '/usr/bin:/bin', 'LANG' => 'C.UTF-8'], ['bypass_shell' => true]);
    if (!is_resource($process)) {
        throw new RuntimeException('Bridge unavailable');
    }
    $output = '';
    $offset = 0;
    $exitCode = -1;
    try {
        stream_set_blocking($pipes[0], false);
        stream_set_blocking($pipes[1], false);
        while (true) {
            if (hrtime(true) / 1e9 >= $deadline) {
                throw new RuntimeException('Bridge unavailable');
            }
            $read = [$pipes[1]];
            $write = isset($pipes[0]) ? [$pipes[0]] : [];
            $except = [];
            if (@stream_select($read, $write, $except, 0, 100000) === false) {
                throw new RuntimeException('Bridge unavailable');
            }
            if ($write) {
                $written = @fwrite($pipes[0], substr($encoded, $offset, 8192));
                if ($written === false) {
                    throw new RuntimeException('Bridge unavailable');
                }
                $offset += $written;
                if ($offset === strlen($encoded)) {
                    fclose($pipes[0]);
                    unset($pipes[0]);
                }
            }
            if ($read) {
                $chunk = @fread($pipes[1], 8192);
                if ($chunk === false) {
                    throw new RuntimeException('Bridge unavailable');
                }
                $output .= $chunk;
                if (strlen($output) > 16384) {
                    throw new RuntimeException('Bridge unavailable');
                }
            }
            $state = proc_get_status($process);
            if (!$state['running'] && $exitCode === -1) {
                $exitCode = $state['exitcode'];
            }
            if (!$state['running'] && feof($pipes[1])) {
                break;
            }
        }
    } finally {
        foreach ($pipes as $pipe) {
            fclose($pipe);
        }
        if (proc_get_status($process)['running']) {
            proc_terminate($process, 9);
        }
        proc_close($process);
    }
    if ($exitCode !== 0) {
        throw new RuntimeException('Bridge unavailable');
    }
    $result = json_decode($output, true, 8, JSON_THROW_ON_ERROR);
    if (!is_array($result) || !is_bool($result['ok'] ?? null) || !is_int($result['status'] ?? null)) {
        throw new RuntimeException('Bridge unavailable');
    }
    if (!$result['ok'] && (!in_array($result['status'], [400, 403, 409, 429, 500, 503], true)
        || !is_string($result['message'] ?? null))) {
        throw new RuntimeException('Bridge unavailable');
    }
    if ($result['ok'] && ($result['status'] !== 200 || ($operation === 'issue'
        && (!is_string($result['token'] ?? null) || !preg_match('/\A[A-Za-z0-9_-]{43}\z/', $result['token']))))) {
        throw new RuntimeException('Bridge unavailable');
    }
    return $result;
}

function qid_session(): ?string {
    $raw = $_SERVER['HTTP_COOKIE'] ?? '';
    if (strlen($raw) > 2048) {
        return null;
    }
    $found = [];
    foreach (explode(';', $raw) as $part) {
        $parts = explode('=', trim($part), 2);
        if ($parts[0] === QID_COOKIE) {
            $found[] = $parts[1] ?? '';
        }
    }
    return count($found) === 1 && preg_match('/\A(?:[A-Za-z0-9_-]{43}|[a-f0-9]{64})\z/', $found[0]) ? $found[0] : null;
}

function qid_form(int $status = 200, string $notice = '', array $values = []): never {
    $template = @file_get_contents(__DIR__ . '/templates/feature-requests.html');
    if ($template === false) {
        throw new RuntimeException('Template unavailable');
    }
    $session = qid_session() ?? bin2hex(random_bytes(32));
    $result = qid_bridge('issue', $session);
    if (!$result['ok']) {
        qid_failure($result['status'], $result['message']);
    }
    $replacements = [
        '__REQUEST_NOTICE__' => qid_escape($notice),
        '__REQUEST_TOKEN__' => qid_escape($result['token']),
        '__REQUEST_TITLE__' => qid_escape($values['title'] ?? ''),
        '__REQUEST_DETAILS__' => qid_escape($values['details'] ?? ''),
    ];
    foreach (['mac', 'windows', 'both', 'other'] as $platform) {
        $replacements['__PLATFORM_' . strtoupper($platform) . '__'] = ($values['platform'] ?? '') === $platform ? 'selected' : '';
    }
    // Associative strtr makes one pass: submitted placeholders stay literal.
    $document = strtr($template, $replacements);
    header('Set-Cookie: ' . QID_COOKIE . '=' . $session . '; Path=/; Max-Age=1800; Secure; HttpOnly; SameSite=Strict');
    qid_html($document, $status);
}

function qid_fields(int $limit, array $allowed): array {
    if (isset($_SERVER['HTTP_TRANSFER_ENCODING'])
        || !preg_match('/\Aapplication\/x-www-form-urlencoded(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?\s*\z/i', $_SERVER['CONTENT_TYPE'] ?? '')) {
        qid_failure(400, 'Submit the form using the fields provided.');
    }
    $length = $_SERVER['CONTENT_LENGTH'] ?? '';
    if (!preg_match('/\A[0-9]{1,8}\z/', (string)$length) || (int)$length > $limit) {
        qid_failure(413, 'The submitted form is too large or could not be read.');
    }
    if ((int)$length === 0) {
        qid_failure(400, 'Submit the form using the fields provided.');
    }
    $raw = @file_get_contents('php://input', false, null, 0, $limit + 1);
    if ($raw === false || strlen($raw) !== (int)$length || strlen($raw) > $limit) {
        qid_failure(400, 'The submitted form could not be read.');
    }
    $fields = [];
    foreach (explode('&', $raw) as $pair) {
        $parts = explode('=', $pair, 2);
        if (count($parts) !== 2 || preg_match('/%(?![0-9a-fA-F]{2})/', $pair)) {
            qid_failure(400, 'The submitted form could not be read.');
        }
        $key = urldecode($parts[0]);
        $value = urldecode($parts[1]);
        if (!preg_match('//u', $key) || !preg_match('//u', $value)
            || !in_array($key, $allowed, true) || array_key_exists($key, $fields)) {
            qid_failure(400, 'Submit the form using the fields provided.');
        }
        $fields[$key] = $value;
    }
    if (isset($fields['details'])) {
        $fields['details'] = str_replace("\r\n", "\n", $fields['details']);
    }
    return $fields;
}

function qid_download(array $fields): never {
    if (count($fields) !== 2 || ($fields['accepted'] ?? '') !== 'evaluation-and-afl-2.1'
        || ($fields['version'] ?? '') !== QID_VERSION) {
        qid_failure(400, 'Accept both linked license texts before downloading.');
    }
    [$filename, $contentType] = QID_DOWNLOADS[QID_ROUTE];
    $handle = @fopen(__DIR__ . '/downloads/' . $filename, 'rb');
    if ($handle === false) {
        qid_failure(503, 'The download is temporarily unavailable.');
    }
    $stat = fstat($handle);
    if ($stat === false || ($stat['mode'] & 0170000) !== 0100000 || $stat['size'] <= 0) {
        fclose($handle);
        qid_failure(503, 'The download is temporarily unavailable.');
    }
    // No compression or output buffering: length describes the unchanged file.
    ini_set('zlib.output_compression', '0');
    while (ob_get_level() > 0) {
        if (!@ob_end_clean()) {
            fclose($handle);
            throw new RuntimeException('Output buffering unavailable');
        }
    }
    http_response_code(200);
    header('Content-Type: ' . $contentType);
    header('Content-Disposition: attachment; filename="' . $filename . '"');
    header('Content-Length: ' . $stat['size']);
    @fpassthru($handle);
    fclose($handle);
    exit;
}

qid_headers();
try {
    if (!defined('QID_ROUTE') || !is_string(QID_ROUTE) || !in_array(QID_ROUTE, ['form', 'submit', 'download', 'download-windows'], true)) {
        qid_failure(404, 'This page is unavailable.');
    }
    $host = $_SERVER['HTTP_HOST'] ?? '';
    $origin = $_SERVER['HTTP_ORIGIN'] ?? null;
    $fetchSite = $_SERVER['HTTP_SEC_FETCH_SITE'] ?? null;
    if (!in_array($host, ['quickid3.com', 'www.quickid3.com'], true)
        || !in_array(strtolower((string)($_SERVER['HTTPS'] ?? '')), ['on', '1'], true)
        || ($origin !== null && $origin !== 'https://' . $host)
        || ($fetchSite !== null && !in_array($fetchSite, ['same-origin', 'same-site', 'none'], true))) {
        qid_failure(403, 'Open this form on https://quickid3.com and try again.');
    }
    $method = $_SERVER['REQUEST_METHOD'] ?? '';
    $allowed = QID_ROUTE === 'form' ? ['GET', 'HEAD'] : ['POST'];
    if (!in_array($method, $allowed, true)) {
        header('Allow: ' . implode(', ', $allowed));
        qid_failure(405, 'Use the form provided for this action.');
    }
    if (QID_ROUTE === 'form') {
        if ($method === 'HEAD') {
            qid_html(''); // No token, cookie refresh, or rate-limit consumption.
        }
        qid_form();
    }
    if (isset(QID_DOWNLOADS[QID_ROUTE])) {
        qid_download(qid_fields(1024, ['accepted', 'version']));
    }
    $fields = qid_fields(65536, ['token', 'title', 'details', 'platform', 'website']);
    $session = qid_session();
    if ($session === null) {
        qid_failure(403, 'Open the request form first and allow its temporary cookie.');
    }
    $result = qid_bridge('submit', $session, $fields);
    if (!$result['ok']) {
        if ($result['status'] === 400) {
            qid_form(400, $result['message'], $fields);
        }
        qid_failure($result['status'], $result['message']);
    }
    http_response_code(303);
    header('Location: /request-received.html');
    header('Content-Length: 0');
} catch (Throwable $error) {
    // Do not expose stderr, exception text, request data, or private paths.
    if (!headers_sent()) {
        qid_failure(503, QID_UNAVAILABLE);
    }
}
