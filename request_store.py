"""Private, local SQLite feature requests (Python 3.9 standard library).

Use a dedicated private directory outside site/: RequestStore("private/requests.db").
The HTTP layer owns request size limits, trusted client IP extraction, origin/Host
checks and the HttpOnly SameSite session cookie. Never log fields or bearer tokens.
Anti-spam records contain short-lived raw IP/session/token data, not request rows.
SQLite errors intentionally propagate for the HTTP layer to report a generic 500.
"""
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import unicodedata
from contextlib import contextmanager


class RequestError(Exception):
    """Expected rejection; both status and message are safe for HTTP responses."""

    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


class RequestStore:
    TOKEN_TTL = 1800
    MIN_AGE = 3
    SHORT_WINDOW = 600
    ACCEPT_WINDOW = 3600
    ISSUE_LIMIT = 20
    ATTEMPT_LIMIT = 30
    ACCEPT_LIMIT = 5
    GLOBAL_TOKEN_LIMIT = 2000
    GLOBAL_ISSUE_LIMIT = 2000
    GLOBAL_ATTEMPT_LIMIT = 10000
    _FIELDS = frozenset(("token", "title", "details", "platform", "website"))
    # Count explicit URI schemes and www links across title + details.
    _URL = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|mailto:|www\.)[^\s<>]*", re.I)

    def __init__(self, db_path, clock=time.time):
        supplied = Path(db_path).expanduser().absolute()
        site = (Path(__file__).resolve().parent / "site").resolve()
        resolved = supplied.resolve()
        if ("site" in supplied.parts or resolved == site or site in resolved.parents
                or "site" in resolved.parts):
            raise ValueError("The request database must be outside site/.")
        if supplied.is_symlink():
            raise ValueError("The request database must not be a symbolic link.")
        if resolved.parent == Path(__file__).resolve().parent:
            raise ValueError("Use a dedicated private database directory.")
        # The caller must supply a dedicated directory; never point at a shared
        # directory, since its mode is deliberately restricted here.
        resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(str(resolved.parent), 0o700)
        try:
            fd = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            info = resolved.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("The request database must be a regular private file.")
        else:
            os.close(fd)
        os.chmod(str(resolved), 0o600)
        self.db_path = resolved
        self.clock = clock
        with self._transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL, title TEXT NOT NULL,
                details TEXT NOT NULL, platform TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS form_tokens (
                token TEXT PRIMARY KEY, client_ip TEXT NOT NULL,
                session_id TEXT NOT NULL, created_at REAL NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS spam_events (
                id INTEGER PRIMARY KEY, client_ip TEXT NOT NULL,
                kind TEXT NOT NULL, created_at REAL NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS token_age ON form_tokens(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS event_rate ON spam_events(kind, client_ip, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS event_age ON spam_events(kind, created_at)")
            self._prune(conn, self._now())

    @contextmanager
    def _transaction(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except RequestError:
                # Invalid submissions still persist their attempt, and pruning.
                conn.commit()
                raise
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    def _now(self):
        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("The store clock must return a finite timestamp.")
        return now

    @staticmethod
    def _identity(client_ip, session_id):
        for value, maximum in ((client_ip, 128), (session_id, 256)):
            if (not isinstance(value, str) or not value or len(value) > maximum
                    or any(unicodedata.category(c).startswith("C") for c in value)):
                raise RequestError(400, "Invalid request identity.")

    def _prune(self, conn, now):
        conn.execute("DELETE FROM form_tokens WHERE created_at <= ?", (now - self.TOKEN_TTL,))
        conn.execute("DELETE FROM spam_events WHERE kind IN ('issue', 'attempt') AND created_at <= ?",
                     (now - self.SHORT_WINDOW,))
        conn.execute("DELETE FROM spam_events WHERE kind = 'accepted' AND created_at <= ?",
                     (now - self.ACCEPT_WINDOW,))

    @staticmethod
    def _count(conn, kind, client_ip=None):
        if client_ip is None:
            return conn.execute("SELECT COUNT(*) FROM spam_events WHERE kind = ?", (kind,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM spam_events WHERE kind = ? AND client_ip = ?",
                            (kind, client_ip)).fetchone()[0]

    @staticmethod
    def _event(conn, client_ip, kind, now):
        conn.execute("INSERT INTO spam_events(client_ip, kind, created_at) VALUES (?, ?, ?)",
                     (client_ip, kind, now))

    def issue(self, client_ip, session_id):
        """Mint an IP-and-session-bound single-use form token, or raise RequestError."""
        self._identity(client_ip, session_id)
        with self._transaction() as conn:
            now = self._now()  # After acquiring the write lock, never before.
            self._prune(conn, now)
            if (self._count(conn, "issue", client_ip) >= self.ISSUE_LIMIT
                    or self._count(conn, "issue") >= self.GLOBAL_ISSUE_LIMIT
                    or conn.execute("SELECT COUNT(*) FROM form_tokens").fetchone()[0] >= self.GLOBAL_TOKEN_LIMIT):
                raise RequestError(429, "Too many forms. Please try again later.")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO form_tokens(token, client_ip, session_id, created_at, consumed) VALUES (?, ?, ?, ?, 0)",
                         (token, client_ip, session_id, now))
            self._event(conn, client_ip, "issue", now)
            return token

    def _validate(self, fields):
        if not isinstance(fields, dict) or set(fields) != self._FIELDS:
            raise RequestError(400, "Invalid form fields.")
        if any(not isinstance(v, str) for v in fields.values()):
            raise RequestError(400, "Form fields must be text.")
        # Length checks precede character/URL scans, bounding work for this API.
        if (not 5 <= len(fields["title"]) <= 120
                or not 20 <= len(fields["details"]) <= 4000
                or len(fields["token"]) > 128 or len(fields["website"]) > 2000
                or len(fields["platform"]) > 20):
            raise RequestError(400, "Invalid form field length.")
        for key, value in fields.items():
            if any(unicodedata.category(c).startswith("C")
                   and not (key == "details" and c in "\n\t") for c in value):
                raise RequestError(400, "Form contains invalid characters.")
        title, details = fields["title"].strip(), fields["details"].strip()
        if len(title) < 5 or len(details) < 20:
            raise RequestError(400, "Please provide a descriptive title and details.")
        if fields["platform"] not in ("mac", "windows", "both", "other"):
            raise RequestError(400, "Invalid platform.")
        if fields["website"] != "":
            raise RequestError(403, "Unable to accept this form.")
        if len(self._URL.findall(title + "\n" + details)) > 2:
            raise RequestError(400, "Please include no more than two links.")
        return title, details, fields["platform"]

    def submit(self, client_ip, session_id, fields):
        """Validate and atomically accept a request, returning its integer row id.

        400: invalid fields; 403: missing/expired/bound/too-young token or
        honeypot; 409: already used token; 429: persistent rate limit.
        A validation failure does not consume the token, but counts an attempt.
        All five fields (including empty website) are required.
        """
        self._identity(client_ip, session_id)
        with self._transaction() as conn:
            now = self._now()
            self._prune(conn, now)
            if (self._count(conn, "attempt", client_ip) >= self.ATTEMPT_LIMIT
                    or self._count(conn, "attempt") >= self.GLOBAL_ATTEMPT_LIMIT):
                raise RequestError(429, "Too many attempts. Please try again later.")
            self._event(conn, client_ip, "attempt", now)
            if self._count(conn, "accepted", client_ip) >= self.ACCEPT_LIMIT:
                raise RequestError(429, "Request limit reached. Please try again later.")
            title, details, platform = self._validate(fields)
            row = conn.execute("SELECT client_ip, session_id, created_at, consumed FROM form_tokens WHERE token = ?",
                               (fields["token"],)).fetchone()
            if row is None or row[0] != client_ip or row[1] != session_id:
                raise RequestError(403, "Invalid or expired form. Please reload the form.")
            if row[3]:
                raise RequestError(409, "This form has already been submitted.")
            if now < row[2] + self.MIN_AGE:
                raise RequestError(403, "Please wait a few seconds before submitting.")
            if now >= row[2] + self.TOKEN_TTL:
                raise RequestError(403, "Invalid or expired form. Please reload the form.")
            cursor = conn.execute("INSERT INTO requests(created_at, title, details, platform, status) VALUES (?, ?, ?, ?, 'new')",
                                  (now, title, details, platform))
            conn.execute("UPDATE form_tokens SET consumed = 1 WHERE token = ?", (fields["token"],))
            self._event(conn, client_ip, "accepted", now)
            return int(cursor.lastrowid)
