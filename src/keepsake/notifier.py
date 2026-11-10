"""Optional notifiers. Nothing here is required: with none configured Keepsake
shows everything in its own web UI and writes nothing anywhere else.

A notifier receives small JSON events and must never raise out of ``send``: a
broken channel must not stop a sync. ``test`` is the same delivery, but it says
in a plain sentence why it failed, for the Settings page.

  * ``WebhookNotifier``: HTTP POST of the event as JSON (standard library only).
  * ``NtfyNotifier``: HTTP POST of a short text to an ntfy topic address.
  * ``PushoverNotifier``: the Pushover HTTP API (application token and user key).
  * ``EmailNotifier``: smtplib, STARTTLS or SSL, optional login.
  * ``MqttNotifier``: retained status and events over MQTT, on the small
    standard-library client in ``mqttlite`` (no extra package).

Every secret (webhook and topic addresses, tokens, passwords) is kept out of
logs and of the sentences returned by ``test``.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional, Tuple

from keepsake.config import Config, ConfigError
from keepsake.mqttlite import MqttClient, MqttError

logger = logging.getLogger("keepsake")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
ERROR_MAX = 200


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):  # noqa: D401
        return None


class NotifyError(Exception):
    """A delivery failed. The text is a plain sentence with no secret in it."""


def describe(event: Dict[str, Any]) -> Tuple[str, str, bool]:
    """(title, body, urgent) for the channels that show text. Ascii titles: the
    title travels in an HTTP header."""
    kind = event.get("event", "")
    job = event.get("job") or ""
    what = {"failed": "needs attention", "stale": "has not backed up",
            "recovered": "is fine again"}.get(kind)
    if kind == "test":
        title = "Keepsake: test alert"
    elif what:
        title = f"Keepsake: {job} {what}".strip()
    else:
        title = f"Keepsake: {kind or 'message'}"
    body = str(event.get("message") or "")
    if event.get("at"):
        body = f"{body}\n{event['at']}" if body else str(event["at"])
    return title.encode("ascii", "replace").decode(), body, kind in ("failed", "stale")


class Channel:
    """Shared plumbing: `send` for the sync path, `test` for the Settings page."""

    name = ""

    def secrets(self) -> List[str]:
        return []

    @property
    def label(self) -> str:
        return self.name

    def deliver(self, event: Dict[str, Any]) -> None:
        raise NotImplementedError

    def _clean(self, text: str) -> str:
        for secret in self.secrets():
            if secret:
                text = text.replace(secret, "***")
        return text[:ERROR_MAX]

    def send(self, event: Dict[str, Any]) -> bool:
        try:
            self.deliver(event)
            return True
        except NotifyError:
            # Never the address or a token: the channel's label only.
            logger.warning("%s alert to %s failed", self.name, self.label)
            return False
        except Exception:  # noqa: BLE001 - a notifier never stops a sync
            logger.warning("%s alert to %s failed", self.name, self.label)
            return False

    def test(self, event: Dict[str, Any]) -> Tuple[bool, str]:
        try:
            self.deliver(event)
            return True, ""
        except NotifyError as exc:
            return False, self._clean(str(exc))
        except Exception as exc:  # noqa: BLE001
            return False, self._clean(f"unexpected error: {type(exc).__name__}")


def _http(opener: urllib.request.OpenerDirector, req: urllib.request.Request,
          timeout: float) -> bytes:
    """One request. A non-2xx answer, a refused connection and a timeout each
    become a sentence; the URL never appears in it."""
    try:
        with opener.open(req, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                raise NotifyError(f"the server answered {resp.status}")
            return resp.read(4096)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(512).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise NotifyError(f"the server answered {exc.code}"
                          + (f": {_detail(detail)}" if _detail(detail) else "")) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise NotifyError(f"could not reach the server: "
                          f"{getattr(reason, 'strerror', None) or type(reason).__name__}"
                          ) from None
    except TimeoutError:
        raise NotifyError("the server did not answer in time") from None
    except (OSError, ValueError):
        raise NotifyError("could not send the request") from None


def _detail(body: str) -> str:
    """The short human reason in a JSON error body (Pushover and ntfy send one)."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors[:3])
        if isinstance(data.get("error"), str):
            return data["error"]
    return ""


class WebhookNotifier(Channel):
    name = "webhook"

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self._opener = urllib.request.build_opener(NoRedirect)

    def secrets(self) -> List[str]:
        return [self.url]

    @property
    def label(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc or "webhook"

    def deliver(self, event: Dict[str, Any]) -> None:
        req = urllib.request.Request(
            self.url, data=json.dumps(event).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "keepsake-for-icloud"})
        _http(self._opener, req, self.timeout)


