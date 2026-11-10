"""Alert channels (ntfy, Pushover, e-mail), the test alert, and the "no
successful run" alert. Local fake servers and mocks only: nothing leaves the
machine."""

import json
import smtplib
import socketserver
import tempfile
import threading
import unittest
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from keepsake.config import Config, ConfigError
from keepsake.notifier import (
    EmailNotifier,
    NotifyError,
    NtfyNotifier,
    PushoverNotifier,
    build_notifiers,
    describe,
)
from keepsake.service import Service
from tests.helpers import make_cfg, write

TOKEN = "tok-SECRET-123"
USER = "user-SECRET-456"
PASSWORD = "pw-SECRET-789"
TOPIC = "/my-private-topic-xyz"


class Fake(BaseHTTPRequestHandler):
    got = []
    status = 200
    reply = b"{}"

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Fake.got.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(Fake.status)
        self.send_header("Content-Length", str(len(Fake.reply)))
        self.end_headers()
        self.wfile.write(Fake.reply)

    def log_message(self, *a):
        pass


class HttpBase(unittest.TestCase):
    def setUp(self):
        Fake.got, Fake.status, Fake.reply = [], 200, b"{}"
        self.httpd = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()


FAILED = {"event": "failed", "job": "photos", "message": "disk said no",
          "at": "2026-11-07T03:00:00+00:00"}


