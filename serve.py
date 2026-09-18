"""Loopback-only website preview with explicit beta license assent.
The approved downloads remain outside the document root. No packaging or upload.
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qs
import argparse
import os
import shutil

ROOT = Path(__file__).resolve().parent / "site"
VERSION = "0.1.0-beta.2"
FILENAME = f"QuickID3-{VERSION}-macOS-arm64.dmg"
WINDOWS_FILENAME = f"QuickID3-{VERSION}-Windows-x64.zip"
DOWNLOADS = {
    "/download/mac": ("dmg", FILENAME, "application/x-apple-diskimage"),
    "/download/windows": ("windows_zip", WINDOWS_FILENAME, "application/zip"),
}


def configure_downloads(server, args, parser):
    for attribute, filename, _ in DOWNLOADS.values():
        path = getattr(args, attribute).resolve()
        if path.name != filename or not path.is_file() or path.is_relative_to(ROOT.resolve()):
            parser.error(f"Supply the existing approved {filename} outside site/.")
        setattr(server, attribute, path)


class PreviewHandler(SimpleHTTPRequestHandler):
    server_version = "QuickID3Preview"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def send_head(self):
        route = unquote(urlsplit(self.path).path)
        if route in DOWNLOADS:
            self.send_error(405, "Use the license acceptance form to download.")
            return None
        if any(part.startswith(".") for part in route.split("/") if part):
            self.send_error(404)
            return None
        candidate = Path(self.translate_path(self.path)).resolve()
        if not candidate.is_relative_to(ROOT.resolve()):
            self.send_error(404)
            return None
        return super().send_head()

    def do_POST(self):
        download = DOWNLOADS.get(urlsplit(self.path).path)
        if download is None:
            self.send_error(404)
            return
        host = self.headers.get("Host", "")
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if host not in allowed_hosts or (origin and origin != "http://" + host):
            self.send_error(403)
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403)
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.send_error(400)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("invalid length")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        except (ValueError, UnicodeError):
            self.send_error(400, "Accept both linked license texts before downloading.")
            return
        if fields != {"accepted": ["evaluation-and-afl-2.1"], "version": [VERSION]}:
            self.send_error(400, "Accept both linked license texts before downloading.")
            return
        attribute, filename, content_type = download
        try:
            handle = getattr(self.server, attribute).open("rb")
        except OSError:
            self.send_error(503, "The download is unavailable.")
            return
        with handle:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format, *args):
        # No durable acceptance/access log or visitor profile in this preview.
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--dmg", type=Path, required=True)
    parser.add_argument("--windows-zip", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    configure_downloads(server, args, parser)
    print(f"Preview ready: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
