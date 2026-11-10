"""The web server: REST API, static UI, login, CSRF protection, security headers.

Standard library only (``http.server``). The traffic is one household clicking
a few buttons, so a threaded stdlib server is enough and keeps the dependency
list empty; the price is no TLS and no HTTP/2: put a reverse proxy in front if
you expose it beyond one machine (see docs/security.md).

Protections, all enforced here:

  * every ``/api`` route except session, login and health needs a session (or
    the password as a Bearer token) when a password is configured;
  * every state-changing request needs the per-session CSRF token in the
    ``X-CSRF-Token`` header, a JSON content type, and an Origin that matches
    the Host (a cross-site page can set none of those);
  * with no password (loopback only) the Host header must name this machine, so
    DNS rebinding cannot reach the API;
  * login failures are rate limited; secrets are never logged;
  * the password can be changed from the page (``POST /api/password``); the new
    one is stored hashed and every other login is signed out.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from keepsake import __version__, diagnostics
from keepsake.auth import PasswordStore, check_new_password, stored_password_exists
from keepsake.config import ConfigError, is_loopback
from keepsake.service import Service

logger = logging.getLogger("keepsake")

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 64  # at most this many requests are served at once
REQUEST_TIMEOUT = 20  # seconds a client may stall before it is dropped
SESSION_TTL = 12 * 3600
MAX_SESSIONS = 50
LOGIN_WINDOW = 300
LOGIN_PER_CLIENT = 5
LOGIN_GLOBAL = 25
COOKIE = "keepsake_session"
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                 "X-Real-IP", "Forwarded", "Via")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon-32.png": ("favicon-32.png", "image/png"),
    "/touch-icon.png": ("touch-icon.png", "image/png"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


class HttpError(Exception):
    def __init__(self, status: int, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class Sessions:
    """In-memory sessions: id -> {csrf, auth, expires}. Lost on restart by design."""

    def __init__(self) -> None:
        self.items: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def create(self, auth: bool = False, epoch: str = "") -> Tuple[str, dict]:
        sid = secrets.token_urlsafe(32)
        item = {"csrf": secrets.token_urlsafe(24), "auth": auth, "epoch": epoch,
                "expires": time.time() + SESSION_TTL}
        with self.lock:
            now = time.time()
            for key in [k for k, v in self.items.items() if v["expires"] < now]:
                del self.items[key]
            while len(self.items) >= MAX_SESSIONS:
                # Anonymous sessions go first, so a flood of page loads cannot
                # log a person out.
                pool = [k for k, v in self.items.items() if not v["auth"]] or list(self.items)
                del self.items[min(pool, key=lambda k: self.items[k]["expires"])]
            self.items[sid] = item
        return sid, item

    def get(self, sid: Optional[str]) -> Optional[dict]:
        if not sid:
            return None
        with self.lock:
            item = self.items.get(sid)
            if item and item["expires"] < time.time():
                del self.items[sid]
                return None
            return item

    def drop(self, sid: Optional[str]) -> None:
        with self.lock:
            self.items.pop(sid or "", None)

    def keep_only(self, sid: Optional[str], epoch: str) -> None:
        """After a password change: sign every other login out, keep this one."""
        with self.lock:
            for key in [k for k, v in self.items.items() if v["auth"] and k != sid]:
                del self.items[key]
            if sid in self.items:
                self.items[sid]["epoch"] = epoch


class LoginLimiter:
    def __init__(self) -> None:
        self.fails: Dict[str, list] = {}
        self.lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list:
        kept = [t for t in self.fails.get(key, []) if now - t < LOGIN_WINDOW]
        self.fails[key] = kept
        return kept

    def check(self, client: str) -> None:
        now = time.time()
        with self.lock:
            mine = self._prune(client, now)
            everyone = self._prune("*", now)
            if len(mine) >= LOGIN_PER_CLIENT or len(everyone) >= LOGIN_GLOBAL:
                oldest = min(mine or everyone)
                raise HttpError(429, "too many wrong passwords; wait a few "
                                "minutes and try again",
                                int(LOGIN_WINDOW - (now - oldest)) + 1)

    def fail(self, client: str) -> None:
        now = time.time()
        with self.lock:
            self._prune(client, now).append(now)
            self._prune("*", now).append(now)

    def ok(self, client: str) -> None:
        with self.lock:
            self.fails.pop(client, None)


class App:
    def __init__(self, svc: Service) -> None:
        self.svc = svc
        self.sessions = Sessions()
        self.auth = PasswordStore(svc.cfg.data_dir, svc.cfg.password)
        self.limiter = LoginLimiter()
        self.static = {path: ((WEB_DIR / name).read_bytes(), ctype)
                       for path, (name, ctype) in STATIC.items()}

    @property
    def cfg(self):
        return self.svc.cfg


SETUP_REFUSAL = ("finish the guided setup first: until it is done Keepsake only "
                 "allows the setup steps")


def setup_allows(method: str, segs: List[str], svc: Any) -> bool:
    """What the REST API still answers while the guided setup is not complete.

    An ALLOW list, not a block list: a route added later is locked until someone
    names it here. The wizard needs to read the status, browse the readable
    locations, confirm the iCloud folder, add a folder, preview, sync and check
    it, and sign out. `destinations` feeds the destination list of the picker and
    every `locations` route lets the wizard add, remove and browse the places
    Keepsake may read. It may also remove a folder that has not made a copy yet,
    so a wrong choice is not a trap. Settings,
    alerts, restore, deleted files, the diagnostics and every change to an
    existing copy wait for the end of the setup.
    """
    if segs == ["logout"] and method == "POST":
        return True
    if segs in (["status"], ["browse"], ["destinations"]) and method == "GET":
        return True
    if segs[:1] == ["locations"]:
        return True
    if segs == ["icloud", "confirm"] and method == "POST":
        return True
    if segs == ["jobs"] and method == "POST":
        return True
    if len(segs) == 3 and segs[0] == "jobs":
        if segs[2] == "preview" and method in ("GET", "POST"):
            return True
        if segs[2] in ("sync", "verify", "cancel") and method == "POST":
            return True
        if segs[2] == "remove" and method == "POST":
            return svc.never_synced(segs[1])
    return False


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Keepsake"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT  # a stalled client cannot hold a thread forever

        # -- plumbing ----------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:  # no query, no cookies
            logger.debug("%s %s", self.command, urlsplit(self.path).path)

        def client(self) -> str:
            return self.client_address[0]

        def cookie(self) -> Optional[str]:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return None

        def send_bytes(self, status: int, body: bytes, ctype: str,
                       extra: Optional[Dict[str, str]] = None, api: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy",
                             "camera=(), microphone=(), geolocation=(), interest-cohort=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store" if api else "no-cache")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, status: int, data: Any,
                      extra: Optional[Dict[str, str]] = None) -> None:
            self.send_bytes(status, json.dumps(data).encode(),
                            "application/json; charset=utf-8", extra, api=True)

        def set_cookie(self, sid: str) -> Dict[str, str]:
            flags = "; HttpOnly; SameSite=Strict; Path=/"
            if app.cfg.cookie_secure:
                flags += "; Secure"
            return {"Set-Cookie": f"{COOKIE}={sid}{flags}; Max-Age={SESSION_TTL}"}

        # -- checks --------------------------------------------------------
        def check_host(self) -> None:
            host = (self.headers.get("Host") or "").lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") \
                else host.split("]")[0] + "]"
            if app.auth.configured:
                return  # a password stops a rebinding page; the host may be a LAN name
            if any(self.headers.get(h) for h in PROXY_HEADERS):
                # No password means "this Mac only". A reverse proxy would make
                # the page reachable by others, so it is refused outright.
                raise HttpError(403, "this request came through a proxy, which "
                                "needs a password: set KEEPSAKE_PASSWORD")
            allowed = {"localhost", "127.0.0.1", "[::1]", *app.cfg.allowed_hosts}
            if name not in allowed:
                raise HttpError(403, "this address is not allowed; open Keepsake "
                                "at http://localhost or set KEEPSAKE_ALLOWED_HOSTS")

        def bearer_ok(self) -> bool:
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Bearer ") or not app.auth.configured:
                return False
            app.limiter.check(self.client())
            if app.auth.verify(header[7:]):
                return True
            app.limiter.fail(self.client())
            return False

        def read_json(self) -> Any:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() \
                    != "application/json":
                raise HttpError(415, "send JSON with Content-Type: application/json")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HttpError(400, "bad Content-Length")
            if length < 0:
                raise HttpError(400, "bad Content-Length")
            if length > MAX_BODY:
                raise HttpError(413, "that request is too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                raise HttpError(400, "the request was not valid JSON")

        def check_csrf(self, session: Optional[dict]) -> None:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != (self.headers.get("Host") or ""):
                raise HttpError(403, "cross-site request refused")
            token = self.headers.get("X-CSRF-Token") or ""
            if not session or not hmac.compare_digest(token.encode(),
                                                      session["csrf"].encode()):
                raise HttpError(403, "the page is out of date; reload it and try again")

        # -- dispatch ------------------------------------------------------
        def handle_any(self) -> None:
            try:
                self.dispatch()
            except HttpError as exc:
                extra = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
                self.send_json(exc.status, {"ok": False, "message": exc.message}, extra)
            except ConfigError as exc:
                self.send_json(400, {"ok": False, "message": str(exc)})
            except Exception:  # noqa: BLE001 - never leak a traceback to the client
                logger.exception("request failed")
                self.send_json(500, {"ok": False, "message": "something went wrong; "
                                     "see the container log"})

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = handle_any

        def dispatch(self) -> None:
            parts = urlsplit(self.path)
            path = unquote(parts.path)
            method = "GET" if self.command == "HEAD" else self.command
            if path == "/healthz":
                alive = time.monotonic() - app.svc.last_beat < 180
                self.send_json(200 if alive else 503, {"ok": alive})
                return
            if path in app.static and method == "GET":
                body, ctype = app.static[path]
                self.send_bytes(200, body, ctype)
                return
            if not path.startswith("/api/"):
                raise HttpError(404, "not found")
            self.check_host()
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            session = app.sessions.get(self.cookie())
            if session is not None and session["auth"] and session["epoch"] != app.auth.epoch:
                app.sessions.drop(self.cookie())  # the password changed since this login
                session = None
            bearer = self.bearer_ok()
            body: Any = None
            if method != "GET":
                if not bearer:
                    self.check_csrf(session)
                body = self.read_json()
            if path == "/api/session" and method == "GET":
                self.route_session(session)
                return
            if path == "/api/login" and method == "POST":
                self.route_login(session, body)
                return
            authed = bearer or (session is not None and (
                session["auth"] or not app.auth.configured))
            if not authed:
                raise HttpError(401, "log in first")
            if not bearer and session is None:
                raise HttpError(401, "open the page first")
            self.route_api(method, path, query, body, session)

        def route_session(self, session: Optional[dict]) -> None:
            extra = None
            if session is None:
                sid, session = app.sessions.create()
                extra = self.set_cookie(sid)
            self.send_json(200, {
                "csrf": session["csrf"], "version": __version__,
                "auth_required": app.auth.configured,
                "authenticated": session["auth"] or not app.auth.configured}, extra)

        def route_login(self, session: Optional[dict], body: Any) -> None:
            if not app.auth.configured:
                raise HttpError(400, "no password is set; nothing to log in to")
            if not isinstance(body, dict):
                raise HttpError(400, "bad request")
            app.limiter.check(self.client())
            given = str(body.get("password", ""))
            if not app.auth.verify(given):
                app.limiter.fail(self.client())
                raise HttpError(401, "that password is not right")
            app.limiter.ok(self.client())
            app.sessions.drop(self.cookie())  # new id after login
            sid, item = app.sessions.create(auth=True, epoch=app.auth.epoch)
            self.send_json(200, {"ok": True, "csrf": item["csrf"]}, self.set_cookie(sid))

        def route_password(self, body: Any) -> None:
            if not app.auth.configured:
                raise HttpError(400, "no password is set, so Keepsake is open only on "
                                "this Mac and there is nothing to change")
            if not isinstance(body, dict):
                raise HttpError(400, "bad request")
            app.limiter.check(self.client())
            current = str(body.get("current", ""))
            if not app.auth.verify(current):
                app.limiter.fail(self.client())
                raise HttpError(403, "the current password is not right")
            new = check_new_password(body.get("new"), body.get("confirm"))
            if new == current:
                raise ConfigError("the new password is the same as the current one")
            epoch = app.auth.set(new)
            app.limiter.ok(self.client())
            app.sessions.keep_only(self.cookie(), epoch)
            self.send_json(200, {"ok": True, "message": "password changed; every other "
                                 "login was signed out"})

        def route_api(self, method: str, path: str, query: Dict[str, str],
                      body: Any, session: Optional[dict]) -> None:
            svc = app.svc
            segs = path.split("/")[2:]  # after /api
            ok = lambda message, **more: self.send_json(  # noqa: E731
                200, {"ok": True, "message": message, **more})
            if not svc.setup_complete() and not setup_allows(method, segs, svc):
                raise HttpError(403, SETUP_REFUSAL)
            if segs == ["logout"] and method == "POST":
                app.sessions.drop(self.cookie())
                ok("logged out")
            elif segs == ["password"] and method == "POST":
                self.route_password(body)
            elif segs == ["status"] and method == "GET":
                self.send_json(200, svc.status())
            elif segs == ["diagnostics"] and method == "GET":
                self.send_bytes(
                    200, diagnostics.build_report(svc).encode(),
                    "text/plain; charset=utf-8",
                    {"Content-Disposition":
                     f'attachment; filename="{diagnostics.FILENAME}"'}, api=True)
            elif segs == ["browse"] and method == "GET":
                self.send_json(200, svc.browse(query.get("root", ""),
                                               query.get("path", "")))
            elif segs == ["locations"] and method == "GET":
                self.send_json(200, svc.locations_view())
            elif segs == ["locations"] and method == "POST":
                ok(svc.add_location(body))
            elif segs == ["locations", "browse"] and method == "GET":
                self.send_json(200, svc.browse_locations(query.get("path", "")))
            elif len(segs) == 3 and segs[0] == "locations" and segs[2] == "remove" \
                    and method == "POST":
                ok(svc.remove_location(segs[1]))
            elif segs == ["destinations"] and method == "GET":
                self.send_json(200, svc.destinations())
            elif segs == ["activity"] and method == "GET":
                self.send_json(200, {"entries": svc.activity(
                    int(query.get("limit", "100") or 100))})
            elif segs == ["icloud", "confirm"] and method == "POST":
                ok(svc.confirm_icloud())
            elif segs == ["settings"] and method == "GET":
                self.send_json(200, svc.settings_view())
            elif segs == ["settings"] and method == "PUT":
                ok(svc.update_settings(body), settings=svc.settings_view())
            elif segs == ["settings", "test-alert"] and method == "POST":
                result = svc.send_test_alert()
                if result["ok"]:
                    ok(result["message"], results=result["results"])
                else:  # a channel that failed is never a success
                    self.send_json(502, {"ok": False, "message": result["message"],
                                         "results": result["results"]})
            elif segs == ["jobs"] and method == "POST":
                ok(svc.add_job(body))
            elif len(segs) == 2 and segs[0] == "jobs" and method == "GET":
                views = [v for v in svc.status()["jobs"] if v["name"] == segs[1]]
                if not views:
                    raise HttpError(404, f"there is no folder job named {segs[1]!r}")
                self.send_json(200, views[0])
            elif len(segs) == 2 and segs[0] == "jobs" and method == "PATCH":
                ok(svc.update_job(segs[1], body))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "sync" \
                    and method == "POST":
                ok(svc.sync_now(segs[1], (body or {}).get("accept_shrink")
                                if isinstance(body, dict) else False))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "preview" \
                    and method == "POST":
                ok(svc.preview(segs[1], (body or {}).get("accept_shrink")
                               if isinstance(body, dict) else False))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "preview" \
                    and method == "GET":
                self.send_json(200, svc.preview_view(segs[1]))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "cancel" \
                    and method == "POST":
                ok(svc.cancel(segs[1]))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "verify" \
                    and method == "POST":
                result = svc.verify_job(segs[1])
                if result["ok"]:
                    ok(result["message"], verify=result)
                else:  # a difference is never a success, even though the check ran
                    self.send_json(409, {"ok": False, "message": result["message"],
                                         "verify": result})
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "deleted" \
                    and method == "GET":
                self.send_json(200, svc.deleted_files(segs[1]))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "restore" \
                    and method == "POST":
                b = body if isinstance(body, dict) else {}
                ok(svc.restore_file(segs[1], b.get("date"), b.get("path")))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "restore-plan" \
                    and method == "GET":
                self.send_json(200, svc.restore_plan(segs[1], query.get("date")))
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "restore-batch" \
                    and method == "POST":
                b = body if isinstance(body, dict) else {}
                out = svc.restore_many(segs[1], b.get("date"), b.get("all"),
                                       b.get("expect_files"))
                ok(out["message"], restored=out["restored"], bytes=out["bytes"],
                   skipped=out["skipped"], dates=out["dates"])
            elif len(segs) == 3 and segs[0] == "jobs" and segs[2] == "remove" \
                    and method == "POST":
                ok(svc.remove_job(segs[1], (body or {}).get("delete_copy")
                                  if isinstance(body, dict) else False))
            else:
                raise HttpError(404, "not found")

    return Handler


class BoundedServer(ThreadingHTTPServer):
    """One thread per connection, but never more than MAX_CONNECTIONS at once."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def serve(svc: Service) -> ThreadingHTTPServer:
    cfg = svc.cfg
    has_password = bool(cfg.password) or stored_password_exists(cfg.data_dir)
    if not is_loopback(cfg.host) and not has_password:
        raise ConfigError("refusing to start: Keepsake is set to listen on "
                          f"{cfg.host}, which other devices can reach, but no "
                          "password is set. Set KEEPSAKE_PASSWORD, or keep "
                          "KEEPSAKE_HOST=127.0.0.1")
    if cfg.allowed_hosts and not has_password:
        raise ConfigError("refusing to start: KEEPSAKE_ALLOWED_HOSTS names other "
                          "host names but no password is set. Set KEEPSAKE_PASSWORD")
    app = App(svc)
    httpd = BoundedServer((cfg.host, cfg.port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