class NtfyNotifier(Channel):
    """POST the message to an ntfy topic address (https://ntfy.sh/<topic> or
    your own server). The address is the secret: anyone who has it can read the
    topic. An access token is optional."""

    name = "ntfy"

    def __init__(self, url: str, token: str = "", timeout: float = 10.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(NoRedirect)

    def secrets(self) -> List[str]:
        return [self.url, self.token]

    @property
    def label(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc or "ntfy"

    def deliver(self, event: Dict[str, Any]) -> None:
        title, body, urgent = describe(event)
        headers = {"Title": title, "Priority": "high" if urgent else "default",
                   "Tags": "warning" if urgent else "white_check_mark",
                   "Content-Type": "text/plain; charset=utf-8",
                   "User-Agent": "keepsake-for-icloud"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=(body or title).encode("utf-8"),
                                     method="POST", headers=headers)
        _http(self._opener, req, self.timeout)


class PushoverNotifier(Channel):
    name = "pushover"

    def __init__(self, token: str, user: str, api_url: str = PUSHOVER_URL,
                 timeout: float = 10.0) -> None:
        self.token = token
        self.user = user
        self.api_url = api_url
        self.timeout = timeout
        self._opener = urllib.request.build_opener(NoRedirect)

    def secrets(self) -> List[str]:
        return [self.token, self.user]

    @property
    def label(self) -> str:
        return "pushover"

    def deliver(self, event: Dict[str, Any]) -> None:
        title, body, urgent = describe(event)
        form = urllib.parse.urlencode({
            "token": self.token, "user": self.user, "title": title,
            "message": body or title, "priority": 1 if urgent else 0}).encode()
        req = urllib.request.Request(
            self.api_url, data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "keepsake-for-icloud"})
        _http(self._opener, req, self.timeout)


class EmailNotifier(Channel):
    name = "email"

    def __init__(self, cfg: Config, timeout: float = 15.0) -> None:
        self.cfg = cfg
        self.timeout = timeout

    def secrets(self) -> List[str]:
        return [self.cfg.smtp_password]

    @property
    def label(self) -> str:
        return self.cfg.smtp_host or "email"

    def deliver(self, event: Dict[str, Any]) -> None:
        c = self.cfg
        title, body, _ = describe(event)
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = c.smtp_from
        msg["To"] = ", ".join(c.smtp_to)
        msg.set_content(body or title)
        try:
            if c.smtp_security == "ssl":
                server = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=self.timeout,
                                          context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=self.timeout)
            try:
                if c.smtp_security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                if c.smtp_user:
                    server.login(c.smtp_user, c.smtp_password)
                server.send_message(msg)
            finally:
                try:
                    server.quit()
                except (smtplib.SMTPException, OSError):
                    pass
        except smtplib.SMTPAuthenticationError:
            raise NotifyError("the mail server refused the user name or password") from None
        except smtplib.SMTPRecipientsRefused:
            raise NotifyError("the mail server refused the recipient address") from None
        except smtplib.SMTPException as exc:
            raise NotifyError(f"the mail server refused the message: "
                              f"{type(exc).__name__}") from None
        except ssl.SSLError as exc:
            raise NotifyError(f"the secure connection failed: "
                              f"{exc.reason or type(exc).__name__}") from None
        except TimeoutError:
            raise NotifyError("the mail server did not answer in time") from None
        except OSError as exc:
            raise NotifyError(f"could not reach the mail server: "
                              f"{exc.strerror or type(exc).__name__}") from None


class MqttNotifier(Channel):
    name = "mqtt"

    def __init__(self, cfg: Config, client_factory: Optional[Callable[[], Any]] = None) -> None:
        self.cfg = cfg
        self._factory = client_factory

    def secrets(self) -> List[str]:
        return [self.cfg.mqtt_password]

    @property
    def label(self) -> str:
        return self.cfg.mqtt_host or "mqtt"

    def _client(self) -> Any:
        if self._factory:
            return self._factory()
        client = MqttClient()
        if self.cfg.mqtt_user:
            client.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_password)
        return client

    def deliver(self, event: Dict[str, Any]) -> None:
        try:
            client = self._client()
            client.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, 30)
            info = client.publish(f"{self.cfg.mqtt_prefix}/event", json.dumps(event))
            if event.get("job"):
                client.publish(f"{self.cfg.mqtt_prefix}/job/{event['job']}",
                               json.dumps(event), retain=True)
            wait = getattr(info, "wait_for_publish", None)
            if wait:
                wait(timeout=10)
            client.disconnect()
        except (ConfigError, MqttError) as exc:
            raise NotifyError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a notifier never stops a sync
            raise NotifyError(f"could not publish to the broker: "
                              f"{getattr(exc, 'strerror', None) or type(exc).__name__}"
                              ) from None


def build_notifiers(cfg: Config) -> List[Any]:
    out: List[Any] = []
    if cfg.webhook_url:
        out.append(WebhookNotifier(cfg.webhook_url))
    if cfg.ntfy_url:
        out.append(NtfyNotifier(cfg.ntfy_url, cfg.ntfy_token))
    if cfg.pushover_token and cfg.pushover_user:
        out.append(PushoverNotifier(cfg.pushover_token, cfg.pushover_user))
    if cfg.smtp_host and cfg.smtp_from and cfg.smtp_to:
        out.append(EmailNotifier(cfg))
    if cfg.mqtt_host:
        out.append(MqttNotifier(cfg))
    return out
