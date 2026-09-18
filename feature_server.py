"""Loopback-only multi-page preview with a private feature-request database.
The download handler streams the existing approved Mac and Windows files unchanged.
No email transport, public request listing or deployment is enabled here.
"""
import argparse
import html
import re
import secrets
import sqlite3
from http.cookies import SimpleCookie, CookieError
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from serve import PreviewHandler, ROOT, configure_downloads
from request_store import RequestStore, RequestError

TEMPLATE = ROOT.parent / 'templates' / 'feature-requests.html'
COOKIE = 'qid_request_session'


class SiteHandler(PreviewHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def end_headers(self):
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        super().end_headers()

    def allowed_request(self):
        host = self.headers.get('Host', '')
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        origin = self.headers.get('Origin')
        return (host in hosts and (origin is None or origin == 'http://' + host)
                and self.headers.get('Sec-Fetch-Site') != 'cross-site')

    def session(self):
        cookie = SimpleCookie()
        raw = self.headers.get('Cookie', '')
        if len(raw) <= 2048:
            try:
                cookie.load(raw)
            except CookieError:
                pass
        value = cookie.get(COOKIE)
        if value and re.fullmatch(r'[A-Za-z0-9_-]{43}', value.value):
            return value.value, False
        return secrets.token_urlsafe(32), True

    def send_html(self, content, status=200, session=None, head=False):
        body = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if session:
            # Secure is required on the future HTTPS host, not loopback HTTP.
            self.send_header('Set-Cookie', f'{COOKIE}={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age=1800')
        if status == 429:
            self.send_header('Retry-After', '600')
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def form(self, status=200, notice='', values=None):
        values = values or {}
        session, fresh = self.session()
        try:
            token = self.server.store.issue(self.client_address[0], session)
        except RequestError as error:
            self.failure(error.status, error.message)
            return
        except (sqlite3.Error, OSError):
            self.failure(503, 'Requests are temporarily unavailable. Please try again later.')
            return
        document = TEMPLATE.read_text(encoding='utf-8')
        replacements = {
            '__REQUEST_NOTICE__': notice,
            '__REQUEST_TOKEN__': token,
            '__REQUEST_TITLE__': values.get('title', ''),
            '__REQUEST_DETAILS__': values.get('details', ''),
        }
        # One substitution pass: visitor text cannot become another placeholder.
        substitutions = {key: html.escape(value, quote=True) for key, value in replacements.items()}
        for platform in ['mac', 'windows', 'both', 'other']:
            substitutions['__PLATFORM_' + platform.upper() + '__'] = 'selected' if values.get('platform') == platform else ''
        document = re.sub('|'.join(map(re.escape, substitutions)), lambda match: substitutions[match.group()], document)
        self.send_html(document, status=status, session=session)

    def failure(self, status, message):
        # Never reflect arbitrary exception or visitor input into an error page.
        content = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                   '<title>Feature requests — Quick·ID3</title><link rel="stylesheet" href="/assets/dsm-tokens.css"><link rel="stylesheet" href="/style.css">'
                   '<main class="wrap" style="padding-block:64px"><h1>Feature requests</h1><p style="margin-block:32px">'
                   + html.escape(message) + '</p><a class="button" href="/feature-requests.html">Return to request form</a></main></html>')
        self.send_html(content, status=status)

    def do_GET(self):
        if not self.allowed_request():
            self.send_error(403)
            return
        if urlsplit(self.path).path == '/feature-requests.html':
            self.form()
            return
        super().do_GET()

    def do_HEAD(self):
        if not self.allowed_request():
            self.send_error(403)
            return
        if urlsplit(self.path).path == '/feature-requests.html':
            # HEAD never mints tokens or consumes form-rate capacity.
            self.send_html('', head=True)
            return
        super().do_HEAD()

    def do_POST(self):
        if not self.allowed_request():
            self.send_error(403)
            return
        if urlsplit(self.path).path != '/feature-requests':
            super().do_POST()
            return
        if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'application/x-www-form-urlencoded':
            self.failure(400, 'Submit the feature-request form using the fields provided.')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                self.failure(413, 'The request is too large or empty.')
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete body')
            fields = parse_qs(raw.decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=10)
            if any(len(value) != 1 for value in fields.values()):
                raise ValueError('Repeated fields')
            fields = {key: value[0] for key, value in fields.items()}
            # Native HTML textarea submissions use CRLF on the wire.
            if 'details' in fields:
                fields['details'] = fields['details'].replace('\r\n', '\n')
        except (ValueError, UnicodeError):
            self.failure(400, 'The request could not be read. Please use the request form.')
            return
        session, fresh = self.session()
        if fresh:
            self.failure(403, 'Open the request form first and allow its temporary cookie.')
            return
        try:
            self.server.store.submit(self.client_address[0], session, fields)
        except RequestError as error:
            if error.status == 400:
                self.form(status=error.status, notice=error.message, values=fields)
            else:
                self.failure(error.status, error.message)
            return
        except (sqlite3.Error, OSError):
            self.failure(503, 'Your request could not be saved. Please try again later.')
            return
        self.send_response(303)
        self.send_header('Location', '/request-received.html')
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--dmg', type=Path, required=True)
    parser.add_argument('--windows-zip', type=Path, required=True)
    parser.add_argument('--db', type=Path, default=ROOT.parent / 'private' / 'feature-requests.sqlite3')
    args = parser.parse_args()

    if args.db.resolve().is_relative_to(ROOT.resolve()):
        parser.error('The request database must be outside site/.')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), SiteHandler)
    configure_downloads(server, args, parser)
    server.store = RequestStore(args.db)
    print(f'Preview ready: http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
