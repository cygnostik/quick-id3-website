"""Local-only feature request store tests; never print bearer tokens."""
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from request_store import RequestError, RequestStore


def _process_submit(db, fields, now, ready, start, results):
    """Spawn-safe worker; result diagnostics contain status only, never tokens."""
    try:
        store = RequestStore(db, clock=lambda: now)
        ready.put(True)
        if not start.wait(10):
            results.put("timeout")
            return
        store.submit("127.0.0.1", "test-session", fields)
        results.put(201)
    except RequestError as exc:
        results.put(exc.status)
    except Exception:
        results.put("unexpected store failure")


class RequestStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 100000.0
        self.db = Path(self.tmp.name) / "private" / "requests.sqlite3"
        self.store = RequestStore(self.db, clock=lambda: self.now)

    def test_happy_path_private_database_readback(self):
        token = self.store.issue("127.0.0.1", "test-session")
        self.now += 3
        row_id = self.store.submit("127.0.0.1", "test-session", {
            "token": token, "title": "Batch artwork editing",
            "details": "Please support editing artwork for several tracks.",
            "platform": "mac", "website": "",
        })
        self.assertIsInstance(row_id, int)
        with sqlite3.connect(str(self.db)) as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (row_id,)).fetchone()
            columns = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        self.assertEqual(columns, ["id", "created_at", "title", "details", "platform", "status"])
        self.assertEqual(row, (row_id, self.now, "Batch artwork editing",
                              "Please support editing artwork for several tracks.", "mac", "new"))

    def fields(self, token=None, **updates):
        result = {
            "token": token if token is not None else self.store.issue("127.0.0.1", "test-session"),
            "title": "Batch artwork editing",
            "details": "Please support editing artwork for several tracks.",
            "platform": "mac", "website": "",
        }
        result.update(updates)
        return result

    def submit(self, fields, ip="127.0.0.1", session="test-session", store=None):
        return (store or self.store).submit(ip, session, fields)

    def rejected(self, status, action):
        with self.assertRaises(RequestError) as caught:
            action()
        self.assertEqual(caught.exception.status, status)
        self.assertIsInstance(caught.exception.message, str)
        self.assertTrue(bool(caught.exception.message))

    def scalar(self, sql, params=()):
        with sqlite3.connect(str(self.db)) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def test_token_randomness_and_generator(self):
        first = self.store.issue("127.0.0.1", "test-session")
        second = self.store.issue("127.0.0.1", "test-session")
        self.assertTrue(isinstance(first, str) and 32 <= len(first) <= 128)
        self.assertTrue(first != second, "Tokens must differ")
        self.assertTrue(first not in repr(self.store), "Store repr must not disclose tokens")
        with patch("request_store.secrets.token_urlsafe", wraps=__import__("secrets").token_urlsafe) as generator:
            self.store.issue("127.0.0.1", "test-session")
            generator.assert_called_once_with(32)

    def test_too_fast_then_minimum_age_success(self):
        fields = self.fields()
        self.now += 2.999
        self.rejected(403, lambda: self.submit(fields))
        self.now += .001
        self.assertIsInstance(self.submit(fields), int)

    def test_expiry_at_boundary_and_pruning(self):
        fields = self.fields()
        self.now += 1800
        self.rejected(403, lambda: self.submit(fields))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM form_tokens"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 0)

    def test_success_just_before_expiry(self):
        fields = self.fields()
        self.now += 1799.999
        self.assertIsInstance(self.submit(fields), int)

    def test_invalid_token_and_binding_do_not_consume(self):
        fields = self.fields()
        self.now += 3
        self.rejected(403, lambda: self.submit(dict(fields, token="invalid-token")))
        self.rejected(403, lambda: self.submit(fields, ip="127.0.0.2"))
        self.rejected(403, lambda: self.submit(fields, session="other-session"))
        self.assertIsInstance(self.submit(fields), int)

    def test_honeypot_must_be_exactly_empty(self):
        fields = self.fields()
        self.now += 3
        for value in ("https://example.test", " "):
            self.rejected(403, lambda: self.submit(dict(fields, website=value)))
        self.assertIsInstance(self.submit(fields), int)

    def test_replay_conflict_and_safe_error(self):
        fields = self.fields()
        self.now += 3
        self.submit(fields)
        with self.assertRaises(RequestError) as caught:
            self.submit(fields)
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(fields["token"] not in str(caught.exception), "Do not disclose tokens")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 1)

    def test_invalid_shape_unknown_missing_and_nonstring_fields(self):
        fields = self.fields()
        self.now += 3
        invalid = [None, [], "form", dict(fields, unexpected="value")]
        for key in fields:
            missing = dict(fields)
            del missing[key]
            invalid.extend((missing, dict(fields, **{key: []}), dict(fields, **{key: 3})))
        for index, bad in enumerate(invalid):
            with self.subTest(case=index):
                self.rejected(400, lambda: self.submit(bad))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 0)
        self.assertIsInstance(self.submit(fields), int)

    def test_invalid_lengths_platform_and_whitespace(self):
        fields = self.fields()
        self.now += 3
        changes = [dict(title="four"), dict(title="x" * 121), dict(title=" " * 8),
                   dict(details="x" * 19), dict(details="x" * 4001), dict(details=" " * 30),
                   dict(platform="linux"), dict(platform="Mac"), dict(token="x" * 129)]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                self.rejected(400, lambda: self.submit(dict(fields, **change)))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 0)

    def test_control_characters_rejected(self):
        fields = self.fields()
        self.now += 3
        changes = [("title", "\x00"), ("title", "\n"), ("title", "\t"),
                   ("details", "\x00"), ("details", "\r"), ("details", "\x01"),
                   ("details", "\x7f"), ("details", "\x85"), ("details", "\u202e"),
                   ("details", "\ud800"), ("token", "\x00"), ("website", "\x00")]
        for index, (key, character) in enumerate(changes):
            with self.subTest(case=index):
                self.rejected(400, lambda: self.submit(dict(fields, **{key: fields[key] + character})))

    def test_valid_boundaries_platforms_and_details_format(self):
        cases = [("12345", "d" * 20, "mac"), ("t" * 120, "d" * 4000, "windows"),
                 ("Unicode café", "First line.\nSecond line\twith tab.", "both"),
                 ("Other platform", "Please support this other platform.", "other")]
        for title, details, platform in cases:
            fields = self.fields(title=title, details=details, platform=platform)
            self.now += 3
            self.assertIsInstance(self.submit(fields), int)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 4)

    def test_maximum_two_links_across_title_and_details(self):
        fields = self.fields(title="See https://a.test", details="Please see https://b.test and www.c.test for examples.")
        self.now += 3
        self.rejected(400, lambda: self.submit(fields))
        self.rejected(400, lambda: self.submit(dict(fields, title="Three links", details="ftp://a.test mailto:person@example.test www.c.test")))
        self.assertIsInstance(self.submit(dict(fields, title="Two references", details="Compare https://www.a.test and https://www.b.test please.")), int)

    def test_sql_injection_is_stored_as_plain_data(self):
        details = "'); DROP TABLE requests; -- this must remain plain text"
        fields = self.fields(title="Safe title '; --", details=details)
        self.now += 3
        row_id = self.submit(fields)
        self.assertEqual(self.scalar("SELECT details FROM requests WHERE id = ?", (row_id,)), details)

    def test_issue_limit_per_ip_cannot_be_bypassed_by_session(self):
        for index in range(20):
            self.store.issue("127.0.0.1", "session-" + str(index))
        self.rejected(429, lambda: self.store.issue("127.0.0.1", "fresh-session"))
        self.assertTrue(bool(self.store.issue("127.0.0.2", "session")))
        self.now += 600
        self.assertTrue(bool(self.store.issue("127.0.0.1", "session")))

    def test_invalid_tokens_count_and_attempt_table_is_bounded(self):
        fields = self.fields(token="invalid-token")
        for _ in range(30):
            self.rejected(403, lambda: self.submit(fields))
        for _ in range(5):
            self.rejected(429, lambda: self.submit(fields, session="fresh-session"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events WHERE kind='attempt'"), 30)
        self.now += 600
        self.rejected(403, lambda: self.submit(fields))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events WHERE kind='attempt'"), 1)

    def test_malformed_fields_count_as_attempts(self):
        for _ in range(30):
            self.rejected(400, lambda: self.submit(None))
        self.rejected(429, lambda: self.submit(None))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events WHERE kind='attempt'"), 30)

    def test_accepted_limit_is_per_ip_and_survives_restart(self):
        for _ in range(5):
            fields = self.fields()
            self.now += 3
            self.submit(fields)
        fields = self.fields()
        self.now += 3
        restarted = RequestStore(self.db, clock=lambda: self.now)
        self.rejected(429, lambda: self.submit(fields, store=restarted))
        other = self.store.issue("127.0.0.2", "test-session")
        self.now += 3
        self.assertIsInstance(self.submit(self.fields(token=other), ip="127.0.0.2"), int)
        self.now += 3600
        fields = self.fields()
        self.now += 3
        self.assertIsInstance(self.submit(fields, store=restarted), int)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 7)

    def test_tokens_replay_issue_and_attempt_limits_persist(self):
        fields = self.fields()
        for _ in range(19):
            self.store.issue("127.0.0.1", "test-session")
        self.now += 3
        restarted = RequestStore(self.db, clock=lambda: self.now)
        self.rejected(429, lambda: restarted.issue("127.0.0.1", "new-session"))
        self.assertIsInstance(self.submit(fields, store=restarted), int)
        self.rejected(409, lambda: self.submit(fields))
        for _ in range(28):
            self.rejected(403, lambda: self.submit(self.fields(token="invalid-token")))
        restarted = RequestStore(self.db, clock=lambda: self.now)
        self.rejected(429, lambda: self.submit(None, store=restarted))
        self.now += 3600
        RequestStore(self.db, clock=lambda: self.now)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM form_tokens"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 1)

    def test_global_issuance_and_live_token_caps(self):
        self.store.GLOBAL_ISSUE_LIMIT = 3
        self.store.GLOBAL_TOKEN_LIMIT = 4
        for index in range(3):
            self.store.issue("client-" + str(index), "session")
        self.rejected(429, lambda: self.store.issue("client-extra", "session"))
        self.now += 600
        self.store.issue("client-extra", "session")
        self.rejected(429, lambda: self.store.issue("client-another", "session"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM form_tokens"), 4)
        self.now += 1800
        self.assertTrue(bool(self.store.issue("client-another", "session")))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM form_tokens"), 1)

    def test_global_attempt_limit_bounds_distributed_invalid_tokens(self):
        self.store.GLOBAL_ATTEMPT_LIMIT = 3
        for index in range(3):
            self.rejected(403, lambda: self.submit(self.fields(token="invalid-token"), ip="client-" + str(index)))
        self.rejected(429, lambda: self.submit(None, ip="client-extra"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events"), 3)
        self.now += 600
        self.rejected(400, lambda: self.submit(None, ip="client-extra"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events"), 1)

    def test_concurrent_threaded_replay_is_atomic(self):
        fields = self.fields()
        self.now += 3
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait(timeout=10)
            try:
                self.submit(fields)
                return 201
            except RequestError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: worker(), range(8)))
        self.assertEqual(sorted(statuses), [201] + [409] * 7)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events WHERE kind='accepted'"), 1)

    def test_concurrent_distinct_forms_respect_accept_limit(self):
        forms = [self.fields() for _ in range(8)]
        self.now += 3
        barrier = threading.Barrier(8)

        def worker(fields):
            barrier.wait(timeout=10)
            try:
                self.submit(fields)
                return 201
            except RequestError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(worker, forms))
        self.assertEqual(sorted(statuses), [201] * 5 + [429] * 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 5)

    def test_concurrent_issuance_respects_limit(self):
        self.store.ISSUE_LIMIT = 5
        barrier = threading.Barrier(8)

        def worker(index):
            barrier.wait(timeout=10)
            try:
                self.store.issue("127.0.0.1", "session-" + str(index))
                return 200
            except RequestError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(worker, range(8)))
        self.assertEqual(sorted(statuses), [200] * 5 + [429] * 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM form_tokens"), 5)

    def test_concurrent_spawned_process_replay_is_atomic(self):
        fields = self.fields()
        self.now += 3
        ctx = multiprocessing.get_context("spawn")
        ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=_process_submit, args=(str(self.db), fields, self.now, ready, start, results)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                self.assertTrue(ready.get(timeout=15))
            start.set()
            statuses = [results.get(timeout=15) for _ in workers]
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(sorted(statuses), [201, 409])
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 1)
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                if worker.pid:
                    worker.join(timeout=5)
            ready.close()
            results.close()

    def test_expiry_clock_checked_after_write_lock(self):
        fields = self.fields()
        self.now += 3
        started, clock_called = threading.Event(), threading.Event()
        statuses = []

        def clock():
            clock_called.set()
            return self.now

        self.store.clock = clock
        blocker = sqlite3.connect(str(self.db), isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")

        def worker():
            started.set()
            try:
                self.submit(fields)
                statuses.append(201)
            except RequestError as exc:
                statuses.append(exc.status)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=5))
            self.assertFalse(clock_called.wait(timeout=.1), "Clock must be read only after acquiring write lock")
            self.now += 1800
        finally:
            blocker.commit()
            blocker.close()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(statuses, [403])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 0)

    def test_atomic_rollback_keeps_token_after_sqlite_failure(self):
        fields = self.fields()
        self.now += 3
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute("CREATE TRIGGER reject_accept BEFORE INSERT ON spam_events WHEN NEW.kind = 'accepted' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(fields)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM requests"), 0)
        self.assertEqual(self.scalar("SELECT consumed FROM form_tokens"), 0)
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute("DROP TRIGGER reject_accept")
        self.assertIsInstance(self.submit(fields), int)

    def test_private_directory_file_modes_and_outside_docroot(self):
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("site", self.db.parts)
        for path in (Path(self.tmp.name) / "site" / "requests.db",
                     Path(self.tmp.name) / "site" / "nested" / "requests.db"):
            with self.assertRaises(ValueError):
                RequestStore(path)
            self.assertFalse(path.exists())
        os.chmod(str(self.db.parent), 0o755)
        os.chmod(str(self.db), 0o644)
        RequestStore(self.db, clock=lambda: self.now)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db.parent.stat().st_mode & 0o777, 0o700)

    def test_symlinks_into_docroot_and_symlink_database_rejected(self):
        site = Path(self.tmp.name) / "site"
        site.mkdir()
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(site, target_is_directory=True)
        with self.assertRaises(ValueError):
            RequestStore(alias / "requests.db")
        linked = self.db.parent / "linked.db"
        linked.symlink_to(self.db)
        with self.assertRaises(ValueError):
            RequestStore(linked)
        hardlink = self.db.parent / "hardlink.db"
        os.link(str(self.db), str(hardlink))
        with self.assertRaises(ValueError):
            RequestStore(hardlink)

    def test_invalid_identity_does_not_reach_database(self):
        for ip, session in ((None, "session"), ("", "session"), ("x" * 129, "session"),
                            ("127.0.0.1", ""), ("127.0.0.1", "x" * 257),
                            ("127.0.0.1", "bad\x00session")):
            self.rejected(400, lambda: self.store.issue(ip, session))
            self.rejected(400, lambda: self.store.submit(ip, session, None))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM spam_events"), 0)


if __name__ == "__main__":
    unittest.main()
