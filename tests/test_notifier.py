import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from keepsake.config import Config
from keepsake.notifier import MqttNotifier, WebhookNotifier, build_notifiers


class Hook(BaseHTTPRequestHandler):
    got = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Hook.got.append((self.path, json.loads(body), self.headers["Content-Type"]))
        self.send_response(Hook.status)
        self.send_header("Location", "http://127.0.0.1:9/never")
        self.end_headers()

    def log_message(self, *a):
        pass


class WebhookTests(unittest.TestCase):
    def setUp(self):
        Hook.got, Hook.status = [], 200
        self.httpd = HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/hook"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_posts_the_event_as_json(self):
        self.assertTrue(WebhookNotifier(self.url).send({"event": "failed", "job": "x"}))
        path, body, ctype = Hook.got[0]
        self.assertEqual((path, body["job"], ctype), ("/hook", "x", "application/json"))

    def test_a_redirect_is_not_followed_and_is_a_failure(self):
        Hook.status = 302
        self.assertFalse(WebhookNotifier(self.url).send({"event": "failed"}))
        self.assertEqual(len(Hook.got), 1)

    def test_an_unreachable_hook_returns_false_and_never_raises(self):
        self.assertFalse(WebhookNotifier("http://127.0.0.1:9/x", timeout=1).send({}))

    def test_the_label_hides_the_path_that_may_hold_a_token(self):
        self.assertEqual(WebhookNotifier("https://h.example/tok/en").label, "h.example")


class FakeClient:
    def __init__(self):
        self.calls = []

    def connect(self, *a):
        self.calls.append(("connect", a))

    def publish(self, topic, payload, retain=False):
        self.calls.append(("publish", topic, retain))

    def disconnect(self):
        self.calls.append(("disconnect",))


class MqttTests(unittest.TestCase):
    def cfg(self, **kw):
        return Config(data_dir=Path("/x"), dest_root=Path("/y"), roots={}, **kw)

    def test_publishes_event_and_retained_job_status(self):
        client = FakeClient()
        n = MqttNotifier(self.cfg(mqtt_host="broker.test"), lambda: client)
        self.assertTrue(n.send({"event": "failed", "job": "docs"}))
        topics = [c[1:] for c in client.calls if c[0] == "publish"]
        self.assertIn(("keepsake/event", False), topics)
        self.assertIn(("keepsake/job/docs", True), topics)

    def test_an_unreachable_broker_fails_quietly_with_a_sentence(self):
        n = MqttNotifier(self.cfg(mqtt_host="127.0.0.1", mqtt_port=1))
        self.assertFalse(n.send({"event": "x"}))
        ok, error = n.test({"event": "test"})
        self.assertFalse(ok)
        self.assertIn("broker", error)

    def test_nothing_is_built_when_nothing_is_configured(self):
        self.assertEqual(build_notifiers(self.cfg()), [])
        self.assertEqual(len(build_notifiers(self.cfg(webhook_url="https://h.example/x"))), 1)


if __name__ == "__main__":
    unittest.main()
