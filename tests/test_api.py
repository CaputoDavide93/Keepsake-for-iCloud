import http.client
import json
import logging
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from keepsake.__main__ import main
from keepsake.config import ConfigError
from keepsake.server import serve
from keepsake.service import Service
from tests.helpers import make_cfg, write

PASSWORD = "correct-horse-battery"


class Client:
    """A tiny browser: keeps cookies, sends the CSRF header like the page does."""

    def __init__(self, port, host=None):
        self.port = port
        self.host = host or f"127.0.0.1:{port}"
        self.cookie = None
        self.csrf = ""

    def call(self, method, path, body=None, headers=None, csrf=True, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host}
        if self.cookie:
            h["Cookie"] = self.cookie
        if method != "GET":
            h["Content-Type"] = "application/json"
            if csrf:
                h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body or {}) if method != "GET" else None)
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        text = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        out = (resp.status, payload, dict(resp.getheaders()))
        conn.close()
        return out

    def open(self):
        status, data, _ = self.call("GET", "/api/session")
        self.csrf = data["csrf"]
        return data


class ApiBase(unittest.TestCase):
    password = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        cfg = replace(make_cfg(self.tmp), port=0, password=self.password,
                      host="127.0.0.1")
        write(self.tmp / "sources/Photos/2025/a.jpg", "a")
        write(self.tmp / "sources/Docs/b.txt", "b")
        self.svc = Service(cfg, [])
        self.httpd = serve(self.svc)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.c = Client(self.port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()


class LoopbackNoPassword(ApiBase):
    def test_session_status_and_security_headers(self):
        info = self.c.open()
        self.assertFalse(info["auth_required"])
        self.assertTrue(info["authenticated"])
        status, data, headers = self.c.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["roots"][0]["name"], "sources")
        for name in ("Content-Security-Policy", "X-Content-Type-Options",
                     "X-Frame-Options", "Referrer-Policy", "Cache-Control"):
            self.assertIn(name, headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Python", headers.get("Server", ""))

    def test_jobs_carry_a_bounded_run_history(self):
        self.c.open()
        self.c.call("POST", "/api/jobs", {"name": "photos", "root": "sources", "source": "Photos"})
        self.svc.run_next()
        _, data, _ = self.c.call("GET", "/api/status")
        today = data["today"]
        self.assertEqual([(e["day"], e["outcome"]) for e in data["jobs"][0]["history"]], [(today, "copied")])
        _, one, _ = self.c.call("GET", "/api/jobs/photos")
        self.assertEqual([(e["day"], e["outcome"]) for e in one["history"]], [(today, "copied")])

    def test_static_page_is_served_with_the_csp(self):
        status, body, headers = self.c.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Keepsake", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"http://", body.replace(b"http://www.w3.org", b""))
        self.assertNotIn(b"https://", body)

    def test_state_changing_calls_need_the_csrf_token(self):
        self.c.open()
        job = {"name": "docs", "root": "sources", "source": "Docs"}
        self.assertEqual(self.c.call("POST", "/api/jobs", job, csrf=False)[0], 403)
        self.assertEqual(self.c.call("POST", "/api/jobs", job,
                                     headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.svc.status()["jobs"], [])
        status, data, _ = self.c.call("POST", "/api/jobs", job)
        self.assertEqual(status, 200, data)
        self.assertEqual(self.svc.status()["jobs"][0]["name"], "docs")

    def test_a_cross_site_origin_is_refused_even_with_a_valid_token(self):
        self.c.open()
        status, data, _ = self.c.call("POST", "/api/jobs", {"name": "docs", "root": "sources", "source": "Docs"},
                                      headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-site", data["message"])

    def test_a_form_post_content_type_is_refused(self):
        self.c.open()
        status, _, _ = self.c.call("POST", "/api/jobs", raw="name=x",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)

    def test_dns_rebinding_host_is_refused_without_a_password(self):
        evil = Client(self.port, host="evil.example")
        self.assertEqual(evil.call("GET", "/api/session")[0], 403)
        self.assertEqual(Client(self.port, host=f"localhost:{self.port}").call("GET", "/api/session")[0], 200)

    def test_a_proxy_in_front_of_a_passwordless_instance_is_refused(self):
        for header in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "X-Real-IP", "Via"):
            status, data, _ = self.c.call("GET", "/api/session", headers={header: "203.0.113.9"})
            self.assertEqual(status, 403, header)
            self.assertIn("proxy", data["message"])

    def test_a_negative_content_length_is_refused(self):
        self.c.open()
        self.assertEqual(self.c.call("POST", "/api/jobs", raw="{}",
                                     headers={"Content-Length": "-1"})[0], 400)

    def test_connections_are_capped(self):
        import socket

        from keepsake import server as server_mod
        server_mod.MAX_CONNECTIONS = 2
        try:
            httpd = serve(self.svc)
        finally:
            server_mod.MAX_CONNECTIONS = 64
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        try:
            for s in held:
                s.sendall(b"GET /api/session HTTP/1.1\r\nHost: x\r\n")  # stalls, holds a slot
            import time
            time.sleep(0.3)
            third = socket.create_connection(("127.0.0.1", port))
            third.settimeout(5)
            third.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                reply = third.recv(100)
            except ConnectionResetError:
                reply = b""  # a reset is also a turn-away
            self.assertEqual(reply, b"")
            third.close()
        finally:
            for s in held:
                s.close()
            httpd.shutdown()
            httpd.server_close()

    def test_oversize_and_bad_json_are_refused(self):
        self.c.open()
        self.assertEqual(self.c.call("POST", "/api/jobs", raw="x" * 70000)[0], 413)
        self.assertEqual(self.c.call("POST", "/api/jobs", raw="{nope")[0], 400)

    def test_browse_refuses_traversal_over_http(self):
        self.c.open()
        ok = self.c.call("GET", "/api/browse?root=sources&path=Photos")
        self.assertEqual([f["path"] for f in ok[1]["folders"]], ["Photos/2025"])
        for query in ("root=sources&path=../", "root=sources&path=%2Fetc",
                      "root=etc&path=", "root=sources&path=Photos/../../x"):
            status, data, _ = self.c.call("GET", "/api/browse?" + query)
            self.assertEqual(status, 400, query)
            self.assertFalse(data["ok"])

    def test_refusals_are_plain_json_never_a_traceback(self):
        self.c.open()
        status, data, _ = self.c.call("POST", "/api/jobs", {"name": "x", "root": "sources", "source": "../.."})
        self.assertEqual(status, 400)
        self.assertIn("..", data["message"])
        self.assertNotIn("Traceback", json.dumps(data))
        self.assertEqual(self.c.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.c.call("GET", "/etc/passwd")[0], 404)

    def test_full_job_lifecycle_over_the_api(self):
        self.c.open()
        job = {"name": "docs", "root": "sources", "source": "Docs"}
        self.assertEqual(self.c.call("POST", "/api/jobs", job)[0], 200)
        self.svc.run_next()
        status, view, _ = self.c.call("GET", "/api/jobs/docs")
        self.assertEqual((status, view["result"]), (200, "ok"))
        self.assertEqual(self.c.call("PATCH", "/api/jobs/docs", {"keep_days": 9})[0], 200)
        self.assertEqual(self.c.call("POST", "/api/jobs/docs/sync", {})[0], 200)
        self.assertEqual(self.c.call("POST", "/api/jobs/docs/sync", {})[0], 400)
        self.assertEqual(self.c.call("GET", "/api/activity")[1]["entries"][0]["job"], "docs")
        self.assertEqual(self.c.call("PUT", "/api/settings", {"max_dest_gb": 3})[0], 200)
        self.assertEqual(self.c.call("POST", "/api/jobs/docs/remove", {})[0], 200)
        self.assertEqual(self.c.call("GET", "/api/jobs/docs")[0], 404)

    def test_check_a_copy_over_the_api(self):
        self.c.open()
        self.assertEqual(self.c.call("POST", "/api/jobs/docs/verify", {})[0], 400)
        self.c.call("POST", "/api/jobs", {"name": "docs", "root": "sources", "source": "Docs"})
        status, data, _ = self.c.call("POST", "/api/jobs/docs/verify", {})
        self.assertEqual((status, data["ok"]), (400, False))      # no run yet: a refusal
        self.assertIn("nothing to check yet", data["message"])
        self.svc.run_next()
        self.assertEqual(self.c.call("POST", "/api/jobs/docs/verify", csrf=False)[0], 403)
        status, data, _ = self.c.call("POST", "/api/jobs/docs/verify", {})
        self.assertEqual((status, data["ok"], data["verify"]["state"]), (200, True, "match"))
        self.assertEqual(data["message"], "Checked 1 file, all match")
        (self.tmp / "icloud/docs/b.txt").write_text("c")          # same size, other bytes
        status, data, _ = self.c.call("POST", "/api/jobs/docs/verify", {})
        self.assertEqual((status, data["ok"], data["verify"]["state"]), (409, False, "mismatch"))
        self.assertIn("b.txt", data["message"])
        job = self.c.call("GET", "/api/jobs/docs")[1]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["verify"]["state"], "mismatch")

    def test_deleted_files_and_restore_over_the_api(self):
        self.c.open()
        for i in range(6):
            write(self.tmp / f"sources/Docs/keep{i}.txt", "k")
        self.c.call("POST", "/api/jobs", {"name": "docs", "root": "sources", "source": "Docs"})
        self.svc.run_next()
        (self.tmp / "sources/Docs/b.txt").unlink()
        self.c.call("POST", "/api/jobs/docs/sync", {})
        self.svc.run_next()
        status, data, _ = self.c.call("GET", "/api/jobs/docs/deleted")
        self.assertEqual(status, 200)
        self.assertEqual([e["path"] for e in data["entries"]], ["b.txt"])
        self.assertEqual(data["restore_folder"], ".restored")
        day = data["entries"][0]["date"]
        self.assertEqual(self.c.call("POST", "/api/jobs/docs/restore",
                                     {"date": day, "path": "b.txt"}, csrf=False)[0], 403)
        status, data, _ = self.c.call("POST", "/api/jobs/docs/restore", {"date": day, "path": "b.txt"})
        self.assertEqual(status, 200, data)
        self.assertEqual((self.tmp / f"icloud/docs/.restored/{day}/b.txt").read_text(), "b")
        self.assertFalse((self.tmp / "sources/Docs/b.txt").exists(), "the source is never written")
        for body in ({"date": day, "path": "../../.keepsake-owner"}, {"date": "..", "path": "b.txt"},
                     {"date": day}, {}, {"date": day, "path": ["b.txt"]}):
            status, data, _ = self.c.call("POST", "/api/jobs/docs/restore", body)
            self.assertEqual((status, data["ok"]), (400, False), body)
        self.assertEqual(self.c.call("GET", "/api/jobs/nope/deleted")[0], 400)

    def test_confirming_the_icloud_folder_over_the_api(self):
        (self.tmp / "icloud/.keepsake-anchor").unlink()
        self.c.open()
        self.assertFalse(self.c.call("GET", "/api/status")[1]["anchor_ok"])
        self.assertEqual(self.c.call("POST", "/api/icloud/confirm", csrf=False)[0], 403)
        self.assertEqual(self.c.call("POST", "/api/icloud/confirm", {})[0], 200)
        self.assertTrue(self.c.call("GET", "/api/status")[1]["anchor_ok"])

    def test_health(self):
        status, data, _ = self.c.call("GET", "/healthz")
        self.assertEqual((status, data), (200, {"ok": True}))


class WithPassword(ApiBase):
    password = PASSWORD

    def test_nothing_works_before_login(self):
        info = self.c.open()
        self.assertTrue(info["auth_required"])
        self.assertFalse(info["authenticated"])
        for path in ("/api/status", "/api/browse?root=sources", "/api/activity", "/api/settings"):
            self.assertEqual(self.c.call("GET", path)[0], 401, path)
        self.assertEqual(self.c.call("POST", "/api/jobs", {})[0], 401)

    def test_wrong_password_then_right_password_and_a_fresh_session_id(self):
        self.c.open()
        before = self.c.cookie
        self.assertEqual(self.c.call("POST", "/api/login", {"password": "nope"})[0], 401)
        status, data, _ = self.c.call("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertNotEqual(self.c.cookie, before)
        self.c.csrf = data["csrf"]
        self.assertEqual(self.c.call("GET", "/api/status")[0], 200)
        self.assertEqual(self.c.call("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.c.call("GET", "/api/status")[0], 401)

    def test_old_session_cookie_cannot_be_reused_after_login(self):
        self.c.open()
        old = self.c.cookie
        self.c.call("POST", "/api/login", {"password": PASSWORD})
        stale = Client(self.port)
        stale.cookie = old
        self.assertEqual(stale.call("GET", "/api/status")[0], 401)

    def test_login_is_rate_limited(self):
        self.c.open()
        codes = [self.c.call("POST", "/api/login", {"password": f"bad{i}"})[0] for i in range(7)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5:], [429, 429])
        status, data, headers = self.c.call("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 429, "the right password does not bypass the lock")
        self.assertIn("Retry-After", headers)

    def test_bearer_token_works_without_csrf_and_a_wrong_one_does_not(self):
        c = Client(self.port)
        good = {"Authorization": "Bearer " + PASSWORD}
        self.assertEqual(c.call("GET", "/api/status", headers=good)[0], 200)
        job = {"name": "docs", "root": "sources", "source": "Docs"}
        self.assertEqual(c.call("POST", "/api/jobs", job, headers=good, csrf=False)[0], 200)
        self.assertEqual(c.call("GET", "/api/status", headers={"Authorization": "Bearer wrong"})[0], 401)

    def test_login_failure_never_logs_the_password(self):
        self.c.open()
        with self.assertLogs("keepsake", level="DEBUG") as logs:
            logging.getLogger("keepsake").debug("probe")
            self.c.call("POST", "/api/login", {"password": "my-guess-123"})
        self.assertNotIn("my-guess-123", "\n".join(logs.output))
        self.assertNotIn(PASSWORD, "\n".join(logs.output))

    def test_a_lan_host_name_is_fine_when_a_password_is_set(self):
        lan = Client(self.port, host="mac-mini.local:8765")
        self.assertEqual(lan.call("GET", "/api/session")[0], 200)

    def test_a_flood_of_anonymous_sessions_does_not_log_a_person_out(self):
        self.c.open()
        status, data, _ = self.c.call("POST", "/api/login", {"password": PASSWORD})
        self.c.csrf = data["csrf"]
        for _ in range(80):
            Client(self.port).open()
        self.assertEqual(self.c.call("GET", "/api/status")[0], 200)

    def test_a_stalled_client_is_dropped(self):
        import socket

        from keepsake import server as server_mod
        server_mod.REQUEST_TIMEOUT = 1
        try:
            httpd = serve(self.svc)
        finally:
            server_mod.REQUEST_TIMEOUT = 20
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            s = socket.create_connection(("127.0.0.1", httpd.server_address[1]))
            s.sendall(b"GET /api/session HTTP/1.1\r\nHost: x\r\n")  # never finishes
            s.settimeout(10)
            self.assertEqual(s.recv(100), b"")  # the server hung up
            s.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_cookie_flags(self):
        status, _, headers = self.c.call("GET", "/api/session")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)


class StartupRefusal(unittest.TestCase):
    def test_extra_allowed_hosts_without_a_password_refuse_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(make_cfg(Path(tmp)), port=0, password="", allowed_hosts=("x.example",))
            with self.assertRaises(ConfigError):
                serve(Service(cfg, []))

    def test_serve_refuses_a_reachable_address_without_a_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(make_cfg(Path(tmp)), host="0.0.0.0", port=0, password="")
            with self.assertRaises(ConfigError) as ctx:
                serve(Service(cfg, []))
            self.assertIn("no password", str(ctx.exception))

    def test_main_exits_2_for_each_unsafe_start(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            keep = dict(os.environ)
            try:
                for extra in ({"KEEPSAKE_HOST": "0.0.0.0"},
                              {"KEEPSAKE_HOST": "0.0.0.0", "KEEPSAKE_PASSWORD": "short"}):
                    os.environ.clear()
                    os.environ.update({"KEEPSAKE_DATA": tmp, "KEEPSAKE_PORT": "18999", **extra})
                    self.assertEqual(main([]), 2, extra)
            finally:
                os.environ.clear()
                os.environ.update(keep)


if __name__ == "__main__":
    unittest.main()
