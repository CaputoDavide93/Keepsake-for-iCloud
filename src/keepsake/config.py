"""Keepsake configuration: environment, config file, UI settings and job validation.

Precedence for every setting: environment variable, then the config file
(``config.json``), then the built-in default. Two groups can also be changed in
the web UI (the caps and the notifier); those are stored in ``settings.json`` in
the data directory and win over the defaults until changed again.

A job is one folder kept in iCloud Drive. Jobs live in ``jobs.json`` in the data
directory. Everything a job can name is checked here, once, before it can be
stored: a refusal is a plain sentence that can be shown to a person.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Letters of either case, digits and hyphens, no spaces. The case is kept as typed (on
# disk and in the API), so PhotoLibrary stays PhotoLibrary; but two names that differ
# only by case are one name, because a Mac volume cannot tell them apart.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MODES = ("mirror-keep-deleted", "mirror")
DEFAULT_MODE = "mirror-keep-deleted"
DEFAULT_KEEP_DAYS = 30
# Folder names that should never be offered or synced: they hold credentials.
SECRET_WORDS = ("secret", "password", "credential", "private-key", ".ssh",
                "keyring", "token")
NOTIFY_EVENTS = ("failed", "recovered", "stale")
# "Check a copy automatically": how often, and the default for a new folder.
AUTO_CHECKS = ("off", "daily", "weekly", "monthly")
DEFAULT_AUTO_CHECK = "weekly"
# "No successful run for N days" alert: 0 switches it off for the folder.
DEFAULT_STALE_DAYS = 3
SMTP_SECURITY = ("starttls", "ssl", "none")


class ConfigError(ValueError):
    """A plain-words reason a setting or job was refused."""


def _int(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from exc
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def parse_roots(raw: str) -> Dict[str, Path]:
    """`photos=/sources/photos,docs=/sources/docs` -> {name: path}."""
    roots: Dict[str, Path] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, path = part.partition("=")
        if not sep or not NAME_RE.match(name) or not path.startswith("/"):
            raise ConfigError(f"bad root {part!r}: expected name=/absolute/path")
        roots[name] = Path(path)
    return roots


def _roots_from_file(raw: Any) -> Dict[str, Path]:
    if not isinstance(raw, dict):
        raise ConfigError("config file: roots must be an object of name: path")
    return parse_roots(",".join(f"{k}={v}" for k, v in raw.items()))


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Config:
    data_dir: Path
    dest_root: Path
    roots: Dict[str, Path]
    max_dest_gb: int = 150
    min_free_gb: int = 20
    shrink_pct: int = 20
    pause_ms: int = 0
    max_mbps: int = 0
    tz: str = "UTC"
    # Pause everything until a time (ISO with an offset, "" = not paused) and an
    # optional daily quiet window (HH:MM, "" = none). `pause_ended` is when the
    # last pause stopped, so a long pause does not count as "no run for days".
    pause_until: str = ""
    pause_ended: str = ""
    quiet_start: str = ""
    quiet_end: str = ""
    # Roots whose directory must be the root of a mounted filesystem (inode 1 or
    # 2), and roots that must hold a marker file: a leftover plain folder where
    # a share used to be mounted then reads as unavailable.
    fs_roots: frozenset = frozenset()
    root_markers: Dict[str, str] = field(default_factory=dict)
    # Broad read-only mounts the page may add locations under (see locations.py).
    # `location_require_ro` is only ever turned off by the test suite.
    location_prefixes: tuple = ()
    location_require_ro: bool = True
    # Web server
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = field(default="", repr=False)
    cookie_secure: bool = False
    allowed_hosts: tuple = ()
    # Notifier (all optional)
    webhook_url: str = field(default="", repr=False)
    notify_on: tuple = NOTIFY_EVENTS
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = field(default="", repr=False)
    mqtt_prefix: str = "keepsake"
    # The topic prefix a home automation hub listens on for MQTT discovery. Empty (the
    # default) publishes only the alert events; set it to also publish live status.
    mqtt_discovery_prefix: str = ""
    # More notifiers (all optional, all off unless set). The secrets are never
    # shown by the API or written to a log.
    ntfy_url: str = field(default="", repr=False)
    ntfy_token: str = field(default="", repr=False)
    pushover_token: str = field(default="", repr=False)
    pushover_user: str = field(default="", repr=False)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    smtp_to: tuple = ()
    smtp_security: str = "starttls"

    @property
    def jobs_file(self) -> Path:
        return self.data_dir / "jobs.json"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def setup_file(self) -> Path:
        return self.data_dir / "setup.json"

    @property
    def activity_file(self) -> Path:
        return self.data_dir / "activity.jsonl"

    @property
    def manifest_dir(self) -> Path:
        return self.data_dir / "manifests"

    @property
    def needs_password(self) -> bool:
        return not is_loopback(self.host)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Config":
        env = dict(os.environ if env is None else env)
        data_dir = Path(env.get("KEEPSAKE_DATA") or "/data")
        cfg_path = Path(env.get("KEEPSAKE_CONFIG") or data_dir / "config.json")
        file: Dict[str, Any] = {}
        if cfg_path.exists():
            file = load_json(cfg_path, {})
            if not isinstance(file, dict):
                raise ConfigError("config file must be a JSON object")

        def pick(key: str, var: str, default: Any = None) -> Any:
            value = env.get(var)
            if value not in (None, ""):
                return value
            return file.get(key, default)

        def secret(key: str, var: str) -> str:
            path = env.get(var + "_FILE")
            if path:
                try:
                    return Path(path).read_text().strip()
                except OSError as exc:
                    raise ConfigError(f"{var}_FILE {path} cannot be read") from exc
            return str(pick(key, var, "") or "")

        roots_raw = env.get("KEEPSAKE_ROOTS")
        if roots_raw:
            roots = parse_roots(roots_raw)
        elif "roots" in file:
            roots = _roots_from_file(file["roots"])
        else:
            roots = {"sources": Path("/sources")}

        def listing(value: Any) -> List[str]:
            if isinstance(value, str):
                return [p.strip() for p in value.split(",") if p.strip()]
            return [str(p) for p in (value or [])]

        markers: Dict[str, str] = {}
        raw_markers = pick("root_markers", "KEEPSAKE_ROOT_MARKERS", {})
        if isinstance(raw_markers, str):
            raw_markers = dict(p.split("=", 1) for p in listing(raw_markers) if "=" in p)
        for name, marker in dict(raw_markers).items():
            if "/" in str(marker) or str(marker) in ("", ".", ".."):
                raise ConfigError("a root marker must be a plain file name")
            markers[str(name)] = str(marker)

        cfg = cls(
            data_dir=data_dir,
            dest_root=Path(str(pick("icloud_dir", "KEEPSAKE_ICLOUD_DIR", "/icloud"))),
            roots=roots,
            max_dest_gb=_int(pick("max_dest_gb", "KEEPSAKE_MAX_DEST_GB"),
                             "KEEPSAKE_MAX_DEST_GB", 150, 1, 100000),
            min_free_gb=_int(pick("min_free_gb", "KEEPSAKE_MIN_FREE_GB"),
                             "KEEPSAKE_MIN_FREE_GB", 20, 0, 100000),
            shrink_pct=_int(pick("shrink_pct", "KEEPSAKE_SHRINK_PCT"),
                            "KEEPSAKE_SHRINK_PCT", 20, 1, 99),
            pause_ms=_int(pick("pause_ms", "KEEPSAKE_PAUSE_MS"),
                          "KEEPSAKE_PAUSE_MS", 0, 0, 10000),
            max_mbps=_int(pick("max_mbps", "KEEPSAKE_MAX_MBPS"),
                          "KEEPSAKE_MAX_MBPS", 0, 0, 10000),
            tz=str(pick("tz", "TZ", "UTC") or "UTC"),
            fs_roots=frozenset(listing(pick("fs_roots", "KEEPSAKE_FS_ROOTS", []))),
            location_prefixes=tuple(listing(pick("location_prefixes",
                                                 "KEEPSAKE_LOCATION_PREFIXES", ["/hostfs"]))),
            root_markers=markers,
            host=str(pick("host", "KEEPSAKE_HOST", "127.0.0.1")),
            port=_int(pick("port", "KEEPSAKE_PORT"), "KEEPSAKE_PORT", 8765, 1, 65535),
            password=secret("password", "KEEPSAKE_PASSWORD"),
            cookie_secure=str(pick("cookie_secure", "KEEPSAKE_COOKIE_SECURE", "0")
                              ).lower() in ("1", "true", "yes"),
            allowed_hosts=tuple(h.lower() for h in listing(
                pick("allowed_hosts", "KEEPSAKE_ALLOWED_HOSTS", []))),
            webhook_url=secret("webhook_url", "KEEPSAKE_WEBHOOK_URL"),
            mqtt_host=str(pick("mqtt_host", "KEEPSAKE_MQTT_HOST", "") or ""),
            mqtt_port=_int(pick("mqtt_port", "KEEPSAKE_MQTT_PORT"),
                           "KEEPSAKE_MQTT_PORT", 1883, 1, 65535),
            mqtt_user=str(pick("mqtt_user", "KEEPSAKE_MQTT_USER", "") or ""),
            mqtt_password=secret("mqtt_password", "KEEPSAKE_MQTT_PASSWORD"),
            mqtt_prefix=str(pick("mqtt_prefix", "KEEPSAKE_MQTT_PREFIX", "keepsake")),
            mqtt_discovery_prefix=str(pick("mqtt_discovery_prefix",
                                           "KEEPSAKE_MQTT_DISCOVERY_PREFIX", "") or "").strip("/ "),
            ntfy_url=secret("ntfy_url", "KEEPSAKE_NTFY_URL"),
            ntfy_token=secret("ntfy_token", "KEEPSAKE_NTFY_TOKEN"),
            pushover_token=secret("pushover_token", "KEEPSAKE_PUSHOVER_TOKEN"),
            pushover_user=secret("pushover_user", "KEEPSAKE_PUSHOVER_USER"),
            smtp_host=str(pick("smtp_host", "KEEPSAKE_SMTP_HOST", "") or ""),
            smtp_port=_int(pick("smtp_port", "KEEPSAKE_SMTP_PORT"),
                           "KEEPSAKE_SMTP_PORT", 587, 1, 65535),
            smtp_user=str(pick("smtp_user", "KEEPSAKE_SMTP_USER", "") or ""),
            smtp_password=secret("smtp_password", "KEEPSAKE_SMTP_PASSWORD"),
            smtp_from=str(pick("smtp_from", "KEEPSAKE_SMTP_FROM", "") or "").strip(),
            smtp_to=tuple(listing(pick("smtp_to", "KEEPSAKE_SMTP_TO", []))),
            smtp_security=str(pick("smtp_security", "KEEPSAKE_SMTP_SECURITY",
                                   "starttls") or "starttls").lower(),
        )
        unknown = [m for m in cfg.fs_roots if m not in cfg.roots]
        unknown += [m for m in cfg.root_markers if m not in cfg.roots]
        if unknown:
            raise ConfigError(f"fs_roots / root_markers name an unknown root: "
                              f"{', '.join(sorted(set(unknown)))}")
        if cfg.webhook_url:
            check_webhook_url(cfg.webhook_url)
        check_alert_channels(cfg)
        return cfg

    def with_settings(self) -> "Config":
        """Apply the UI-editable settings stored in settings.json."""
        raw = load_json(self.settings_file, {})
        if not raw:
            return self
        return apply_settings(self, raw)


def check_webhook_url(url: str) -> str:
    if not re.match(r"^https?://[^\s/]+", url) or len(url) > 500:
        raise ConfigError("the webhook address must start with http:// or https://")
    return url


def check_alert_channels(cfg: Config) -> None:
    """Refuse a half-set channel with a sentence that names the variable but
    never a value."""
    if cfg.ntfy_url and not re.match(r"^https?://[^\s/]+", cfg.ntfy_url):
        raise ConfigError("KEEPSAKE_NTFY_URL must be the topic address, starting "
                          "with http:// or https://")
    if bool(cfg.pushover_token) != bool(cfg.pushover_user):
        raise ConfigError("Pushover needs both KEEPSAKE_PUSHOVER_TOKEN and "
                          "KEEPSAKE_PUSHOVER_USER")
    if cfg.smtp_host:
        if not cfg.smtp_from or not cfg.smtp_to:
            raise ConfigError("e-mail alerts need KEEPSAKE_SMTP_FROM and "
                              "KEEPSAKE_SMTP_TO as well as KEEPSAKE_SMTP_HOST")
        if cfg.smtp_security not in SMTP_SECURITY:
            raise ConfigError(f"KEEPSAKE_SMTP_SECURITY must be one of: "
                              f"{', '.join(SMTP_SECURITY)}")
        if bool(cfg.smtp_user) != bool(cfg.smtp_password):
            raise ConfigError("e-mail login needs both KEEPSAKE_SMTP_USER and "
                              "KEEPSAKE_SMTP_PASSWORD")
        if cfg.smtp_user and cfg.smtp_security == "none":
            raise ConfigError("Keepsake will not send an e-mail password without "
                              "encryption: use KEEPSAKE_SMTP_SECURITY=starttls or "
                              "ssl")
        for addr in (cfg.smtp_from, *cfg.smtp_to):
            if not re.match(r"^[^\s@<>]+@[^\s@<>]+$", addr):
                raise ConfigError("KEEPSAKE_SMTP_FROM and KEEPSAKE_SMTP_TO must "
                                  "be plain e-mail addresses")


def apply_settings(cfg: Config, raw: Any) -> Config:
    """Validate a settings object and return the Config with it applied."""
    if not isinstance(raw, dict):
        raise ConfigError("settings must be a JSON object")
    changes: Dict[str, Any] = {}
    for key, low, high in (("max_dest_gb", 1, 100000), ("min_free_gb", 0, 100000),
                           ("shrink_pct", 1, 99), ("pause_ms", 0, 10000),
                           ("max_mbps", 0, 10000)):
        if key in raw:
            changes[key] = _int(raw[key], key, getattr(cfg, key), low, high)
    if "webhook_url" in raw:
        url = str(raw["webhook_url"] or "").strip()
        changes["webhook_url"] = check_webhook_url(url) if url else ""
    if "notify_on" in raw:
        events = raw["notify_on"]
        if (not isinstance(events, list)
                or any(e not in NOTIFY_EVENTS for e in events)):
            raise ConfigError(f"notify_on must be a list of: {', '.join(NOTIFY_EVENTS)}")
        changes["notify_on"] = tuple(events)
    for key in ("pause_until", "pause_ended"):
        if key in raw:
            changes[key] = _iso_instant(raw[key], key)
    for key in ("quiet_start", "quiet_end"):
        if key in raw:
            value = str(raw[key] or "")
            if value and not TIME_RE.match(value):
                raise ConfigError(f"{key} must look like 22:00")
            changes[key] = value
    out = replace(cfg, **changes)
    if bool(out.quiet_start) != bool(out.quiet_end):
        raise ConfigError("quiet hours need both a start and an end time")
    if out.quiet_start and out.quiet_start == out.quiet_end:
        raise ConfigError("quiet hours start and end at the same time, which "
                          "would cover all day or none of it; to stop everything "
                          "use Pause everything instead")
    return out


def _iso_instant(value: Any, name: str) -> str:
    """A stored point in time: "" or an ISO date-time that carries an offset."""
    if value in (None, ""):
        return ""
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a date and time") from exc
    if when.tzinfo is None:
        raise ConfigError(f"{name} must include a time zone offset")
    return when.isoformat(timespec="seconds")


@dataclass
class Job:
    name: str
    root: str
    source: str
    dest: str
    # The iCloud folder that holds this job's folder, when several source folders
    # are kept into one destination: the copy then lives in Keepsake/<group>/<dest>.
    # Empty for a folder straight under Keepsake, which is how every job was
    # stored before this existed.
    group: str = ""
    excludes: List[str] = field(default_factory=list)
    schedule: Dict[str, Any] = field(default_factory=lambda: {"every_hours": 24})
    mode: str = DEFAULT_MODE
    keep_days: int = DEFAULT_KEEP_DAYS
    max_gb: Optional[int] = None
    enabled: bool = True
    # Take over a destination folder that this service did not create. Off by
    # default: an unmarked, non-empty folder is refused.
    adopt_existing: bool = False
    # "Check a copy automatically": off, daily, weekly or monthly. A folder
    # stored before this existed has no value and stays off until it is set.
    auto_check: str = "off"
    # Alert when no run has succeeded for this many days (0 = never). The alert
    # waits at least twice the folder's own period, so a weekly folder is not
    # called stale after three days.
    stale_days: int = DEFAULT_STALE_DAYS

    @property
    def dest_parts(self) -> List[str]:
        """The folder names from the Keepsake folder down to this job's copy."""
        return [self.group, self.dest] if self.group else [self.dest]

    @property
    def dest_path(self) -> str:
        return "/".join(self.dest_parts)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "root": self.root, "source": self.source,
            "dest": self.dest, "group": self.group, "excludes": list(self.excludes),
            "schedule": dict(self.schedule), "mode": self.mode,
            "keep_days": self.keep_days, "max_gb": self.max_gb,
            "enabled": self.enabled, "adopt_existing": self.adopt_existing,
            "auto_check": self.auto_check, "stale_days": self.stale_days,
        }