class NtfyTests(HttpBase):
    def test_posts_the_text_to_the_topic_with_title_and_priority(self):
        self.assertTrue(NtfyNotifier(self.base + TOPIC).send(FAILED))
        got = Fake.got[0]
        self.assertEqual(got["path"], TOPIC)
        self.assertEqual(got["headers"]["Title"], "Keepsake: photos needs attention")
        self.assertEqual(got["headers"]["Priority"], "high")
        self.assertIn(b"disk said no", got["body"])
        self.assertNotIn("Authorization", got["headers"])

    def test_a_token_is_sent_as_a_bearer_header(self):
        NtfyNotifier(self.base + TOPIC, TOKEN).send(FAILED)
        self.assertEqual(Fake.got[0]["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_a_recovery_is_not_urgent(self):
        NtfyNotifier(self.base + TOPIC).send({"event": "recovered", "job": "photos",
                                              "message": "ok"})
        self.assertEqual(Fake.got[0]["headers"]["Priority"], "default")

    def test_a_refusal_is_a_failure_with_a_plain_error_and_no_address(self):
        Fake.status = 403
        ok, error = NtfyNotifier(self.base + TOPIC, TOKEN).test(FAILED)
        self.assertFalse(ok)
        self.assertIn("403", error)
        self.assertNotIn("my-private-topic", error)
        self.assertNotIn(TOKEN, error)

    def test_an_unreachable_server_never_raises_and_hides_the_address(self):
        n = NtfyNotifier("http://127.0.0.1:9" + TOPIC, timeout=1)
        self.assertFalse(n.send(FAILED))
        ok, error = n.test(FAILED)
        self.assertFalse(ok)
        self.assertNotIn("my-private-topic", error)

    def test_a_redirect_is_not_followed(self):
        Fake.status = 302
        self.assertFalse(NtfyNotifier(self.base + TOPIC).send(FAILED))
        self.assertEqual(len(Fake.got), 1)

    def test_the_label_is_the_host_only(self):
        self.assertEqual(NtfyNotifier("https://ntfy.example.org/secret-topic").label,
                         "ntfy.example.org")


class PushoverTests(HttpBase):
    def make(self):
        return PushoverNotifier(TOKEN, USER, api_url=self.base + "/1/messages.json")

    def test_posts_a_form_with_token_user_title_message_priority(self):
        self.assertTrue(self.make().send(FAILED))
        form = urllib.parse.parse_qs(Fake.got[0]["body"].decode())
        self.assertEqual((form["token"], form["user"]), ([TOKEN], [USER]))
        self.assertEqual(form["title"], ["Keepsake: photos needs attention"])
        self.assertIn("disk said no", form["message"][0])
        self.assertEqual(form["priority"], ["1"])

    def test_pushovers_own_explanation_is_shown_without_the_token(self):
        Fake.status = 400
        Fake.reply = json.dumps({"errors": [f"application token is invalid {TOKEN}"],
                                 "status": 0}).encode()
        ok, error = self.make().test(FAILED)
        self.assertFalse(ok)
        self.assertIn("400", error)
        self.assertIn("application token is invalid", error)
        self.assertNotIn(TOKEN, error)

    def test_the_real_api_address_is_pushovers(self):
        from keepsake.notifier import PUSHOVER_URL
        self.assertEqual(PUSHOVER_URL, "https://api.pushover.net/1/messages.json")


class SmtpServer(socketserver.StreamRequestHandler):
    """A tiny SMTP server that accepts one message without TLS or login."""
    messages = []

    def handle(self):
        send = lambda line: self.wfile.write(line.encode() + b"\r\n")  # noqa: E731
        send("220 fake ESMTP")
        data_mode, lines, rcpt = False, [], []
        for raw in self.rfile:
            line = raw.decode().rstrip("\r\n")
            if data_mode:
                if line == ".":
                    SmtpServer.messages.append(("\n".join(lines), rcpt))
                    data_mode = False
                    send("250 queued")
                else:
                    lines.append(line)
                continue
            cmd = line.split(" ")[0].upper()
            if cmd in ("EHLO", "HELO"):
                send("250 fake")
            elif cmd == "MAIL":
                send("250 ok")
            elif cmd == "RCPT":
                rcpt.append(line.split(":", 1)[1].strip(" <>"))
                send("250 ok")
            elif cmd == "DATA":
                data_mode = True
                send("354 go")
            elif cmd == "QUIT":
                send("221 bye")
                return
            else:
                send("250 ok")


def smtp_cfg(**kw):
    base = dict(data_dir=Path("/x"), dest_root=Path("/y"), roots={}, smtp_host="mail.test",
                smtp_from="keepsake@example.test", smtp_to=("me@example.test", "you@example.test"),
                smtp_security="none", smtp_port=2525)
    base.update(kw)
    return Config(**base)


class EmailTests(unittest.TestCase):
    def test_a_message_reaches_a_local_server_with_subject_and_recipients(self):
        SmtpServer.messages = []
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SmtpServer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            n = EmailNotifier(smtp_cfg(smtp_host="127.0.0.1", smtp_port=srv.server_address[1]))
            self.assertEqual(n.test(FAILED), (True, ""))
        finally:
            srv.shutdown()
            srv.server_close()
        text, rcpt = SmtpServer.messages[0]
        self.assertIn("Subject: Keepsake: photos needs attention", text)
        self.assertIn("disk said no", text)
        self.assertEqual(rcpt, ["me@example.test", "you@example.test"])

    def fake_smtp(self, **attrs):
        server = mock.MagicMock()
        for k, v in attrs.items():
            getattr(server, k).side_effect = v
        return server

    def test_starttls_comes_before_the_login_and_the_login_before_the_message(self):
        server = self.fake_smtp()
        cfg = smtp_cfg(smtp_security="starttls", smtp_user="u", smtp_password=PASSWORD,
                       smtp_port=587)
        with mock.patch.object(smtplib, "SMTP", return_value=server) as smtp:
            self.assertTrue(EmailNotifier(cfg).send(FAILED))
        self.assertEqual(smtp.call_args.args[:2], ("mail.test", 587))
        names = [c[0] for c in server.method_calls]
        self.assertEqual([n for n in names if n in ("starttls", "login", "send_message", "quit")],
                         ["starttls", "login", "send_message", "quit"])
        server.login.assert_called_with("u", PASSWORD)

    def test_ssl_uses_a_tls_connection_from_the_start(self):
        server = self.fake_smtp()
        cfg = smtp_cfg(smtp_security="ssl", smtp_user="u", smtp_password=PASSWORD, smtp_port=465)
        with mock.patch.object(smtplib, "SMTP_SSL", return_value=server) as ssl_cls, \
                mock.patch.object(smtplib, "SMTP") as plain:
            self.assertTrue(EmailNotifier(cfg).send(FAILED))
        ssl_cls.assert_called_once()
        plain.assert_not_called()
        server.starttls.assert_not_called()

    def test_no_login_means_none_is_attempted(self):
        server = self.fake_smtp()
        with mock.patch.object(smtplib, "SMTP", return_value=server):
            EmailNotifier(smtp_cfg(smtp_security="starttls")).send(FAILED)
        server.login.assert_not_called()

    def test_a_refused_login_is_a_plain_error_without_the_password(self):
        server = self.fake_smtp(login=smtplib.SMTPAuthenticationError(535, PASSWORD.encode()))
        cfg = smtp_cfg(smtp_security="starttls", smtp_user="u", smtp_password=PASSWORD)
        with mock.patch.object(smtplib, "SMTP", return_value=server):
            ok, error = EmailNotifier(cfg).test(FAILED)
        self.assertFalse(ok)
        self.assertIn("refused the user name or password", error)
        self.assertNotIn(PASSWORD, error)
        server.send_message.assert_not_called()

    def test_an_unreachable_server_is_a_plain_error_and_never_raises(self):
        cfg = smtp_cfg(smtp_host="127.0.0.1", smtp_port=9, smtp_security="none")
        n = EmailNotifier(cfg, timeout=1)
        self.assertFalse(n.send(FAILED))
        ok, error = n.test(FAILED)
        self.assertFalse(ok)
        self.assertIn("could not reach the mail server", error)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **kw):
        return {"KEEPSAKE_DATA": str(self.tmp), **kw}

    def test_everything_is_off_unless_set(self):
        cfg = Config.from_env(self.env())
        self.assertEqual(build_notifiers(cfg), [])

    def test_all_channels_from_the_environment(self):
        cfg = Config.from_env(self.env(
            KEEPSAKE_NTFY_URL="https://ntfy.example.org/t", KEEPSAKE_NTFY_TOKEN="tk",
            KEEPSAKE_PUSHOVER_TOKEN="a", KEEPSAKE_PUSHOVER_USER="b",
            KEEPSAKE_SMTP_HOST="mail.example.org", KEEPSAKE_SMTP_FROM="k@example.test",
            KEEPSAKE_SMTP_TO="a@example.test, b@example.test", KEEPSAKE_SMTP_USER="u",
            KEEPSAKE_SMTP_PASSWORD="p"))
        self.assertEqual([n.name for n in build_notifiers(cfg)], ["ntfy", "pushover", "email"])
        self.assertEqual(cfg.smtp_to, ("a@example.test", "b@example.test"))
        self.assertEqual((cfg.smtp_port, cfg.smtp_security), (587, "starttls"))

    def test_secrets_can_come_from_files(self):
        for name, text in (("ntfy", "https://ntfy.example.org/t\n"), ("tok", "T\n"),
                           ("user", "U\n"), ("pw", "P\n")):
            (self.tmp / name).write_text(text)
        cfg = Config.from_env(self.env(
            KEEPSAKE_NTFY_URL_FILE=str(self.tmp / "ntfy"),
            KEEPSAKE_PUSHOVER_TOKEN_FILE=str(self.tmp / "tok"),
            KEEPSAKE_PUSHOVER_USER_FILE=str(self.tmp / "user"),
            KEEPSAKE_SMTP_HOST="m.example.org", KEEPSAKE_SMTP_FROM="k@example.test",
            KEEPSAKE_SMTP_TO="a@example.test", KEEPSAKE_SMTP_USER="u",
            KEEPSAKE_SMTP_PASSWORD_FILE=str(self.tmp / "pw")))
        self.assertEqual((cfg.ntfy_url, cfg.pushover_token, cfg.pushover_user, cfg.smtp_password),
                         ("https://ntfy.example.org/t", "T", "U", "P"))

    def test_a_missing_secret_file_names_the_variable_not_a_value(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env(self.env(KEEPSAKE_NTFY_URL_FILE=str(self.tmp / "nope")))
        self.assertIn("KEEPSAKE_NTFY_URL_FILE", str(ctx.exception))

    def test_half_set_channels_are_refused_in_plain_words(self):
        for env, word in (
            ({"KEEPSAKE_PUSHOVER_TOKEN": "a"}, "both"),
            ({"KEEPSAKE_SMTP_HOST": "m.example.org"}, "KEEPSAKE_SMTP_FROM"),
            ({"KEEPSAKE_NTFY_URL": "ntfy.example.org/t"}, "http"),
            ({"KEEPSAKE_SMTP_HOST": "m", "KEEPSAKE_SMTP_FROM": "a@b.c",
              "KEEPSAKE_SMTP_TO": "d@e.f", "KEEPSAKE_SMTP_USER": "u"}, "KEEPSAKE_SMTP_PASSWORD"),
            ({"KEEPSAKE_SMTP_HOST": "m", "KEEPSAKE_SMTP_FROM": "not an address",
              "KEEPSAKE_SMTP_TO": "d@e.f"}, "plain e-mail"),
            ({"KEEPSAKE_SMTP_HOST": "m", "KEEPSAKE_SMTP_FROM": "a@b.c", "KEEPSAKE_SMTP_TO": "d@e.f",
              "KEEPSAKE_SMTP_SECURITY": "tls13"}, "starttls"),
        ):
            with self.assertRaises(ConfigError, msg=env) as ctx:
                Config.from_env(self.env(**env))
            self.assertIn(word, str(ctx.exception))

    def test_a_password_is_never_sent_without_encryption(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_env(self.env(
                KEEPSAKE_SMTP_HOST="m", KEEPSAKE_SMTP_FROM="a@b.c", KEEPSAKE_SMTP_TO="d@e.f",
                KEEPSAKE_SMTP_USER="u", KEEPSAKE_SMTP_PASSWORD="p", KEEPSAKE_SMTP_SECURITY="none"))
        self.assertIn("without encryption", str(ctx.exception))

    def test_secrets_are_not_in_the_repr(self):
        cfg = Config.from_env(self.env(
            KEEPSAKE_NTFY_URL="https://ntfy.example.org/the-topic", KEEPSAKE_NTFY_TOKEN=TOKEN,
            KEEPSAKE_PUSHOVER_TOKEN=TOKEN, KEEPSAKE_PUSHOVER_USER=USER,
            KEEPSAKE_SMTP_HOST="m", KEEPSAKE_SMTP_FROM="a@b.c", KEEPSAKE_SMTP_TO="d@e.f",
            KEEPSAKE_SMTP_USER="u", KEEPSAKE_SMTP_PASSWORD=PASSWORD))
        text = repr(cfg)
        for secret in (TOKEN, USER, PASSWORD, "the-topic"):
            self.assertNotIn(secret, text)

    def test_stale_is_a_notify_event_and_default(self):
        self.assertIn("stale", Config.from_env(self.env()).notify_on)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Rec:
    name = "rec"

    def __init__(self, ok=True, error=""):
        self.events, self.ok, self.error = [], ok, error

    def send(self, event):
        self.events.append(event)
        return self.ok

    def test(self, event):
        self.events.append(event)
        return self.ok, self.error


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.rec = Rec()
        self.clock = Clock()
        self.svc = Service(self.cfg, [self.rec], self.clock)
        write(self.tmp / "sources/Photos/a.jpg", "a")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, **kw):
        self.svc.add_job({"name": "photos", "root": "sources", "source": "Photos", **kw})

    def events(self, kind=None):
        return [e for e in self.rec.events if kind is None or e["event"] == kind]


class TestAlertTests(ServiceBase):
    def test_nothing_set_up_is_a_refusal(self):
        svc = Service(self.cfg, [])
        with self.assertRaises(ConfigError) as ctx:
            svc.send_test_alert()
        self.assertIn("no alert channel", str(ctx.exception))

    def test_reports_each_channel_honestly(self):
        good, bad = Rec(), Rec(False, "the server answered 403")
        good.name, bad.name = "ntfy", "pushover"
        svc = Service(self.cfg, [good, bad], self.clock)
        out = svc.send_test_alert()
        self.assertFalse(out["ok"])
        self.assertEqual(out["results"], [
            {"channel": "ntfy", "ok": True, "error": ""},
            {"channel": "pushover", "ok": False, "error": "the server answered 403"}])
        self.assertIn("delivered to ntfy", out["message"])
        self.assertIn("pushover failed: the server answered 403", out["message"])
        self.assertEqual(good.events[0]["event"], "test")

    def test_all_good_is_ok(self):
        out = self.svc.send_test_alert()
        self.assertTrue(out["ok"])
        self.assertEqual(out["message"], "Test alert delivered to rec")

    def test_a_notifier_that_raises_is_a_failed_channel_not_a_crash(self):
        class Boom:
            name = "boom"

            def test(self, event):
                raise RuntimeError(f"secret {TOKEN}")
        out = Service(self.cfg, [Boom()], self.clock).send_test_alert()
        self.assertFalse(out["ok"])
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_settings_show_only_whether_a_channel_is_set_never_the_secret(self):
        cfg = replace(self.cfg, ntfy_url=f"https://ntfy.example.org{TOPIC}", ntfy_token=TOKEN,
                      pushover_token=TOKEN, pushover_user=USER, smtp_host="mail.example.org",
                      smtp_from="a@example.test", smtp_to=("b@example.test",),
                      smtp_password=PASSWORD, smtp_user="u", webhook_url="https://h.example/tok")
        view = Service(cfg, []).settings_view()
        text = json.dumps(view)
        for secret in (TOKEN, USER, PASSWORD, "my-private-topic", "/tok"):
            self.assertNotIn(secret, text)
        self.assertEqual({c["name"]: c["configured"] for c in view["channels"]},
                         {"webhook": True, "ntfy": True, "pushover": True, "email": True,
                          "mqtt": False})

    def test_the_activity_log_keeps_counts_not_errors(self):
        bad = Rec(False, f"oops {TOKEN}")
        Service(self.cfg, [bad], self.clock).send_test_alert()
        log = (self.cfg.activity_file).read_text()
        self.assertIn("0 of 1 channels worked", log)
        self.assertNotIn(TOKEN, log)


class StaleTests(ServiceBase):
    def ran(self):
        self.add(schedule={"every_hours": 24})
        self.svc.run_next()
        self.rec.events.clear()

    def later(self, **kw):
        self.clock.now += timedelta(**kw)

    def test_alerts_once_after_the_limit_and_not_again(self):
        self.ran()
        self.later(days=2, hours=23)
        self.svc.check_stale()
        self.assertEqual(self.events("stale"), [])
        self.later(hours=2)
        self.svc.check_stale()
        self.svc.check_stale()
        self.later(days=1)
        self.svc.check_stale()
        found = self.events("stale")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["job"], "photos")
        self.assertIn("no successful run for 3 days", found[0]["message"])

    def test_it_is_not_repeated_after_a_restart(self):
        self.ran()
        self.later(days=4)
        self.svc.check_stale()
        again = Service(self.cfg, [self.rec], self.clock)
        again.check_stale()
        self.assertEqual(len(self.events("stale")), 1)

    def test_a_run_that_heals_it_sends_one_recovery_and_arms_the_alert_again(self):
        self.ran()
        self.later(days=4)
        self.svc.check_stale()
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual([e["event"] for e in self.rec.events], ["stale", "recovered"])
        self.assertIn("after no success since", self.events("recovered")[0]["message"])
        self.svc.check_stale()
        self.assertEqual(len(self.events("stale")), 1, "healthy again: nothing more")
        self.later(days=4)
        self.svc.check_stale()
        self.assertEqual(len(self.events("stale")), 2, "a new outage is a new alert")

    def test_no_recovery_message_for_a_folder_that_never_alerted(self):
        self.ran()
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual(self.events("recovered"), [])

    def test_a_failure_and_a_stale_spell_heal_with_one_message(self):
        self.ran()
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            p.unlink()
        self.later(hours=25)
        self.svc.run_next()                     # fails: empty source
        self.later(days=4)
        self.svc.check_stale()
        write(self.tmp / "sources/Photos/a.jpg", "a")
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual([e["event"] for e in self.rec.events],
                         ["failed", "stale", "recovered"])

    def test_the_limit_never_drops_below_twice_the_folders_own_period(self):
        self.add(schedule={"every_hours": 168})
        self.svc.run_next()
        self.rec.events.clear()
        self.later(days=6)                      # past 3 days, under 2 x 7
        self.svc.check_stale()
        self.assertEqual(self.events("stale"), [])
        self.later(days=9)
        self.svc.check_stale()
        self.assertEqual(len(self.events("stale")), 1)

    def test_zero_turns_it_off_and_a_paused_folder_is_never_stale(self):
        self.add(stale_days=0)
        self.svc.run_next()
        self.rec.events.clear()
        self.later(days=30)
        self.svc.check_stale()
        self.assertEqual(self.events("stale"), [])
        self.svc.update_job("photos", {"stale_days": 3, "enabled": False})
        self.svc.check_stale()
        self.assertEqual(self.events("stale"), [])

    def test_a_folder_that_never_succeeded_counts_from_when_it_was_added(self):
        self.add()
        self.later(days=1)
        self.svc.check_stale()
        self.later(days=3)
        self.svc.check_stale()
        found = self.events("stale")
        self.assertEqual(len(found), 1)
        self.assertIn("since the folder was added", found[0]["message"])

    def test_the_stale_event_can_be_switched_off(self):
        self.ran()
        self.svc.update_settings({"notify_on": ["failed", "recovered"]})
        self.later(days=4)
        self.svc.check_stale()
        self.assertEqual(self.events("stale"), [])
        self.assertIn("no successful run", self.svc.activity()[0]["message"])

    def test_stale_days_is_validated(self):
        for bad in (-1, 366, "3", 2.5, True):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self.add(stale_days=bad)


class DescribeTests(unittest.TestCase):
    def test_titles_are_ascii_and_say_what_happened(self):
        self.assertEqual(describe({"event": "stale", "job": "docs", "message": "m"})[0],
                         "Keepsake: docs has not backed up")
        title, body, urgent = describe({"event": "test", "message": "hi", "at": "t"})
        self.assertEqual((title, urgent), ("Keepsake: test alert", False))
        self.assertEqual(body, "hi\nt")

    def test_notify_error_is_a_plain_exception(self):
        self.assertEqual(str(NotifyError("x")), "x")


if __name__ == "__main__":
    unittest.main()
