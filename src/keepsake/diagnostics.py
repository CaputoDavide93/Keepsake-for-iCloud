"""A plain-text report a person can attach to a bug report, safe by construction.

Two rules make it safe. It is built only from values that are already public in
the status API (counts, times, states, channel names), never from the config
fields that hold secrets. And every text that could carry something else, the
log lines and the sentences a run left behind, goes through ``redact`` last:
configured secret values, then URLs (cut to scheme and host), then anything
after ``token=``, ``password=`` or ``Authorization:``, then the home directory
(``~``). File names from inside a person's folders are not included.
"""

from __future__ import annotations

import collections
import logging
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any, Deque, Iterable, List, Optional
from urllib.parse import urlsplit

from keepsake import __version__
from keepsake.config import Config

LOG_LINES = 200       # how many recent log lines the report carries
LOG_LINE_MAX = 400    # characters kept per line
FILENAME = "keepsake-diagnostics.txt"

URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>]+")
AUTH = re.compile(r"(?i)\b(authorization|proxy-authorization)\b\s*[:=]\s*.*")
KEYED = re.compile(r"(?i)\b(\w*(?:token|password|passwd|secret|api[_-]?key|access[_-]?key))"
                   r"\b(\s*[=:]\s*)[^\s,;&]+")
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}")
# The engine's per-file warnings name a file inside a folder and repeat the
# system's error, which names its path; keep the fact, drop the name.
FILE_WARNING = re.compile(r"(could not (?:copy|retire)) .*")


class RingHandler(logging.Handler):
    """The last few log lines, in memory only. Bounded: old lines fall off."""

    def __init__(self, capacity: int = LOG_LINES) -> None:
        super().__init__(level=logging.INFO)
        self.buffer: Deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record).split("\n", 1)[0]
            if record.exc_info and record.exc_info[0] is not None:
                # The type only: a traceback and its message can name files.
                line += f" [{record.exc_info[0].__name__}]"
            self.buffer.append(line[:LOG_LINE_MAX])  # deque appends are atomic
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def lines(self) -> List[str]:
        return list(self.buffer)


RING = RingHandler()


def install_ring(logger: Optional[logging.Logger] = None,
                 handler: Optional[RingHandler] = None) -> RingHandler:
    """Attach the ring to the root logger (once), next to logging.basicConfig."""
    logger = logger or logging.getLogger()
    handler = handler or RING
    if handler not in logger.handlers:
        logger.addHandler(handler)
    return handler


def _secret_values(cfg: Config) -> List[str]:
    values = [cfg.password, cfg.webhook_url, cfg.mqtt_password, cfg.ntfy_url,
              cfg.ntfy_token, cfg.pushover_token, cfg.pushover_user,
              cfg.smtp_password]
    # Longest first, so a URL is replaced whole before its token alone.
    return sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)


def _short_url(match: "re.Match[str]") -> str:
    try:
        parts = urlsplit(match.group(0))
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "[url]"
    return f"{parts.scheme}://{host}{port}" if host else "[url]"


def redact(text: Any, cfg: Config, home: Optional[str] = None) -> str:
    """Make one piece of text safe to share. See the module docstring."""
    out = str(text)
    for value in _secret_values(cfg):
        out = out.replace(value, "[redacted]")
    out = AUTH.sub(lambda m: m.group(1) + ": [redacted]", out)
    out = BEARER.sub(lambda m: m.group(1) + " [redacted]", out)
    out = URL.sub(_short_url, out)
    out = KEYED.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", out)
    out = FILE_WARNING.sub(lambda m: m.group(1) + " a file (name left out)", out)
    home = str(Path.home()) if home is None else home
    if len(home.rstrip("/\\")) > 1:
        out = re.sub(re.escape(home.rstrip("/\\")) + r"(?![\w.\-])", "~", out)
    return out


def _uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}d {hours}h {rest // 60}m"


def _folder_block(view: dict, auto: Optional[dict], say) -> List[str]:
    check = view.get("verify") or {}
    lines = [
        f"[{say(view['name'])}]",
        f"  enabled: {view['enabled']}  mode: {view['mode']}  schedule: {view['schedule']}",
        f"  status: {view['status']}  last run: {view.get('last_run') or 'never'}"
        f"  outcome: {view['result']}",
    ]
    if view.get("error"):
        lines.append(f"  error: {say(view['error'])}")
    lines.append(
        f"  counts: files {view['files_total']}, copied {view['files_copied']}, "
        f"moved to .deleted {view['moved_to_deleted']}, "
        f"iCloud bytes {view['icloud_bytes']}")
    ch = view.get("changes") or {}
    if ch:
        lines.append(f"  last changes: added {ch['added']}, updated {ch['updated']}, "
                     f"removed {ch['removed']}")
    lines.append(f"  iCloud state: evicted placeholders {view['icloud']['evicted'] or 0}")
    if check:
        lines.append(f"  last self-check: {check.get('state', '?')} at "
                     f"{check.get('at', '?')}: {say(check.get('message', ''))}")
    else:
        lines.append("  last self-check: none yet")
    if auto:
        lines.append(f"  last scheduled check: {auto.get('result', '?')} at "
                     f"{auto.get('at', '?')}")
    return lines


def build_report(svc: Any, log_lines: Optional[Iterable[str]] = None,
                 home: Optional[str] = None) -> str:
    """The report text. ``svc`` is the Service; ``log_lines`` default to the ring."""
    cfg = svc.cfg

    def say(text: Any) -> str:
        return redact(text, cfg, home)

    status = svc.status()
    pause = status["pause"]
    out = [
        "Keepsake diagnostics",
        "Built to leave out passwords, tokens, addresses and file names from your "
        "folders. Read it before you post it anyway.",
        "",
        "== Environment ==",
        f"Keepsake: {__version__}",
        f"Python: {sys.version.split()[0]}",
        f"Platform: {platform.platform()}",
        f"Uptime: {_uptime(time.monotonic() - svc.started)}",
        f"Time zone: {cfg.tz}",
        "",
        "== Alert channels (names only) ==",
    ]
    for chan in svc.channels_view():
        out.append(f"{chan['name']}: {'configured' if chan['configured'] else 'not set'}")
    out.append(f"alert events: {', '.join(cfg.notify_on) or 'none'}")
    out += ["", "== Schedule ==",
            f"paused: {pause['paused']}  kind: {pause['kind'] or '-'}  "
            f"until: {pause['until'] or '-'}",
            "quiet hours: " + (f"{pause['quiet']['start']} to {pause['quiet']['end']}"
                               if pause["quiet"] else "off")
            + f"  in quiet hours now: {pause['in_quiet']}",
            f"running now: {say(status['running'] or '-')}",
            f"iCloud folder available: {status['icloud_available']}  "
            f"confirmed: {status['anchor_ok']}",
            "", "== Folders =="]
    if not status["jobs"]:
        out.append("(none)")
    for view in status["jobs"]:
        out += _folder_block(view, svc.state.get(view["name"], {}).get("auto_check"), say)
    lines = list(RING.lines() if log_lines is None else log_lines)[-LOG_LINES:]
    out += ["", f"== Recent log (last {len(lines)} lines, redacted) =="]
    out += [say(line)[:LOG_LINE_MAX] for line in lines] or ["(empty)"]
    return "\n".join(out) + "\n"
