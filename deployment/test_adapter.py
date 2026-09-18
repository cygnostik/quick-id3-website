"""Isolated integration harness: python3 -B deployment/test_adapter.py.
Requires php CLI and /usr/bin/python3. Never touches the live DB or approved DMG.
The test router models server-verified TLS; it does not trust forwarding headers.
"""
import html
import http.client
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from urllib.parse import urlencode

SOURCE = Path(__file__).resolve().parent
# Works beside runtime adapter/store files or in the source deployment/ folder.
APP = SOURCE if (SOURCE / 'request_store.py').is_file() else SOURCE.parent


class AdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='qid-adapter-')
        cls.root = Path(cls.tmp.name)
        for name in ('adapter.php', 'request_bridge.py'):
            shutil.copyfile(SOURCE / name, cls.root / name)
        shutil.copyfile(APP / 'request_store.py', cls.root / 'request_store.py')
        (cls.root / 'templates').mkdir()
        shutil.copyfile(APP / 'templates/feature-requests.html', cls.root / 'templates/feature-requests.html')
        (cls.root / 'downloads').mkdir()
        cls.fixture = b'ISOLATED TEST FILE, NOT A DMG\x00\xff'
        (cls.root / 'downloads/QuickID3-0.1.0-beta.2-macOS-arm64.dmg').write_bytes(cls.fixture)
        cls.windows_fixture = b'ISOLATED TEST FILE, NOT A ZIP\x00\xfe'
        (cls.root / 'downloads/QuickID3-0.1.0-beta.2-Windows-x64.zip').write_bytes(cls.windows_fixture)
        (cls.root / 'router.php').write_text('''<?php
$_SERVER['HTTPS'] = 'on';
$routes = ['/feature-requests.html'=>'form','/feature-requests'=>'submit','/download/mac'=>'download','/download/windows'=>'download-windows'];
define('QID_ROUTE', $routes[parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH)] ?? 'invalid');
require __DIR__ . '/adapter.php';
''')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.server = subprocess.Popen(['php', '-S', f'127.0.0.1:{cls.port}', str(cls.root / 'router.php')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.1):
                    break
            except OSError:
                if cls.server.poll() is not None:
                    raise RuntimeError('PHP test server failed')
                time.sleep(.05)
        else:
            raise RuntimeError('PHP test server not ready')

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.tmp.cleanup()

    def request(self, method, path, body=None, **extra):
        headers = {'Host': 'quickid3.com', **extra}
        if body is not None:
            headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=22)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def form(self, **headers):
        status, headers, body = self.request('GET', '/feature-requests.html', **headers)
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        for part in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Max-Age=1800'):
            self.assertIn(part, headers['Set-Cookie'])
        token = re.search(rb'name="token" value="([^"]+)"', body).group(1).decode()
        return cookie, token

    def test_end_to_end(self):
        self.assertEqual(self.request('HEAD', '/feature-requests.html')[0], 200)
        self.assertFalse((self.root / 'private/feature-requests.sqlite3').exists())
        for method in ('GET', 'HEAD'):
            result = self.request(method, '/download/mac')
            self.assertEqual(result[0], 405)
            self.assertEqual(result[1]['Allow'], 'POST')
        self.assertEqual(self.request('GET', '/feature-requests.html', Host='evil.invalid')[0], 403)
        self.assertEqual(self.request('GET', '/feature-requests.html', Origin='https://evil.invalid')[0], 403)
        self.assertEqual(self.request('GET', '/feature-requests.html', **{'Sec-Fetch-Site': 'cross-site'})[0], 403)
        cookie, token = self.form(**{'X-Forwarded-For': '198.51.100.99'})
        db = self.root / 'private/feature-requests.sqlite3'
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute('select client_ip from form_tokens').fetchone()[0], '127.0.0.1')
        fields = dict(token=token, title='Add album grouping', details='Native textarea line one.\r\nSecond line with details.', platform='mac', website='')
        self.assertEqual(self.request('POST', '/feature-requests', urlencode(fields), Cookie=cookie)[0], 403)
        for bad in ('title=a&title=b', 'title=%FF', 'title=%GG', 'title=x&extra=y', 'title[]=x', 'title=x&'):
            self.assertEqual(self.request('POST', '/feature-requests', bad, Cookie=cookie)[0], 400)
        self.assertEqual(self.request('POST', '/feature-requests', 'x=' + 'a'*65536, Cookie=cookie)[0], 413)
        invalid = dict(fields, title='__REQUEST_DETAILS__ <script>', details='short')
        status, headers, body = self.request('POST', '/feature-requests', urlencode(invalid), Cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn(b'__REQUEST_DETAILS__ &lt;script&gt;', body)
        self.assertIn(b'>short</textarea>', body)
        self.assertIn(b'value="mac" selected', body)
        self.assertIn('Set-Cookie', headers)
        time.sleep(3.1)
        status, headers, body = self.request('POST', '/feature-requests', urlencode(fields), Cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/request-received.html')
        self.assertEqual(body, b'')
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute('select title,details,platform,status from requests').fetchall(), [(fields['title'], fields['details'].replace('\r\n', '\n'), 'mac', 'new')])
        self.assertEqual(self.request('POST', '/feature-requests', urlencode(fields), Cookie=cookie)[0], 409)
        for data in ('', 'accepted=yes&version=0.1.0-beta.1', 'accepted=evaluation-and-afl-2.1&version=0.1.0-beta.1&version=0.1.0-beta.1'):
            self.assertEqual(self.request('POST', '/download/mac', data)[0], 400)
        status, headers, body = self.request('POST', '/download/mac', 'accepted=evaluation-and-afl-2.1&version=0.1.0-beta.2')
        self.assertEqual(status, 200)
        self.assertEqual(body, self.fixture)
        self.assertEqual(int(headers['Content-Length']), len(self.fixture))
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="QuickID3-0.1.0-beta.2-macOS-arm64.dmg"')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        # Bridge failures remain JSON, with no traceback or local paths.
        bridge = subprocess.run(['/usr/bin/python3', '-I', '-B', str(self.root / 'request_bridge.py')], input=b'{broken', capture_output=True, timeout=20)
        self.assertFalse(bridge.stderr)
        self.assertEqual(json.loads(bridge.stdout)['status'], 400)

    def test_windows_and_mac_download_boundaries(self):
        accepted = 'accepted=evaluation-and-afl-2.1&version=0.1.0-beta.2'
        for platform in ('mac', 'windows'):
            route = '/download/' + platform
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request(method, route)[0], 405)
            for invalid in ('', 'version=0.1.0-beta.2', 'accepted=yes&version=0.1.0-beta.2',
                            'accepted=evaluation-and-afl-2.1&version=0.1.0-beta.1',
                            accepted + '&version=0.1.0-beta.2', accepted + '&extra='):
                self.assertEqual(self.request('POST', route, invalid)[0], 400)
            self.assertEqual(self.request('POST', route, accepted, Origin='https://evil.invalid')[0], 403)
        status, headers, body = self.request('POST', '/download/windows', accepted)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.windows_fixture)
        self.assertEqual(headers['Content-Type'], 'application/zip')
        self.assertEqual(int(headers['Content-Length']), len(self.windows_fixture))
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="QuickID3-0.1.0-beta.2-Windows-x64.zip"')
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_z_failure_limits(self):
        bridge_path = self.root / 'request_bridge.py'
        original = bridge_path.read_bytes()
        # Exercise the real private bridge with an opaque, non-cookie identity.
        payload = json.dumps(dict(op='issue', client_ip='192.0.2.7', session='opaque-session')).encode()
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(bridge_path)], input=payload, capture_output=True, timeout=20)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertFalse(result.stderr)
        with sqlite3.connect(self.root / 'private/feature-requests.sqlite3') as conn:
            conn.execute("update spam_events set client_ip='127.0.0.1' where kind='issue'")
            conn.executemany("insert into spam_events(client_ip,kind,created_at) values ('127.0.0.1','issue',?)", [(time.time(),)] * 20)
        status, headers, body = self.request('GET', '/feature-requests.html')
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '600')
        try:
            for program in (
                "import sys; sys.stdout.write('x' * 100000)",
                "import sys; sys.stderr.write('PRIVATE DIAGNOSTIC' * 100000); raise RuntimeError('PRIVATE DIAGNOSTIC')",
                "import time; time.sleep(30)",
            ):
                bridge_path.write_text(program)
                start = time.monotonic()
                status, headers, body = self.request('GET', '/feature-requests.html')
                elapsed = time.monotonic() - start
                self.assertEqual(status, 503)
                self.assertLess(elapsed, 20)
                self.assertNotIn(b'PRIVATE DIAGNOSTIC', body)
                self.assertNotIn(str(self.root).encode(), body)
                self.assertIn(b'href="/feature-requests.html"', body)
        finally:
            bridge_path.write_bytes(original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
