"""The diagnostics report: complete enough to debug with, safe enough to post.

Every secret field is set to a canary string; none may appear in the report, in
the endpoint's answer, or after the log lines are redacted.
"""

import logging
import re
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

from keepsake import diagnostics
from keepsake.service import Service
from tests.helpers import make_cfg, make_job, write
from tests.test_api import PASSWORD, ApiBase, Client

HOME = "/home/canary-person"
CANARIES = {
    "password": "pw-CANARY-1111",
    "webhook_url": "https://hooks.canary-host.test/services/WEBHOOK-CANARY-2222",
    "mqtt_password": "mqtt-CANARY-3333",
    "ntfy_url": "https://ntfy.canary-host.test/topic-CANARY-4444",
    "ntfy_token": "ntfy-CANARY-5555",
    "pushover_token": "pushover-CANARY-6666",
    "pushover_user": "pushuser-CANARY-7777",
    "smtp_password": "smtp-CANARY-8888",
}
OTHER_CANARIES = ["mqtt-user-CANARY-9999", "canary-mail@canary-host.test",
                  "smtp-user-CANARY-0000"]


def canary_cfg(tmp):
    return replace(make_cfg(tmp), mqtt_host="mqtt.canary-host.test",
                   mqtt_user=OTHER_CANARIES[0], smtp_host="smtp.canary-host.test",
                   smtp_user=OTHER_CANARIES[2], smtp_from=OTHER_CANARIES[1],
                   smtp_to=(OTHER_CANARIES[1],), **CANARIES)