def clean_relative(value: Any, what: str, allow_empty: bool = False) -> str:
    """A relative path with no escape: absolute, `..`, NUL and backslash refused."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_empty:
            return ""
        raise ConfigError(f"{what} is missing")
    if not isinstance(value, str):
        raise ConfigError(f"{what} must be text")
    text = value.strip().strip("/")
    if "\x00" in text or "\\" in text:
        raise ConfigError(f"{what} contains a character that is not allowed")
    if value.strip().startswith("/"):
        raise ConfigError(f"{what} must be relative to the chosen location, "
                          f"not an absolute path")
    parts = text.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ConfigError(f"{what} may not contain '..' or empty parts")
    return "/".join(parts)


def looks_secret(path: str) -> bool:
    low = path.lower()
    return any(word in low for word in SECRET_WORDS)


def validate_job(raw: Any, cfg: Config, existing: List[Job],
                 replacing: Optional[str] = None) -> Job:
    """Turn a payload into a Job or raise ConfigError in plain words."""
    if not isinstance(raw, dict):
        raise ConfigError("the job must be a JSON object")
    name = str(raw.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise ConfigError("name must be 1-40 characters: letters, digits and "
                          "hyphens (no spaces), starting with a letter or digit")
    root = str(raw.get("root", "")).strip()
    if root not in cfg.roots:
        raise ConfigError(f"location {root!r} is not one this service may read "
                          f"(allowed: {', '.join(sorted(cfg.roots)) or 'none'})")
    source = clean_relative(raw.get("source"), "source folder")
    if looks_secret(source):
        raise ConfigError(f"{source!r} looks like it holds secrets or keys; "
                          f"nothing secret may be kept in iCloud")
    dest = str(raw.get("dest") or name).strip()
    if not NAME_RE.match(dest):
        raise ConfigError("destination folder name must be letters, digits and "
                          "hyphens (no spaces)")
    group = raw.get("group") or ""
    if not isinstance(group, str):
        raise ConfigError("the destination folder must be text")
    group = group.strip()
    if group and not NAME_RE.match(group):
        raise ConfigError("destination folder name must be letters, digits and "
                          "hyphens (no spaces)")
    excludes = raw.get("excludes") or []
    if (not isinstance(excludes, list) or len(excludes) > 50
            or any(not isinstance(e, str) or not e or len(e) > 200
                   or "\x00" in e for e in excludes)):
        raise ConfigError("excludes must be a list of up to 50 short patterns")
    schedule = raw.get("schedule") or {"every_hours": 24}
    if not isinstance(schedule, dict):
        raise ConfigError("schedule must be {every_hours: N} or {at: 'HH:MM'}")
    if "at" in schedule:
        if not TIME_RE.match(str(schedule["at"])):
            raise ConfigError("schedule time must look like 03:30")
        schedule = {"at": str(schedule["at"])}
    else:
        try:
            hours = int(schedule.get("every_hours", 24))
        except (TypeError, ValueError) as exc:
            raise ConfigError("schedule every_hours must be a number") from exc
        if not 1 <= hours <= 168:
            raise ConfigError("schedule every_hours must be 1 to 168")
        schedule = {"every_hours": hours}
    mode = str(raw.get("mode") or DEFAULT_MODE)
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {', '.join(MODES)}")
    try:
        keep_days = int(raw.get("keep_days", DEFAULT_KEEP_DAYS))
    except (TypeError, ValueError) as exc:
        raise ConfigError("keep_days must be a number") from exc
    if not 1 <= keep_days <= 365:
        raise ConfigError("keep_days must be 1 to 365")
    max_gb = raw.get("max_gb")
    if max_gb in ("", None):
        max_gb = None
    else:
        try:
            max_gb = int(max_gb)
        except (TypeError, ValueError) as exc:
            raise ConfigError("max_gb must be a number") from exc
        if not 1 <= max_gb <= 2000:
            raise ConfigError("max_gb must be 1 to 2000")
    adopt = raw.get("adopt_existing", False)
    if not isinstance(adopt, bool):
        raise ConfigError("adopt_existing must be true or false")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("enabled must be true or false")
    auto_check = raw.get("auto_check", "off")
    if auto_check not in AUTO_CHECKS:
        raise ConfigError(f"auto_check must be one of: {', '.join(AUTO_CHECKS)}")
    stale_days = raw.get("stale_days", DEFAULT_STALE_DAYS)
    if isinstance(stale_days, bool) or not isinstance(stale_days, int) \
            or not 0 <= stale_days <= 365:
        raise ConfigError("stale_days must be a whole number from 0 to 365")
    job = Job(name=name, root=root, source=source, dest=dest, group=group,
              excludes=excludes,
              schedule=schedule, mode=mode, keep_days=keep_days, max_gb=max_gb,
              enabled=enabled, adopt_existing=adopt, auto_check=auto_check,
              stale_days=stale_days)
    for other in existing:
        if other.name == replacing:
            continue
        if other.name.lower() == name.lower():
            raise ConfigError(f"a folder job named {other.name!r} already exists "
                              f"(names that differ only by case count as the same)")
        if other.dest_path.lower() == job.dest_path.lower():
            raise ConfigError(f"iCloud folder {job.dest_path!r} is already used by "
                              f"job {other.name!r}")
        # A folder that holds one job's files directly cannot also hold other
        # folders, and the other way round: the copies would be mixed.
        if group and not other.group and other.dest.lower() == group.lower():
            raise ConfigError(f"iCloud folder {group!r} already holds the copy of "
                              f"job {other.name!r} directly, so other folders "
                              f"cannot be put inside it. Choose another destination")
        if not group and other.group.lower() == dest.lower():
            raise ConfigError(f"iCloud folder {dest!r} already holds folders from "
                              f"other jobs, so one folder cannot be copied straight "
                              f"into it. Choose another destination")
        if other.root == root and (other.source == source
                                   or other.source.startswith(source + "/")
                                   or source.startswith(other.source + "/")):
            raise ConfigError(f"{source!r} overlaps job {other.name!r}; the "
                              f"same files would be copied twice")
    return job


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path.name} is unreadable: {exc}") from exc


class JobStore:
    """jobs.json: the only place a job exists. Writes are atomic."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def load(self) -> List[Job]:
        raw = load_json(self.cfg.jobs_file, {"jobs": []})
        jobs: List[Job] = []
        for item in raw.get("jobs", []):
            # Validated on read as well as on write: a hand edit that escapes a
            # root must be refused here, not discovered at copy time.
            jobs.append(validate_job(item, self.cfg, jobs))
        return jobs

    def save(self, jobs: List[Job]) -> None:
        atomic_write_json(self.cfg.jobs_file,
                          {"jobs": [j.as_dict() for j in jobs]})
