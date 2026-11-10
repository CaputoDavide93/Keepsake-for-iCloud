"""Changing and resetting the page password.

The password starts as KEEPSAKE_PASSWORD. Once it is changed in Settings, or reset
with `python3 -m keepsake reset-password`, a salted hash in the data folder is the
password and the environment value no longer works; every other login is signed
out; and nothing the API returns, and nothing in a log, holds either password.
"""

import io
import json
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from keepsake import auth
from keepsake.auth import PasswordStore, check_record, make_record, reset_password_command
from keepsake.config import ConfigError
from keepsake.server import serve
from keepsake.service import Service
from tests.helpers import make_cfg
from tests.test_api import PASSWORD, ApiBase, Client

NEW = "a-brand-new-passphrase"
NEWER = "yet-another-passphrase"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class PasswordApi(ApiBase):
    password = PASSWORD

    def login(self, client=None, password=PASSWORD):
        client = client or self.c
        client.open()
        status, data, _ = client.call("POST", "/api/login", {"password": password})
        if status == 200:
            client.csrf = data["csrf"]
        return status

    def change(self, client=None, current=PASSWORD, new=NEW, confirm=None):
        client = client or self.c
        return client.call("POST", "/api/password", {
            "current": current, "new": new, "confirm": new if confirm is None else confirm})

    @property
    def stored_file(self):
        return self.tmp / "data" / auth.FILE_NAME

    def test_change_succeeds_and_the_new_password_logs_in(self):
        self.assertEqual(self.login(), 200)
        status, data, _ = self.change()
        self.assertEqual((status, data["ok"]), (200, True), data)
        self.assertEqual(self.c.call("GET", "/api/status")[0], 200, "this login stays")
        self.c.call("POST", "/api/logout")
        other = Client(self.port)
        self.assertEqual(self.login(other, NEW), 200)

    def test_the_old_environment_password_stops_working(self):
        self.login()
        self.assertEqual(self.change()[0], 200)
        fresh = Client(self.port)
        self.assertEqual(self.login(fresh, PASSWORD), 401)
        self.assertEqual(self.login(fresh, NEW), 200)
        bearer = {"Authorization": f"Bearer {PASSWORD}"}
        self.assertEqual(Client(self.port).call("GET", "/api/status", headers=bearer)[0], 401)
        bearer = {"Authorization": f"Bearer {NEW}"}
        self.assertEqual(Client(self.port).call("GET", "/api/status", headers=bearer)[0], 200)

    def test_a_wrong_current_password_is_refused_and_changes_nothing(self):
        self.login()
        status, data, _ = self.change(current="not-the-password")
        self.assertEqual(status, 403)
        self.assertEqual(data["message"], "the current password is not right")
        self.assertFalse(self.stored_file.exists())
        self.assertEqual(self.login(Client(self.port), PASSWORD), 200, "the old one still works")

    def test_wrong_current_passwords_are_rate_limited_like_login(self):
        self.login()
        codes = [self.change(current=f"guess-{i}")[0] for i in range(7)]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5], 429)
        self.assertEqual(self.change()[0], 429, "the right current password does not bypass it")

    def test_a_short_password_is_refused(self):
        self.login()
        status, data, _ = self.change(new="short")
        self.assertEqual(status, 400)
        self.assertIn("at least 10 characters", data["message"])
        self.assertFalse(self.stored_file.exists())

    def test_two_different_new_passwords_are_refused(self):
        self.login()
        status, data, _ = self.change(new=NEW, confirm=NEWER)
        self.assertEqual(status, 400)
        self.assertIn("not the same", data["message"])
        self.assertFalse(self.stored_file.exists())

    def test_the_same_password_again_is_refused(self):
        self.login()
        status, data, _ = self.change(new=PASSWORD)
        self.assertEqual(status, 400)
        self.assertIn("same as the current", data["message"])

    def test_other_sessions_are_signed_out_and_this_one_is_kept(self):
        mine, other = Client(self.port), Client(self.port)
        self.assertEqual(self.login(mine), 200)
        self.assertEqual(self.login(other), 200)
        self.assertEqual(other.call("GET", "/api/status")[0], 200)
        self.assertEqual(self.change(mine)[0], 200)
        self.assertEqual(other.call("GET", "/api/status")[0], 401, "the other browser is out")
        self.assertEqual(mine.call("GET", "/api/status")[0], 200, "this browser stays in")

    def test_a_change_needs_the_csrf_token_and_a_login(self):
        self.login()
        status, _, _ = self.c.call("POST", "/api/password", {
            "current": PASSWORD, "new": NEW, "confirm": NEW}, csrf=False)
        self.assertEqual(status, 403)
        stranger = Client(self.port)
        stranger.open()
        status, _, _ = stranger.call("POST", "/api/password", {
            "current": PASSWORD, "new": NEW, "confirm": NEW})
        self.assertEqual(status, 401)
        self.assertFalse(self.stored_file.exists())

    def test_the_stored_value_is_a_salted_hash_and_never_the_password(self):
        self.login()
        self.assertEqual(self.change()[0], 200)
        text = self.stored_file.read_text()
        record = json.loads(text)
        self.assertEqual(record["algo"], "scrypt")
        self.assertNotIn(NEW, text)
        self.assertNotIn(PASSWORD, text)
        self.assertTrue(check_record(record, NEW))
        self.assertFalse(check_record(record, PASSWORD))
        self.assertEqual(oct(self.stored_file.stat().st_mode & 0o777), "0o600")

    def test_nothing_the_api_returns_holds_the_hash(self):
        self.login()
        self.assertEqual(self.change()[0], 200)
        record = json.loads(self.stored_file.read_text())
        body = ""
        for path in ("/api/session", "/api/status", "/api/settings", "/api/activity",
                     "/api/diagnostics"):
            status, data, _ = self.c.call("GET", path)
            self.assertEqual(status, 200, path)
            body += data.decode() if isinstance(data, bytes) else json.dumps(data)
        for secret in (record["hash"], record["salt"], NEW, PASSWORD):
            self.assertNotIn(secret, body)

    def test_passwords_are_never_logged(self):
        handler = Capture()
        log = logging.getLogger("keepsake")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            self.login()
            self.change(current="wrong-current-guess")
            self.change()
        finally:
            log.removeHandler(handler)
        text = "\n".join(handler.lines)
        for secret in (PASSWORD, NEW, "wrong-current-guess"):
            self.assertNotIn(secret, text)

    def test_the_new_password_survives_a_restart(self):
        self.login()
        self.assertEqual(self.change()[0], 200)
        store = PasswordStore(self.tmp / "data", PASSWORD)
        self.assertTrue(store.verify(NEW))
        self.assertFalse(store.verify(PASSWORD))

    def test_reset_signs_everyone_out_and_sets_a_working_password(self):
        mine, other = Client(self.port), Client(self.port)
        self.assertEqual(self.login(mine), 200)
        self.assertEqual(self.login(other), 200)
        pw_file = self.tmp / "new.txt"
        pw_file.write_text(NEW + "\n")
        with mock.patch.dict(os.environ, {"KEEPSAKE_DATA": str(self.tmp / "data")}):
            out = io.StringIO()
            with redirect_stdout(out):
                code = reset_password_command(["--password-file", str(pw_file)])
        self.assertEqual(code, 0)
        self.assertNotIn(NEW, out.getvalue())
        self.assertEqual(mine.call("GET", "/api/status")[0], 401)
        self.assertEqual(other.call("GET", "/api/status")[0], 401)
        self.assertEqual(self.login(Client(self.port), PASSWORD), 401)
        self.assertEqual(self.login(Client(self.port), NEW), 200)

    def test_reset_works_without_a_password_ever_set_in_the_page(self):
        with mock.patch.dict(os.environ, {"KEEPSAKE_DATA": str(self.tmp / "data")}), \
                mock.patch("sys.stdin", io.StringIO(NEW + "\n")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(reset_password_command(["--stdin"]), 0)
        self.assertEqual(self.login(Client(self.port), NEW), 200)


class ResetCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"KEEPSAKE_DATA": str(self.tmp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def run_cmd(self, argv):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = reset_password_command(argv)
        return code, err.getvalue()

    def test_a_short_password_is_refused_and_nothing_is_written(self):
        pw = self.tmp / "p.txt"
        pw.write_text("short")
        code, err = self.run_cmd(["--password-file", str(pw)])
        self.assertEqual(code, 1)
        self.assertIn("at least 10 characters", err)
        self.assertFalse((self.tmp / auth.FILE_NAME).exists())

    def test_a_missing_file_is_a_plain_sentence(self):
        code, err = self.run_cmd(["--password-file", str(self.tmp / "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("nothing was changed", err)

    def test_it_will_not_prompt_without_a_terminal(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            code, err = self.run_cmd([])
        self.assertEqual(code, 2)
        self.assertIn("needs a terminal", err)

    def test_the_prompt_asks_twice_and_refuses_a_mismatch(self):
        answers = iter([NEW, NEWER])
        with mock.patch("sys.stdin.isatty", return_value=True, create=True), \
                mock.patch("getpass.getpass", side_effect=lambda *_: next(answers)):
            code, err = self.run_cmd([])
        self.assertEqual(code, 1)
        self.assertIn("not the same", err)
        self.assertFalse((self.tmp / auth.FILE_NAME).exists())

    def test_the_prompt_stores_a_hash_and_never_echoes_it(self):
        answers = iter([NEW, NEW])
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True, create=True), \
                mock.patch("getpass.getpass", side_effect=lambda *_: next(answers)), \
                redirect_stderr(err), redirect_stdout(out):
            code = reset_password_command([])
        self.assertEqual(code, 0)
        self.assertNotIn(NEW, out.getvalue() + err.getvalue())
        self.assertTrue(PasswordStore(self.tmp).verify(NEW))

    def test_unknown_arguments_print_the_usage(self):
        code, err = self.run_cmd(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)


class StoredPasswordCounts(unittest.TestCase):
    def test_a_stored_password_lets_a_reachable_address_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(make_cfg(Path(tmp)), host="0.0.0.0", port=0, password="")
            with self.assertRaises(ConfigError):
                serve(Service(cfg, []))
            PasswordStore(cfg.data_dir).set(NEW)
            httpd = serve(Service(cfg, []))
            httpd.server_close()

    def test_main_starts_with_only_a_stored_password(self):
        from keepsake.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            PasswordStore(Path(tmp) / "data").set(NEW)
            env = {"KEEPSAKE_DATA": str(Path(tmp) / "data"), "KEEPSAKE_HOST": "0.0.0.0",
                   "KEEPSAKE_PORT": "18765", "KEEPSAKE_ROOTS": f"s={tmp}",
                   "KEEPSAKE_ICLOUD_DIR": tmp}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("keepsake.__main__.serve",
                               side_effect=ConfigError("stop here")) as served:
                # It got past the password check and reached serve(), which we stop.
                self.assertEqual(main([]), 2)
                served.assert_called_once()
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("keepsake.__main__.stored_password_exists", return_value=False), \
                    self.assertLogs("keepsake", "ERROR") as logs:
                self.assertEqual(main([]), 2)
            self.assertIn("needs a password", "\n".join(logs.output))


class NoPasswordSet(ApiBase):
    def test_there_is_nothing_to_change_on_a_loopback_page_without_a_password(self):
        self.c.open()
        status, data, _ = self.c.call("POST", "/api/password", {
            "current": "", "new": NEW, "confirm": NEW})
        self.assertEqual(status, 400)
        self.assertIn("nothing to change", data["message"])


class Records(unittest.TestCase):
    def test_every_record_has_its_own_salt_and_epoch(self):
        a, b = make_record(NEW), make_record(NEW)
        self.assertNotEqual(a["salt"], b["salt"])
        self.assertNotEqual(a["hash"], b["hash"])
        self.assertNotEqual(a["epoch"], b["epoch"])

    def test_a_damaged_record_never_verifies(self):
        record = make_record(NEW)
        self.assertTrue(check_record(record, NEW))
        self.assertFalse(check_record({**record, "hash": "AAAA"}, NEW))
        self.assertFalse(check_record({**record, "algo": "md5"}, NEW))
        self.assertFalse(check_record({}, NEW))

    def test_the_password_length_limits(self):
        for bad in ("", "x" * 9, "x" * 257, None, 12345678901):
            with self.assertRaises(ConfigError):
                auth.check_new_password(bad, bad, confirming=True)


if __name__ == "__main__":
    unittest.main()