class Report(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        write(self.tmp / "sources/Photos/secret-holiday-name.jpg", "a")
        self.svc = Service(canary_cfg(self.tmp), [])
        self.svc.store.save([make_job()])
        self.svc.sync_now("photos")
        self.svc.run_next()

    def test_every_secret_field_is_covered_by_the_canaries(self):
        marked = {f.name for f in fields(self.svc.cfg) if f.repr is False}
        self.assertEqual(sorted(marked - set(CANARIES)), [],
                         "a new secret field needs a canary here")

    def test_no_canary_no_full_url_no_home_path_no_file_name(self):
        secret_lines = [
            f"alert to {CANARIES['webhook_url']} failed",
            "GET https://user:hunter2@example.test:8443/a/b?token=abc123&x=1 done",
            "request headers Authorization: Bearer abcdef123456 sent",
            "retry with password=hunter2 and api_key: k-123456",
            f"[photos] could not copy secret-holiday-name.jpg: [Errno 13] {HOME}/Photos/x",
            f"opened {HOME}/Library/Keepsake/state.json",
            f"{CANARIES['smtp_password']} and {CANARIES['mqtt_password']} leaked",
        ]
        self.svc.state.setdefault("photos", {})["error"] = f"cannot read {HOME}/Photos"
        text = diagnostics.build_report(self.svc, secret_lines, home=HOME)
        for value in [*CANARIES.values(), *OTHER_CANARIES, "hunter2", "abc123",
                      "abcdef123456", "k-123456", "secret-holiday-name", "WEBHOOK-CANARY"]:
            self.assertNotIn(value, text)
        self.assertNotIn(HOME, text)
        self.assertIn("~/Library/Keepsake/state.json", text)
        self.assertIsNone(re.search(r"://[^\s/]+/", text), "a URL kept its path")
        self.assertIn("https://example.test:8443", text)

    def test_the_real_home_directory_becomes_a_tilde(self):
        home = str(Path.home())
        text = diagnostics.build_report(self.svc, [f"wrote {home}/Documents/x"])
        self.assertNotIn(home + "/", text)
        self.assertIn("~/Documents/x", text)
        self.assertEqual(diagnostics.redact(f"{home}x", self.svc.cfg), f"{home}x",
                         "a longer name that starts like the home is not the home")

    def test_the_report_says_what_a_bug_report_needs(self):
        text = diagnostics.build_report(self.svc, [])
        for want in ("Keepsake: ", "Python: ", "Platform: ", "Uptime: ",
                     "webhook: configured", "pushover: configured", "email: configured",
                     "paused: False", "quiet hours: off", "[photos]", "outcome: ok", "added 1",
                     "last self-check",
                     "iCloud state", "== Recent log (last 0 lines"):
            self.assertIn(want, text)

    def test_an_unconfigured_channel_says_not_set(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        svc = Service(make_cfg(Path(other.name)), [])
        text = diagnostics.build_report(svc, [])
        for name in ("webhook", "ntfy", "pushover", "email", "mqtt"):
            self.assertIn(f"{name}: not set", text)
        self.assertIn("(none)", text)

    def test_a_traceback_leaves_only_its_type(self):
        handler = diagnostics.RingHandler()
        log = logging.getLogger("keepsake.test-traceback")
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        try:
            raise OSError("no such file: /sources/Photos/private-name.jpg")
        except OSError:
            log.exception("worker error")
        joined = "\n".join(handler.lines())
        self.assertIn("worker error [OSError]", joined)
        self.assertNotIn("private-name", joined)
        self.assertNotIn("Traceback", joined)


class RingBuffer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.svc = Service(make_cfg(Path(self._tmp.name)), [])

    def test_it_is_bounded_and_keeps_the_newest_lines(self):
        handler = diagnostics.RingHandler(capacity=5)
        log = logging.getLogger("keepsake.test-ring")
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        for i in range(50):
            log.info("line %d", i)
        lines = handler.lines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[-1].endswith("line 49"))
        self.assertTrue(lines[0].endswith("line 45"))

    def test_the_default_ring_holds_at_most_the_documented_number(self):
        self.assertEqual(diagnostics.RING.buffer.maxlen, diagnostics.LOG_LINES)
        text = diagnostics.build_report(
            self.svc, [f"l{i}" for i in range(diagnostics.LOG_LINES + 50)])
        self.assertEqual(text.count("\nl"), diagnostics.LOG_LINES)

    def test_a_very_long_line_is_cut(self):
        handler = diagnostics.RingHandler()
        log = logging.getLogger("keepsake.test-long")
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        log.info("x" * 5000)
        self.assertLessEqual(len(handler.lines()[0]), diagnostics.LOG_LINE_MAX)

    def test_installing_twice_adds_one_handler(self):
        log = logging.getLogger("keepsake.test-install")
        handler = diagnostics.RingHandler()
        diagnostics.install_ring(log, handler)
        diagnostics.install_ring(log, handler)
        self.addCleanup(log.removeHandler, handler)
        self.assertEqual(log.handlers.count(handler), 1)


class EndpointNoPassword(ApiBase):
    def test_it_downloads_a_text_file_with_the_live_log_redacted(self):
        diagnostics.install_ring()
        self.addCleanup(logging.getLogger().removeHandler, diagnostics.RING)
        logging.getLogger("keepsake").setLevel(logging.INFO)
        logging.getLogger("keepsake").info("sent to https://hooks.example.test/T0KEN-path?token=zzz")
        self.c.open()
        status, body, headers = self.c.call("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"],
                         'attachment; filename="keepsake-diagnostics.txt"')
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        text = body.decode()
        self.assertIn("Keepsake diagnostics", text)
        self.assertIn("sent to https://hooks.example.test", text)
        self.assertNotIn("T0KEN-path", text)
        self.assertNotIn("zzz", text)

    def test_post_is_not_a_route(self):
        self.c.open()
        self.assertEqual(self.c.call("POST", "/api/diagnostics", {})[0], 404)


class EndpointWithPassword(ApiBase):
    password = PASSWORD

    def test_it_needs_a_login_and_the_text_never_holds_the_password(self):
        self.c.open()
        self.assertEqual(self.c.call("GET", "/api/diagnostics")[0], 401)
        self.assertEqual(self.c.call("GET", "/api/diagnostics",
                                     headers={"Authorization": "Bearer wrong"})[0], 401)
        self.c.call("POST", "/api/login", {"password": PASSWORD})
        status, body, _ = self.c.call("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertNotIn(PASSWORD, body.decode())

    def test_the_password_as_a_bearer_token_also_works(self):
        status, body, _ = bearer_client(self.port).call(
            "GET", "/api/diagnostics", headers={"Authorization": f"Bearer {PASSWORD}"})
        self.assertEqual(status, 200)
        self.assertNotIn(PASSWORD, body.decode())


def bearer_client(port):
    client = Client(port)
    client.open()
    return client


if __name__ == "__main__":
    unittest.main()
