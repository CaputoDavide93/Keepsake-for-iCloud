"""The brain: jobs, schedule, actions, status and the activity log.

One worker runs jobs strictly one at a time (iCloud is slow enough already). A
second request for a job that is queued or running is refused with a sentence,
and every action either succeeds with a message or raises ``ConfigError`` with
the reason, so a refusal can never be mistaken for success.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from keepsake import engine, locations
from keepsake.config import (
    DEFAULT_AUTO_CHECK,
    Config,
    ConfigError,
    Job,
    JobStore,
    apply_settings,
    atomic_write_json,
    clean_relative,
    load_json,
    looks_secret,
    validate_job,
)
from keepsake.engine import (
    REMOVED_DIR,
    REMOVED_KEEP_DAYS,
    VERIFY_FILES,
    PreviewReport,
    Refused,
    RunReport,
    anchor_problem,
    list_deleted,
    plan_restore_batch,
    preview_job,
    prune_deleted,
    restore_batch,
    restore_deleted,
    retire_job_folder,
    run_job,
    verify_copy,
    write_anchor,
)
from keepsake.hamqtt import StatusPublisher
from keepsake.notifier import build_notifiers
from keepsake.progress import Cancelled, Progress, tracking

logger = logging.getLogger("keepsake")

BROWSE_CAP = 500
RETRY_AFTER_FAILURE = timedelta(hours=1)
ACTIVITY_KEEP = 500
ACTIVITY_MAX = 1000
HISTORY_DAYS = 14
OUTCOMES = ("copied", "nothing", "paused")
DETAIL_SAMPLES = 50            # example paths kept per night
DETAIL_PATH_MAX = 300
DETAIL_ERROR_MAX = 300
PAUSE_MAX = timedelta(days=366)
PAUSE_PRESETS = ("1h", "tomorrow", "1w")
TOMORROW_AT = dtime(8, 0)      # "until tomorrow 08:00"
EVICTED_NOTE = ("macOS Optimize Storage has replaced {n} of this folder's files "
                "with placeholders to free space on this Mac. macOS normally does "
                "that only to files iCloud already holds, so they are not lost, "
                "but they are not on this Mac's disk: open one in Finder to "
                "download it, and a restore from this folder needs them "
                "downloaded first. Keepsake does not re-copy them while the "
                "source is unchanged")
CHECK_PERIODS = {"daily": timedelta(days=1), "weekly": timedelta(days=7),
                 "monthly": timedelta(days=30)}
CHECK_RETRY = timedelta(hours=6)   # after a check that could not compare anything


class CheckRefused(ConfigError):
    """A check that the engine refused (nothing to compare, share unavailable)."""


def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def next_due(job: Job, last_attempt: Optional[datetime], last_ok: bool,
             now: datetime, tz: tzinfo) -> datetime:
    """When a job should next run. Never-run jobs are due now."""
    if last_attempt is None:
        return now
    if "at" in job.schedule:
        hour, minute = (int(x) for x in job.schedule["at"].split(":"))
        wall = last_attempt.astimezone(tz).replace(tzinfo=None)
        cand = wall.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand.replace(tzinfo=tz) <= last_attempt:
            cand += timedelta(days=1)
        due = cand.replace(tzinfo=tz)
    else:
        due = last_attempt + timedelta(hours=job.schedule["every_hours"])
    if not last_ok:
        due = min(due, last_attempt + RETRY_AFTER_FAILURE)
    return due


def change_sentence(st: dict, job: Job) -> str:
    """The one plain sentence for what the last run changed. Counts only: no
    file names (those are on the row and the Activity page, not in the log)."""
    if "files_added" not in st or st.get("result") not in ("ok", "failed"):
        return ""
    parts = []
    if st.get("files_added"):
        parts.append(f"{st['files_added']} added")
    if st.get("files_updated"):
        parts.append(f"{st['files_updated']} updated")
    if st.get("files_removed"):
        parts.append(f"{st['files_removed']} moved to .deleted"
                     if job.mode == "mirror-keep-deleted"
                     or st.get("safe_first_run")
                     else f"{st['files_removed']} removed")
    if st.get("result") == "ok":
        return ", ".join(parts) if parts else "nothing changed"
    return ("before it stopped: " + ", ".join(parts)) if parts else ""


def run_outcome(report: RunReport) -> str:
    """What one finished run means for the nights strip, from its own report.

    copied: it completed and added, updated or moved something. nothing: it
    completed and found no change. paused: it was refused, failed or a guard
    tripped. Only a run that completed can ever be copied or nothing."""
    if report.result != "ok" or report.error:
        return "paused"
    if (report.files_added or report.files_updated or report.files_removed
            or report.files_copied or report.files_moved_to_deleted):
        return "copied"
    return "nothing"


def run_detail(report: RunReport, job: Job) -> dict:
    """What one run did, for the night it belongs to: counts, up to
    DETAIL_SAMPLES example paths and, for a run that did not finish, the
    sentence it gave. Bounded, so 14 nights stay small."""
    kept = job.mode == "mirror-keep-deleted" or report.safe_first_run
    detail: Dict[str, Any] = {
        "at": report.started, "runs": 1,
        "added": int(report.files_added), "updated": int(report.files_updated),
        "removed": int(report.files_removed),
        "removed_how": "moved to .deleted" if kept else "removed",
        "samples": [{"kind": x["kind"], "path": str(x["path"])[:DETAIL_PATH_MAX]}
                    for x in report.change_samples[:DETAIL_SAMPLES]],
    }
    if report.error:
        detail["error"] = report.error[:DETAIL_ERROR_MAX]
    return detail


def clean_detail(raw: Any) -> Optional[dict]:
    """A stored night detail, or None when it is missing or damaged (a night
    recorded before details existed has none). Re-bounded on every read."""
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {}
    for key in ("added", "updated", "removed"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        out[key] = value
    runs = raw.get("runs", 1)
    out["runs"] = runs if isinstance(runs, int) and not isinstance(runs, bool) \
        and runs >= 1 else 1
    out["removed_how"] = ("removed" if raw.get("removed_how") == "removed"
                          else "moved to .deleted")
    if _parse(raw.get("at")):
        out["at"] = raw["at"]
    samples = []
    for item in raw.get("samples") if isinstance(raw.get("samples"), list) else []:
        if (isinstance(item, dict) and item.get("kind") in ("added", "updated", "removed")
                and isinstance(item.get("path"), str)):
            samples.append({"kind": item["kind"], "path": item["path"][:DETAIL_PATH_MAX]})
        if len(samples) >= DETAIL_SAMPLES:
            break
    out["samples"] = samples
    if isinstance(raw.get("error"), str) and raw["error"]:
        out["error"] = raw["error"][:DETAIL_ERROR_MAX]
    return out


def _combine_detail(old: Optional[dict], new: Optional[dict]) -> Optional[dict]:
    """Two copying runs on one day: add the counts, keep the first examples."""
    if not old or not new:
        return new
    both = {k: old[k] + new[k] for k in ("added", "updated", "removed")}
    return {**new, **both, "runs": old["runs"] + new["runs"],
            "samples": (old["samples"] + new["samples"])[:DETAIL_SAMPLES]}


def merge_history(history: Any, day: str, outcome: str, today: str,
                  detail: Optional[dict] = None) -> List[dict]:
    """Add one run to the per-day history: one entry per local day, the last
    HISTORY_DAYS days only. Within a day the latest run decides, except that a
    later run with no changes does not hide an earlier copy. Two copies on one
    day add up in the detail. Anything that is not a well-formed entry (an
    older state file, a damaged one) is dropped."""
    entries = clean_history(history, today)
    detail = clean_detail(detail)
    for entry in entries:
        if entry["day"] == day:
            if outcome == "nothing" and entry["outcome"] == "copied":
                break
            if outcome == "copied" and entry["outcome"] == "copied":
                detail = _combine_detail(entry.get("detail"), detail)
            entry["outcome"] = outcome
            entry.pop("detail", None)
            if detail:
                entry["detail"] = detail
            break
    else:
        entry = {"day": day, "outcome": outcome}
        if detail:
            entry["detail"] = detail
        entries.append(entry)
    entries.sort(key=lambda e: e["day"])
    return entries[-HISTORY_DAYS:]


def clean_history(history: Any, today: str) -> List[dict]:
    """The stored history, tolerant of a missing key or junk, limited to the
    window that ends on `today` (a local YYYY-MM-DD)."""
    if not isinstance(history, list):
        return []
    try:
        first = (datetime.fromisoformat(today).date()
                 - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    except ValueError:
        return []
    seen: Dict[str, Tuple[str, Optional[dict]]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        day, outcome = entry.get("day"), entry.get("outcome")
        if outcome not in OUTCOMES or not isinstance(day, str):
            continue
        try:
            datetime.fromisoformat(day)
        except ValueError:
            continue
        if first <= day <= today:
            seen[day] = (outcome, clean_detail(entry.get("detail")))
    out = []
    for d in sorted(seen):
        item: Dict[str, Any] = {"day": d, "outcome": seen[d][0]}
        if seen[d][1]:
            item["detail"] = seen[d][1]
        out.append(item)
    return out


def _minutes(text: str) -> int:
    hour, minute = (int(x) for x in text.split(":"))
    return hour * 60 + minute


def quiet_end_after(when: datetime, start: str, end: str,
                    tz: tzinfo) -> Optional[datetime]:
    """When the daily quiet window ends if `when` is inside it, else None. The
    window may cross midnight (22:00 to 07:00); start is inside, end is not."""
    if not start or not end:
        return None
    local = when.astimezone(tz)
    s, e, m = _minutes(start), _minutes(end), local.hour * 60 + local.minute
    if s < e:
        inside, day = s <= m < e, local.date()
    else:
        inside = m >= s or m < e
        day = local.date() + timedelta(days=1) if m >= s else local.date()
    if not inside:
        return None
    return datetime.combine(day, dtime(e // 60, e % 60), tzinfo=tz)


def quiet_overlap(first: datetime, last: datetime, start: str, end: str,
                  tz: tzinfo) -> timedelta:
    """How much of [first, last] lies inside the daily quiet window."""
    if not start or not end or last <= first:
        return timedelta(0)
    s, e = _minutes(start), _minutes(end)
    day = first.astimezone(tz).date() - timedelta(days=1)
    stop = last.astimezone(tz).date()
    total = timedelta(0)
    while day <= stop:
        begin = datetime.combine(day, dtime(s // 60, s % 60), tzinfo=tz)
        finish = datetime.combine(day + timedelta(days=1 if s >= e else 0),
                                  dtime(e // 60, e % 60), tzinfo=tz)
        total += max(timedelta(0), min(last, finish) - max(first, begin))
        day += timedelta(days=1)
    return total


def next_check(job: Job, st: dict, now: datetime) -> Optional[datetime]:
    """When the scheduled "check a copy" is next due, or None when it is off,
    the folder is paused, or nothing has been copied yet (there is nothing to
    check). A folder that has never been checked is due at once; after a check
    that could compare nothing it is due again in a few hours, not a whole
    period."""
    if job.auto_check not in CHECK_PERIODS or not job.enabled \
            or not st.get("last_success"):
        return None
    verified = _parse((st.get("verify") or {}).get("at"))
    attempt = _parse((st.get("auto_check") or {}).get("at"))
    if attempt and (not verified or attempt >= verified) \
            and (st.get("auto_check") or {}).get("result") == "refused":
        return attempt + CHECK_RETRY
    last = max((t for t in (verified, attempt) if t), default=None)
    return now if last is None else last + CHECK_PERIODS[job.auto_check]


def period_of(job: Job) -> timedelta:
    return (timedelta(days=1) if "at" in job.schedule
            else timedelta(hours=job.schedule["every_hours"]))


class Service:
    def __init__(self, cfg: Config, notifiers: Optional[List[Any]] = None,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.cfg = cfg
        self.notifiers = build_notifiers(cfg) if notifiers is None else notifiers
        # Live status for a home automation hub, only with an MQTT broker and a discovery prefix.
        self.status_publisher = (StatusPublisher(cfg, self)
                                 if cfg.mqtt_host and cfg.mqtt_discovery_prefix else None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tz = zone(cfg.tz)
        self.lock = threading.RLock()
        self.queue: List[Tuple[str, bool]] = []   # (job name, accept_shrink)
        # Names in `queue` that the schedule put there (a person's Sync now is
        # never in it): quiet hours and a pause drop these, never the others.
        self.scheduled: set = set()
        # Previews wait here and run on the same worker, one job at a time.
        self.preview_queue: List[Tuple[str, bool]] = []
        # The last preview per folder, in memory only: a preview writes nothing.
        self.previews: Dict[str, dict] = {}
        self.running: Optional[str] = None
        self.running_kind = "sync"               # or "preview" while one runs
        self.progress = ""
        self.track: Optional[Progress] = None    # what the running job is doing, live
        # Work done in a request thread or by the check worker (a check, a restore):
        # job name -> (operation, tracker), so the page can show it and stop it.
        self.ops: Dict[str, Tuple[str, Progress]] = {}
        self.state: Dict[str, dict] = load_json(cfg.state_file, {})
        # The guided setup: {"started", "complete", "existing"}. Once "complete"
        # is true it stays true, whatever happens to the folders afterwards.
        self.setup: Dict[str, Any] = load_json(cfg.setup_file, {})
        self.wake = threading.Event()
        self.verify_lock = threading.Lock()     # one "check a copy" at a time
        self.restore_lock = threading.Lock()    # one restore at a time (cap and reserve checks)
        self.last_beat = time.monotonic()
        self.started = time.monotonic()         # for the uptime in the diagnostics
        # Locations set by KEEPSAKE_ROOTS are built in; the page adds and removes the rest.
        self.builtin_roots = frozenset(cfg.roots)
        locations.merge_stored(cfg)
        # Settle the guided setup now, at start-up, so "an install from before
        # the wizard" is judged on what was there before anyone could add a folder.
        self.setup_complete()

    @property
    def store(self) -> JobStore:
        return JobStore(self.cfg)

    # -- activity ------------------------------------------------------------
    def log_activity(self, message: str, job: str = "", level: str = "info") -> None:
        entry = {"at": self.clock().isoformat(timespec="seconds"),
                 "level": level, "job": job, "message": message}
        logger.info("[%s] %s", job or "-", message)
        path = self.cfg.activity_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as handle:
                handle.write(json.dumps(entry) + "\n")
            if path.stat().st_size > ACTIVITY_MAX * 400:
                lines = path.read_text().splitlines()
                if len(lines) > ACTIVITY_MAX:
                    path.write_text("\n".join(lines[-ACTIVITY_KEEP:]) + "\n")
        except OSError:
            logger.warning("could not write the activity log")

    def activity(self, limit: int = 100) -> List[dict]:
        limit = max(1, min(int(limit), ACTIVITY_MAX))
        try:
            lines = self.cfg.activity_file.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines[-limit:]):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def notify(self, event: Dict[str, Any]) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(event)
            except Exception:  # noqa: BLE001
                logger.warning("a notifier raised; ignored")

    # -- status --------------------------------------------------------------
    def _save_state(self) -> None:
        atomic_write_json(self.cfg.state_file, self.state)

    def _jobs(self) -> List[Job]:
        try:
            return self.store.load()
        except ConfigError as exc:
            logger.error("jobs.json refused: %s", exc)
            return []

    def today(self) -> str:
        """The current day in the configured time zone, as YYYY-MM-DD."""
        return self.clock().astimezone(self.tz).date().isoformat()

    # -- pause everything and quiet hours --------------------------------------
    def _pause_until(self, now: datetime) -> Optional[datetime]:
        until = _parse(self.cfg.pause_until)
        return until if until and now < until else None

    def resume_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """When scheduled syncs and checks may start again, or None when they may
        start now. A pause that ends inside the quiet window waits for it to end."""
        now = now or self.clock()
        at = self._pause_until(now)
        probe = at or now
        quiet_end = quiet_end_after(probe, self.cfg.quiet_start, self.cfg.quiet_end,
                                    self.tz)
        if quiet_end:
            return quiet_end
        return at

    def gated(self) -> bool:
        return self.resume_at() is not None

    def pause_view(self) -> dict:
        now = self.clock()
        until = self._pause_until(now)
        resume = self.resume_at(now)
        in_quiet = bool(quiet_end_after(now, self.cfg.quiet_start,
                                        self.cfg.quiet_end, self.tz))
        kind = "pause" if until else "quiet" if in_quiet else ""
        stamp = resume.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if resume else ""
        message = ""
        if kind:
            message = ((f"Everything is paused until {stamp} ({self.cfg.tz}). "
                        if kind == "pause"
                        else f"Quiet hours until {stamp} ({self.cfg.tz}). ")
                       + "Scheduled syncs and checks do not start; a sync that "
                         "is already running is not interrupted; Sync now and "
                         "Preview still work.")
        return {"paused": bool(resume), "kind": kind,
                "until": resume.isoformat(timespec="seconds") if resume else None,
                "pause_until": until.isoformat(timespec="seconds") if until else None,
                "quiet": ({"start": self.cfg.quiet_start, "end": self.cfg.quiet_end}
                          if self.cfg.quiet_start else None),
                "in_quiet": in_quiet, "message": message, "timezone": self.cfg.tz}

    def _last_pause_end(self, now: datetime) -> Optional[datetime]:
        ends = [_parse(self.cfg.pause_ended)]
        until = _parse(self.cfg.pause_until)
        if until and until <= now:
            ends.append(until)
        return max((e for e in ends if e), default=None)

    def _awake(self, since: datetime, now: datetime) -> timedelta:
        """Time since `since` that counts towards "no run for too long": a pause
        and the daily quiet window are not the folder's fault, so they do not."""
        end = self._last_pause_end(now)
        if end and end > since:
            since = min(end, now)
        elapsed = now - since - quiet_overlap(since, now, self.cfg.quiet_start,
                                              self.cfg.quiet_end, self.tz)
        return max(elapsed, timedelta(0))

    def _operation_of(self, name: str) -> Optional[str]:
        """What is happening to a folder right now, from the server's own state:
        sync, preview, check, restore, or None."""
        if self.running == name and self.track is not None:
            return self.running_kind
        op = self.ops.get(name)
        return op[0] if op else None

    def _progress_detail(self, name: str) -> Optional[dict]:
        if self.running == name and self.track is not None:
            return self.track.snapshot()
        op = self.ops.get(name)
        return op[1].snapshot() if op else None

    @contextmanager
    def _operation(self, name: str, op: str) -> Iterator[Progress]:
        """Make an operation visible (and stoppable) for as long as it runs."""
        track = Progress(op=op)
        with self.lock:
            self.ops[name] = (op, track)
        try:
            yield track
        finally:
            with self.lock:
                if self.ops.get(name, (None, None))[1] is track:
                    self.ops.pop(name, None)

    def _busy(self, name: str) -> bool:
        return (self.running == name
                or any(n == name for n, _ in self.queue)
                or any(n == name for n, _ in self.preview_queue))

    def job_view(self, job: Job) -> dict:
        st = self.state.get(job.name, {})
        last = _parse(st.get("last_run"))
        due = next_due(job, last, st.get("result") == "ok", self.clock(), self.tz)
        resume = self.resume_at()
        if resume and due < resume:
            due = resume          # a scheduled sync cannot start before this
        queued = any(n == job.name for n, _ in self.queue)
        previewing = (self.running == job.name and self.running_kind == "preview") \
            or any(n == job.name for n, _ in self.preview_queue)
        check = st.get("verify")
        # A check that found a difference is a problem until a later check passes.
        bad_check = bool(check and check.get("state") == "mismatch")
        status = ("running" if self.running == job.name and not previewing
                  else "queued" if queued
                  else "paused" if not job.enabled
                  else "failed" if bad_check
                  else st.get("result", "never"))
        nxt = next_check(job, st, self.clock())
        if nxt and resume and nxt < resume:
            nxt = resume          # no scheduled check starts before this
        error = st.get("error", "")
        if bad_check and st.get("result") == "ok":
            error = "The last copy check found a difference. " + check["message"]
        return {
            **job.as_dict(),
            "status": status,
            "last_run": st.get("last_run"),
            "last_success": st.get("last_success"),
            "result": st.get("result", "never"),
            "error": error,
            "verify": check,
            "next_check": (nxt.isoformat(timespec="seconds") if nxt else None),
            "check_rounds": (st.get("verify_cursor") or {}).get("rounds", 0),
            "check_cycle_checks": (math.ceil(st["files_total"] / VERIFY_FILES)
                                   if st.get("files_total") else None),
            "icloud": {"evicted": st.get("evicted"),
                       "note": (EVICTED_NOTE.format(n=st["evicted"])
                                if st.get("evicted") else "")},
            "history": clean_history(st.get("history"), self.today()),
            "preview": self.previews.get(job.name),
            "previewing": previewing,
            "changes": ({"summary": change_sentence(st, job),
                         "added": st.get("files_added", 0),
                         "updated": st.get("files_updated", 0),
                         "removed": st.get("files_removed", 0),
                         "samples": st.get("change_samples", [])}
                        if "files_added" in st else None),
            "files_copied": st.get("files_copied", 0),
            "bytes_copied": st.get("bytes_copied", 0),
            "duration_s": st.get("duration_s", 0),
            "files_total": st.get("files_total", 0),
            "icloud_bytes": st.get("dest_bytes", 0),
            "moved_to_deleted": st.get("files_moved_to_deleted", 0),
            "notes": st.get("notes", []),
            "next_run": due.isoformat(timespec="seconds") if job.enabled else None,
            "progress": self.progress if self.running == job.name else "",
            "progress_detail": self._progress_detail(job.name),
            "operation": self._operation_of(job.name),
            "warnings": (["some files looked like secrets and were skipped: "
                          + ", ".join(st["secrets_skipped"][:3])]
                         if st.get("secrets_skipped") else []),
        }

    def attention(self, jobs: List[Job]) -> List[dict]:
        """Jobs a person must hear about: failed, skipped or silent too long.

        Nothing is an alarm until no run has succeeded for twice the job's
        period (floor 6 h): a share that is down for the night is not news, a
        share that has been down for a day is. Green runs never appear.
        """
        now = self.clock()
        paused = self.gated()
        out = []
        for job in jobs:
            if not job.enabled:
                continue
            st = self.state.get(job.name, {})
            check = st.get("verify")
            if check and check.get("state") == "mismatch":
                out.append({"job": job.name, "reason": check["message"]})
                continue
            ok_at = _parse(st.get("last_success"))
            since = ok_at or _parse(st.get("created")) or now
            grace = max(period_of(job) * 2, timedelta(hours=6))
            if self._awake(since, now) <= grace:
                continue
            result = st.get("result")
            if result in ("failed", "skipped"):
                out.append({"job": job.name, "reason": st.get("error") or result})
            elif result in ("ok", None) and not paused:
                # While everything is paused a folder is not late, it is waiting.
                out.append({"job": job.name, "reason": "has not run on time"})
        return out

    def roots_view(self) -> List[dict]:
        out = []
        for name, path in sorted(self.cfg.roots.items()):
            live, why = engine.root_live(self.cfg, name)
            out.append({"name": name, "path": str(path), "available": live,
                        "problem": why,
                        "marker": self.cfg.root_markers.get(name, ""),
                        "checks_mount": name in self.cfg.fs_roots})
        return out

    def status(self) -> dict:
        with self.lock:
            jobs = self._jobs()
            views = [self.job_view(j) for j in jobs]
            attn = self.attention(jobs)
            icloud_ok = self.cfg.dest_root.is_dir()
            return {
                "updated": self.clock().isoformat(timespec="seconds"),
                "today": self.today(),
                "jobs": views,
                "attention": attn,
                "attention_count": len(attn),
                "running": self.running if self.running_kind == "sync" else None,
                "pause": self.pause_view(),
                "first_run": self.first_run(views, icloud_ok),
                "setup": self.setup_view(views, icloud_ok),
                "icloud_bytes": sum(v["icloud_bytes"] for v in views),
                "icloud_available": icloud_ok,
                "anchor_ok": icloud_ok and not anchor_problem(self.cfg.dest_root),
                "anchor_note": "" if not icloud_ok else anchor_problem(self.cfg.dest_root),
                "roots": self.roots_view(),
                "caps": self.caps(),
            }

    def first_run(self, views: List[dict], icloud_ok: bool) -> dict:
        """The three first-run steps, each ticked from what is really there: the
        marker file in the iCloud folder, a stored folder, and a folder whose
        last run finished and whose last copy check matched. Never a flag a
        person sets. `done` is true when all three are."""
        confirmed = icloud_ok and not anchor_problem(self.cfg.dest_root)
        checked = any(v["last_success"] and (v["verify"] or {}).get("state") == "match"
                      for v in views)
        steps = [{"id": "confirm", "done": confirmed},
                 {"id": "folder", "done": bool(views)},
                 {"id": "sync-check", "done": checked}]
        return {"steps": steps, "done": all(x["done"] for x in steps)}

    def setup_view(self, views: List[dict], icloud_ok: bool) -> dict:
        """Whether the guided setup still has to be done, and at which step.

        While it is not complete the web page shows only the wizard and the REST
        API refuses everything outside it (see `server.setup_allows`). The answer
        is read from what is really there, like `first_run`, never from a flag a
        person sets: setup is complete when the three first-run steps are true.
        Two things are remembered in setup.json, because they cannot be read from
        the folders:

        * `complete` stays true once it was, so the wizard never comes back
          (removing every folder later does not lock the page again);
        * the FIRST time this is judged (when the service starts), an install that
          already has the confirmed iCloud folder and a folder is an install from
          before the wizard: it is complete (`existing`), not locked. Judged later,
          the same state is a person part-way through the wizard, so `started` is
          written the first time and ends that exception.
        """
        fr = self.first_run(views, icloud_ok)
        done = {x["id"]: x["done"] for x in fr["steps"]}
        before = dict(self.setup)
        if not self.setup.get("complete"):
            if fr["done"]:
                self.setup["complete"] = True
            elif not self.setup.get("started"):
                if done["confirm"] and views:
                    self.setup.update(complete=True, existing=True)
                else:
                    self.setup["started"] = True
        if self.setup != before:
            atomic_write_json(self.cfg.setup_file, self.setup)
        if self.setup.get("complete"):
            step = "complete"
        elif not done["confirm"]:
            step = "confirm"
        elif not done["folder"]:
            step = "folder"
        elif any(v["enabled"] and not v["last_success"] and not v["preview"]
                 for v in views):
            step = "preview"
        else:
            step = "sync-check"
        return {"complete": bool(self.setup.get("complete")), "step": step,
                "existing": bool(self.setup.get("existing"))}

    def setup_complete(self) -> bool:
        """The cheap question the REST API asks before each call."""
        if self.setup.get("complete"):
            return True
        with self.lock:
            views = [self.job_view(j) for j in self._jobs()]
            return self.setup_view(views, self.cfg.dest_root.is_dir())["complete"]

    def never_synced(self, name: str) -> bool:
        """True for a folder that has not made a copy yet: removing it loses nothing."""
        with self.lock:
            return not self.state.get(name, {}).get("last_success")

    def confirm_icloud(self) -> str:
        """The explicit one-time step: a person says this is the real iCloud folder."""
        with self.lock:
            try:
                msg = write_anchor(self.cfg.dest_root, self.clock())
            except Refused as exc:
                raise ConfigError(str(exc)) from exc
        self.log_activity(msg)
        self._wake()
        return msg

    def caps(self) -> dict:
        c = self.cfg
        return {"max_dest_gb": c.max_dest_gb, "min_free_gb": c.min_free_gb,
                "shrink_pct": c.shrink_pct, "pause_ms": c.pause_ms,
                "max_mbps": c.max_mbps}

    # -- settings --------------------------------------------------------------
    def channels_view(self) -> List[dict]:
        """Which alert channels are set up. Only whether, and the host name of the
        server: never an address with a token in it, a key, or a password."""
        c = self.cfg
        host = ""
        if c.webhook_url:
            host = c.webhook_url.split("/")[2] if c.webhook_url.count("/") >= 2 else "set"
        ntfy_host = ""
        if c.ntfy_url:
            ntfy_host = c.ntfy_url.split("/")[2] if c.ntfy_url.count("/") >= 2 else ""
        return [
            {"name": "webhook", "configured": bool(c.webhook_url), "host": host},
            {"name": "ntfy", "configured": bool(c.ntfy_url), "host": ntfy_host},
            {"name": "pushover", "configured": bool(c.pushover_token and c.pushover_user),
             "host": ""},
            {"name": "email", "configured": bool(c.smtp_host and c.smtp_from and c.smtp_to),
             "host": c.smtp_host},
            {"name": "mqtt", "configured": bool(c.mqtt_host), "host": c.mqtt_host},
        ]

    def settings_view(self) -> dict:
        c = self.cfg
        channels = self.channels_view()
        return {**self.caps(), "roots": self.roots_view(),
                "icloud_dir": str(c.dest_root),
                "webhook_set": channels[0]["configured"],
                "webhook_host": channels[0]["host"],
                "notify_on": list(c.notify_on),
                "mqtt_enabled": bool(c.mqtt_host),
                "channels": channels,
                "pause": self.pause_view(),
                "timezone": c.tz}

    def send_test_alert(self) -> dict:
        """Send one test message through every configured channel and say which
        worked. Nothing in a result holds an address, a token or a password."""
        notifiers = list(self.notifiers)
        if not notifiers:
            raise ConfigError("no alert channel is set up, so there is nothing to "
                              "test. Set one up first (see Settings)")
        event = {"event": "test", "job": "", "at": self.clock().isoformat(timespec="seconds"),
                 "message": "This is a test alert from Keepsake. If you can read "
                            "it, this channel works."}
        results = []
        for notifier in notifiers:
            name = getattr(notifier, "name", "") or type(notifier).__name__
            try:
                if hasattr(notifier, "test"):
                    ok, error = notifier.test(event)
                else:
                    ok = bool(notifier.send(event))
                    error = "" if ok else "the channel did not accept the message"
            except Exception:  # noqa: BLE001
                ok, error = False, "the channel raised an error"
            results.append({"channel": name, "ok": ok, "error": error})
        good = [r["channel"] for r in results if r["ok"]]
        bad = [r for r in results if not r["ok"]]
        parts = []
        if good:
            parts.append("delivered to " + ", ".join(good))
        parts += [f"{r['channel']} failed: {r['error']}" for r in bad]
        message = "Test alert " + "; ".join(parts)
        self.log_activity(f"test alert: {len(good)} of {len(results)} channels worked",
                          level="info" if not bad else "error")
        return {"ok": not bad, "message": message, "results": results}

    def _pause_target(self, spec: Any, now: datetime) -> datetime:
        """When a requested pause ends. A preset, or a date and time in the
        configured time zone (an offset in the text wins)."""
        if not isinstance(spec, dict) or ("preset" in spec) == ("until" in spec):
            raise ConfigError("say how long to pause: a preset (1h, tomorrow, 1w) "
                              "or a date and time, not both and not neither")
        local = now.astimezone(self.tz)
        if "preset" in spec:
            preset = spec["preset"]
            if preset == "1h":
                return now + timedelta(hours=1)
            if preset == "tomorrow":
                return datetime.combine(local.date() + timedelta(days=1),
                                        TOMORROW_AT, tzinfo=self.tz)
            if preset == "1w":
                return now + timedelta(days=7)
            raise ConfigError(f"the pause preset must be one of: "
                              f"{', '.join(PAUSE_PRESETS)}")
        try:
            until = datetime.fromisoformat(str(spec["until"]))
        except ValueError as exc:
            raise ConfigError("the pause time must be a date and time such as "
                              "2026-11-10T08:00") from exc
        if until.tzinfo is None:
            until = until.replace(tzinfo=self.tz)
        if until <= now:
            raise ConfigError("that time has already passed; pick a time in the "
                              "future")
        if until - now > PAUSE_MAX:
            raise ConfigError("a pause can last at most a year; pick an earlier "
                              "time")
        return until

    def _settings_raw(self, raw: Any, now: datetime) -> Tuple[Any, List[str]]:
        """Turn the friendly `pause` and `quiet_hours` keys into the stored ones.
        Returns the new raw settings and what to say in the activity log."""
        if not isinstance(raw, dict):
            return raw, []
        raw, said = dict(raw), []
        if "pause" in raw:
            spec = raw.pop("pause")
            if spec is None:
                if self._pause_until(now):
                    raw["pause_ended"] = now.isoformat(timespec="seconds")
                    said.append("pause ended early")
                raw["pause_until"] = ""
            else:
                until = self._pause_target(spec, now)
                raw["pause_until"] = until.isoformat(timespec="seconds")
                said.append("everything paused until " + until.astimezone(
                    self.tz).strftime("%Y-%m-%d %H:%M"))
        if "quiet_hours" in raw:
            quiet = raw.pop("quiet_hours")
            if quiet is None:
                raw["quiet_start"] = raw["quiet_end"] = ""
                said.append("quiet hours off")
            elif isinstance(quiet, dict) and {"start", "end"} <= set(quiet):
                raw["quiet_start"] = str(quiet["start"] or "")
                raw["quiet_end"] = str(quiet["end"] or "")
                said.append(f"quiet hours {raw['quiet_start']} to {raw['quiet_end']}")
            else:
                raise ConfigError("quiet_hours must be null or an object with "
                                  "start and end times")
        return raw, said

    def _store_settings(self, new: Config, raw: Dict[str, Any]) -> None:
        keep = {k: raw[k] for k in ("max_dest_gb", "min_free_gb", "shrink_pct",
                                    "pause_ms", "max_mbps", "notify_on",
                                    "webhook_url", "pause_until", "pause_ended",
                                    "quiet_start", "quiet_end") if k in raw}
        stored = load_json(self.cfg.settings_file, {})
        stored.update({k: getattr(new, k) if k != "notify_on"
                       else list(new.notify_on) for k in keep})
        atomic_write_json(self.cfg.settings_file, stored)
        self.cfg = new
        if "webhook_url" in raw or "notify_on" in raw:
            self.notifiers = build_notifiers(new)

    def update_settings(self, raw: Any) -> str:
        with self.lock:
            raw, said = self._settings_raw(raw, self.clock())
            new = apply_settings(self.cfg, raw)
            self._store_settings(new, raw)
        for line in said:
            self.log_activity(line)
        if not said:
            self.log_activity("settings changed")
        return "settings saved"

    def _expire_pause(self) -> None:
        """Note that a pause ran out, so what it covered is not counted against
        any folder (see `_awake`) and the stored pause is cleared."""
        with self.lock:
            until = _parse(self.cfg.pause_until)
            if not until or self.clock() < until:
                return
            raw = {"pause_until": "", "pause_ended": until.isoformat(timespec="seconds")}
            self._store_settings(apply_settings(self.cfg, raw), raw)
        self.log_activity("pause ended; scheduled syncs and checks run again")

    # -- browse ----------------------------------------------------------------
    def browse(self, root: str, rel: str = "") -> dict:
        """Subfolders of one folder inside an allowed root, for the picker."""
        if root not in self.cfg.roots:
            raise ConfigError(f"location {root!r} is not one this service may read")
        rel = clean_relative(rel, "folder", allow_empty=True)
        live, why = engine.root_live(self.cfg, root)
        if not live:
            raise ConfigError(f"{root} is unavailable: {why}")
        base = self.cfg.roots[root]
        here = base / rel if rel else base
        if not engine.inside(base, here):
            raise ConfigError("that folder is outside the allowed location")
        if not here.is_dir():
            raise ConfigError("that folder does not exist")
        folders: List[dict] = []
        truncated = False
        try:
            with os.scandir(here) as it:
                entries = sorted((e for e in it if not e.name.startswith(".")),
                                 key=lambda e: e.name.lower())
        except OSError as exc:
            raise ConfigError(f"that folder cannot be read: {exc.strerror}") from exc
        for e in entries:
            try:
                if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = f"{rel}/{e.name}" if rel else e.name
            folders.append({"name": e.name, "path": path,
                            "warn": looks_secret(path)})
            if len(folders) >= BROWSE_CAP:
                truncated = True
                break
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        return {"root": root, "path": rel,
                "parent": parent if rel else None,
                "folders": folders, "truncated": truncated}

    # -- locations: the folders Keepsake may read ----------------------------------
    def locations_view(self) -> dict:
        return locations.describe(self.cfg, self.builtin_roots, self._jobs())

    def browse_locations(self, path: str = "") -> dict:
        return locations.browse(self.cfg, path)

    def add_location(self, data: Any) -> str:
        if not isinstance(data, dict):
            raise ConfigError("the location must be a JSON object")
        with self.lock:
            name, path = locations.check_new(self.cfg, data.get("name"), data.get("path"),
                                             self.cfg.roots)
            stored = locations.load_stored(self.cfg)
            stored[name] = path
            locations.save_stored(self.cfg, stored)
            self.cfg.roots[name] = Path(path)
        self.log_activity(f"location added: {name} is {path} (read-only)")
        return f"{name!r} added: Keepsake may now read {path}, and never change it"

    def remove_location(self, name: str) -> str:
        with self.lock:
            if name not in self.cfg.roots:
                raise ConfigError(f"there is no location named {name!r}")
            if name in self.builtin_roots:
                raise ConfigError(f"{name!r} is built in (KEEPSAKE_ROOTS in the compose "
                                  f"file), so it can only be removed there")
            using = sorted(j.name for j in self._jobs() if j.root == name)
            if using:
                raise ConfigError(
                    f"the folder job {using[0]!r} still keeps folders from {name!r}"
                    + (f" (and {len(using) - 1} more)" if len(using) > 1 else "")
                    + ". Remove that first")
            stored = locations.load_stored(self.cfg)
            stored.pop(name, None)
            locations.save_stored(self.cfg, stored)
            del self.cfg.roots[name]
        self.log_activity(f"location removed: {name}")
        return f"{name!r} removed; your folders there were not touched"

    def destinations(self) -> dict:
        """The folders already inside the Keepsake folder in iCloud Drive, for the
        destination list on the Add folder page. Read fresh every time.

        kind: `group` (a folder Keepsake made to hold several jobs' folders, safe to
        add to), `job` (holds one job's files directly, so it cannot also hold other
        folders) or `other` (Keepsake did not create it: usable only when the person
        says they want that)."""
        base = self.cfg.dest_root
        jobs = self._jobs()
        out: List[dict] = []
        try:
            with os.scandir(base) as it:
                entries = sorted((e for e in it if not e.name.startswith(".")),
                                 key=lambda e: e.name.lower())
        except OSError as exc:
            raise ConfigError(f"the iCloud folder cannot be read: {exc.strerror}") from exc
        for e in entries:
            try:
                if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            direct = next((j for j in jobs if not j.group
                           and j.dest.lower() == e.name.lower()), None)
            inside = [j for j in jobs if j.group.lower() == e.name.lower()]
            marked = os.path.isfile(os.path.join(e.path, engine.GROUP_FILE)) \
                and not os.path.islink(os.path.join(e.path, engine.GROUP_FILE))
            if direct:
                kind = "job"
            elif marked or inside:
                kind = "group"
            elif os.path.lexists(os.path.join(e.path, engine.OWNER_FILE)):
                kind = "job"
            else:
                kind = "other"
            out.append({"name": e.name, "kind": kind,
                        "job": direct.name if direct else None,
                        "folders": sorted(j.dest for j in inside)})
            if len(out) >= BROWSE_CAP:
                break
        # A folder a job will make on its first copy is not on disk yet, but its
        # name is already taken: list it, so it can be chosen and cannot be reused.
        on_disk = {f["name"].lower() for f in out}
        for j in jobs:
            name = j.group or j.dest
            if name.lower() in on_disk:
                continue
            on_disk.add(name.lower())
            inside = [x for x in jobs if x.group.lower() == name.lower()]
            out.append({"name": name, "kind": "group" if j.group else "job",
                        "job": None if j.group else j.name,
                        "folders": sorted(x.dest for x in inside)})
        out.sort(key=lambda f: f["name"].lower())
        return {"folders": out, "usable": engine.anchor_problem(base) == ""}

    def _group_problem(self, job: Job) -> str:
        """Why the destination folder of a job cannot be used now, in plain words
        ("" when it can). The engine asks again at run time; this says it at Add."""
        if not job.group:
            return ""
        folder = self.cfg.dest_root / job.group
        try:
            os.lstat(folder)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"The iCloud folder {job.group!r} cannot be read: {exc.strerror}."
        if not os.path.isdir(folder) or os.path.islink(folder):
            return f"The iCloud folder {job.group!r} is a link or not a plain folder."
        if os.path.lexists(folder / engine.OWNER_FILE):
            return (f"The iCloud folder {job.group!r} already holds one folder's copy "
                    f"directly, so other folders cannot be put inside it. Choose "
                    f"another destination.")
        marker = folder / engine.GROUP_FILE
        if not (os.path.isfile(marker) and not os.path.islink(marker)) \
                and not job.adopt_existing:
            return (f"The iCloud folder {job.group!r} was not created by Keepsake. "
                    f"Choose it again and tick the box that says you want to use it "
                    f"anyway, or pick another destination.")
        return ""

    # -- actions -----------------------------------------------------------------
    def _find(self, name: str) -> Job:
        for job in self._jobs():
            if job.name == name:
                return job
        raise ConfigError(f"there is no folder job named {name!r}")

    def _wake(self) -> None:
        self.wake.set()

    def sync_now(self, name: str, accept_shrink: Any = False) -> str:
        with self.lock:
            job = self._find(name)
            if not job.enabled:
                raise ConfigError(f"{job.name!r} is paused")
            if self._busy(job.name):
                raise ConfigError(f"{job.name!r} is already running or waiting; "
                                  f"this request was ignored")
            self.queue.append((job.name, accept_shrink is True))
            self.scheduled.discard(job.name)
        self.log_activity("sync requested" + (" (accepting a smaller source)"
                          if accept_shrink is True else ""), job.name)
        self._wake()
        return f"{job.name!r} will start shortly"

    # -- trust features ------------------------------------------------------
    def preview(self, name: str, accept_shrink: Any = False) -> str:
        """Queue a preview of the next sync: the engine's plan mode, strictly
        read-only, run on the worker like a sync. Writes nothing, not even the
        activity log. `accept_shrink` (the JSON boolean true only) previews what a
        run would do if a smaller source were accepted."""
        with self.lock:
            job = self._find(name)
            if self._busy(job.name):
                raise ConfigError(f"{job.name!r} is already running or waiting; "
                                  f"preview it when it has finished")
            self.previews.pop(job.name, None)
            self.preview_queue.append((job.name, accept_shrink is True))
        self._wake()
        return (f"a preview of {job.name!r} will start shortly; it changes "
                f"nothing")

    def preview_view(self, name: str) -> dict:
        job = self._find(name)
        view = self.job_view(job)
        return {"state": ("running" if view["previewing"] and self.running == name
                          else "queued" if view["previewing"] else "idle"),
                "progress": view["progress"] if view["previewing"] else "",
                "progress_detail": view["progress_detail"] if view["previewing"] else None,
                "preview": self.previews.get(name)}

    def cancel(self, name: str) -> str:
        """Stop a run or a preview, or take it off the waiting list. A run that is
        working stops between files, never in the middle of one, so nothing is
        left half-written and the originals are never touched. A read that the
        operating system is stuck in ends when the share answers."""
        with self.lock:
            job = self._find(name)
            if self.running == job.name and self.track is not None:
                kind = self.running_kind
                self.track.cancel()
                if kind != "preview":
                    self.log_activity("stop requested", job.name)
                return (f"stopping {job.name!r}: it stops at the next file. If the "
                        f"share is not answering this can take a moment")
            active = self.ops.get(job.name)
            if active is not None:
                active[1].cancel()
                return (f"stopping the {active[0]} of {job.name!r}: it stops at the "
                        f"next file. If the share is not answering this can take a "
                        f"moment")
            before = (len(self.queue), len(self.preview_queue))
            self.queue = [q for q in self.queue if q[0] != job.name]
            self.preview_queue = [q for q in self.preview_queue if q[0] != job.name]
            self.scheduled.discard(job.name)
            if before != (len(self.queue), len(self.preview_queue)):
                return f"{job.name!r} was taken off the waiting list"
        raise ConfigError(f"{job.name!r} is not running or waiting; there is nothing to stop")

    def _preview_result(self, job: Job, rep: PreviewReport, now: datetime) -> dict:
        kept = job.mode == "mirror-keep-deleted" or rep.safe_first_run
        if rep.refused:
            message = rep.error
        else:
            sentence = change_sentence(
                {"files_added": rep.added, "files_updated": rep.updated,
                 "files_removed": rep.removed, "result": "ok",
                 "safe_first_run": rep.safe_first_run}, job)
            message = ("A sync started now would find nothing new."
                       if sentence == "nothing changed"
                       else f"A sync started now would do this: {sentence}.")
        return {"label": "Preview", "changed_nothing": True,
                "at": now.isoformat(timespec="seconds"),
                "accept_shrink": rep.shrink_accepted,
                "would_refuse": rep.refused, "guard": rep.guard,
                "result": rep.result, "message": message,
                "shrink_note": ((f"The shrink guard would refuse this run without "
                                 f"'accept shrink': {rep.shrink_problem}.")
                                if rep.shrink_problem and rep.shrink_accepted
                                else ""),
                "added": rep.added, "updated": rep.updated, "removed": rep.removed,
                "removed_how": "moved to .deleted" if kept else "removed",
                "bytes": rep.bytes, "files_total": rep.files_total,
                "left_alone": rep.left_alone,
                "first_run": rep.safe_first_run, "samples": rep.samples[:50],
                "notes": rep.notes}

    def _idle_job(self, name: str, what: str) -> Job:
        job = self._find(name)
        if self._busy(job.name):
            raise ConfigError(f"{job.name!r} is copying or waiting to copy; {what} "
                              f"when it has finished")
        return job

    def verify_job(self, name: str, scheduled: bool = False) -> dict:
        """Check a copy: read-only on the source and on iCloud. Raises ConfigError
        with the reason when it cannot check; otherwise returns the stored result
        (state `match` or `mismatch`, never both a refusal and a success). Each
        check carries on where the last one stopped, so repeated checks cover
        every file; `scheduled` marks the automatic one in the log and the alert."""
        with self.lock:
            job = self._idle_job(name, "check it")
            before = self.state.get(job.name, {}).get("last_run")
            cursor = dict(self.state.get(job.name, {}).get("verify_cursor") or {})
            if not cursor.get("seed"):
                cursor["seed"] = secrets.token_hex(8)
        if not self.verify_lock.acquire(blocking=False):
            raise ConfigError("another copy check is running; try again when it "
                              "has finished")
        try:
            try:
                with self._operation(job.name, "check") as track, tracking(track):
                    result = verify_copy(self.cfg, job, self.clock(), cursor)
            except Cancelled:
                raise ConfigError("The check was stopped at your request. Nothing "
                                  "was changed and no result was kept") from None
            except Refused as exc:
                self._note_check(job.name, cursor, scheduled, "refused")
                raise CheckRefused(str(exc)) from exc
            except OSError as exc:
                raise ConfigError(f"the check could not read a folder: "
                                  f"{exc.strerror or exc}") from exc
        finally:
            self.verify_lock.release()
        with self.lock:
            if self.running == job.name or \
                    self.state.get(job.name, {}).get("last_run") != before:
                raise ConfigError("a copy ran while the check was reading, so the "
                                  "result was not kept; check again")
            st = self.state.setdefault(job.name, {})
            st["verify"] = result
            st["verify_cursor"] = cursor
            if scheduled:
                st["auto_check"] = {"at": result["at"], "result": "ok"}
            self._save_state()
        prefix = "Scheduled check: " if scheduled else ""
        self.log_activity(prefix + result["message"], job.name,
                          "info" if result["ok"] else "error")
        if not result["ok"] and "failed" in self.cfg.notify_on:
            self.notify({"event": "failed", "job": job.name, "result": "mismatch",
                         "message": prefix + result["message"], "at": result["at"]})
        return result

    def _note_check(self, name: str, cursor: dict, scheduled: bool, result: str) -> None:
        """Remember that a check was tried (and where it got to), so one that
        could compare nothing is not retried every few seconds."""
        with self.lock:
            st = self.state.get(name)
            if st is None:
                return
            st["verify_cursor"] = cursor
            if scheduled:
                st["auto_check"] = {"at": self.clock().isoformat(timespec="seconds"),
                                    "result": result}
            self._save_state()

    def run_due_check(self) -> bool:
        """Run one scheduled check that is due, on the worker, and only when no
        copy is running or waiting: a check and a copy of the same folder never
        overlap, and checks run one at a time. Returns True if it tried one."""
        with self.lock:
            if self.running or self.queue or self.preview_queue:
                return False
            if self.gated():
                return False      # paused or in quiet hours: no scheduled check
            now = self.clock()
            due = [j for j in self._jobs()
                   if (nxt := next_check(j, self.state.get(j.name, {}), now))
                   and nxt <= now]
        for job in due:
            try:
                self.verify_job(job.name, scheduled=True)
            except CheckRefused as exc:
                # Recorded, so it comes back in a few hours, not on every pass.
                self.log_activity(f"scheduled check could not run: {exc}", job.name)
                return True
            except ConfigError:
                continue        # busy or queued: the next pass tries again
            return True
        return False

    def deleted_files(self, name: str) -> dict:
        job = self._find(name)
        try:
            out = list_deleted(self.cfg, job, self.clock().date())
        except Refused as exc:
            raise ConfigError(str(exc)) from exc
        except OSError as exc:
            raise ConfigError(f"the deleted files could not be read: "
                              f"{exc.strerror or exc}") from exc
        return {**out, "keep_days": job.keep_days, "mode": job.mode,
                "restore_folder": ".restored"}

    def restore_file(self, name: str, day: Any, path: Any) -> str:
        with self.lock:
            job = self._idle_job(name, "restore from it")
        try:
            with self.restore_lock, self._operation(job.name, "restore") as track, \
                    tracking(track):
                msg = restore_deleted(self.cfg, job, day, path)
        except Cancelled:
            raise ConfigError("The restore was stopped at your request. The file was "
                              "not restored; nothing half-written was left") from None
        except Refused as exc:
            self.log_activity(f"restore refused: {exc}", job.name, "error")
            raise ConfigError(str(exc)) from exc
        self.log_activity(f"a file was restored from .deleted/{day} into .restored",
                          job.name)
        return msg

    def restore_plan(self, name: str, day: Any = None) -> dict:
        """What restoring one date (or everything kept) would copy: counts and
        bytes, per date, plus what would be skipped. Read-only."""
        job = self._find(name)
        try:
            return plan_restore_batch(self.cfg, job, day)
        except Refused as exc:
            raise ConfigError(str(exc)) from exc
        except OSError as exc:
            raise ConfigError(f"the deleted files could not be read: "
                              f"{exc.strerror or exc}") from exc

    def restore_many(self, name: str, day: Any = None, everything: Any = False,
                     expect_files: Any = None) -> dict:
        """Restore every kept file of one date (`day`) or of all dates
        (`everything` is the JSON boolean true) into .restored. Exactly one of the
        two must be given."""
        if (day is None) == (everything is not True):
            raise ConfigError("say which files to restore: one date, or everything "
                              "kept (all: true), not both and not neither")
        with self.lock:
            job = self._idle_job(name, "restore from it")
        try:
            with self.restore_lock, self._operation(job.name, "restore") as track, \
                    tracking(track):
                out = restore_batch(self.cfg, job, day, expect_files)
        except Cancelled:
            raise ConfigError("The restore was stopped at your request. Files copied "
                              "before it stopped stay in .restored; none is half-"
                              "written. Look at that folder before you restore again"
                              ) from None
        except Refused as exc:
            self.log_activity(f"restore refused: {exc}", job.name, "error")
            raise ConfigError(str(exc)) from exc
        except OSError as exc:
            raise ConfigError(f"could not restore: {exc.strerror or exc}") from exc
        self.log_activity(
            f"{out['restored']} files were restored from .deleted into .restored"
            + (f", {len(out['skipped'])} skipped" if out["skipped"] else ""), job.name)
        return out

    def add_job(self, data: Any) -> str:
        if isinstance(data, dict) and "auto_check" not in data:
            # New folders check a copy weekly unless told otherwise; a folder
            # stored before this existed has no value and stays off until set.
            data = {**data, "auto_check": DEFAULT_AUTO_CHECK}
        with self.lock:
            jobs = self._jobs()
            job = validate_job(data, self.cfg, jobs)
            problem = self._group_problem(job)
            if problem:
                raise ConfigError(problem)
            jobs.append(job)
            self.store.save(jobs)
            self.state.setdefault(job.name, {})["created"] = \
                self.clock().isoformat(timespec="seconds")
            self._save_state()
        self.log_activity(f"added: {job.root}:{job.source} into iCloud/{job.dest_path}",
                          job.name)
        self._wake()
        when = ("the first copy starts shortly" if self.setup_complete()
                else "the first copy starts when you ask for it in the setup")
        return (f"{job.name!r} added: {job.root}:{job.source} will be kept in "
                f"iCloud under {job.dest_path}; {when}")

    def update_job(self, name: str, data: Any) -> str:
        if not isinstance(data, dict):
            raise ConfigError("the job must be a JSON object")
        with self.lock:
            jobs = self._jobs()
            current = self._find(name)
            if self.running == current.name:
                raise ConfigError(f"{current.name!r} is running; change it when it "
                                  f"has finished")
            merged = {**current.as_dict(),
                      **{k: v for k, v in data.items() if k not in ("name",)}}
            merged["name"] = current.name  # a job is renamed by remove + add
            new = validate_job(merged, self.cfg, jobs, replacing=current.name)
            if new.dest_path != current.dest_path:
                raise ConfigError("the iCloud folder name cannot change; remove "
                                  "the job and add it again")
            if (new.root, new.source) != (current.root, current.source):
                raise ConfigError("the source folder cannot change; remove the "
                                  "job and add it again")
            self.store.save([new if j.name == current.name else j for j in jobs])
            self.previews.pop(current.name, None)
        self.log_activity("settings updated", current.name)
        return f"{current.name!r} updated"

    def remove_job(self, name: str, delete_copy: Any = False) -> str:
        with self.lock:
            job = self._find(name)
            if self.running == job.name:
                raise ConfigError(f"{job.name!r} is running; remove it when it "
                                  f"has finished")
            self.queue = [(n, a) for n, a in self.queue if n != job.name]
            self.preview_queue = [(n, a) for n, a in self.preview_queue
                                  if n != job.name]
            self.previews.pop(job.name, None)
            self.scheduled.discard(job.name)
            self.store.save([j for j in self._jobs() if j.name != job.name])
            self.state.pop(job.name, None)
            self._save_state()
            (self.cfg.manifest_dir / f"{job.name}.json").unlink(missing_ok=True)
            if delete_copy is True:
                msg = (f"{job.name!r} removed. "
                       + retire_job_folder(self.cfg, job, self.clock()))
            else:
                msg = (f"{job.name!r} removed. The copy already in iCloud was "
                       f"NOT deleted and will stay there")
        self.log_activity(msg, job.name)
        return msg

    # -- the worker --------------------------------------------------------------
    def due_jobs(self) -> List[str]:
        now = self.clock()
        out: List[str] = []
        if self.gated():
            return out        # paused or in quiet hours: nothing starts by itself
        # While the guided setup is open a folder's FIRST copy waits for the
        # person: the wizard shows the preview first, then starts it (a Sync
        # now is queued directly and does not come through here).
        setup_open = not self.setup_complete()
        for job in self._jobs():
            if not job.enabled or self._busy(job.name):
                continue
            st = self.state.get(job.name, {})
            if setup_open and not st.get("last_run"):
                continue
            if next_due(job, _parse(st.get("last_run")),
                        st.get("result") == "ok", now, self.tz) <= now:
                out.append(job.name)
        return out

    def run_next(self) -> bool:
        """Run one queued or due job (or one preview). Returns True if it ran
        something. A pause or quiet hours drop what the schedule queued but never
        what a person asked for, and never stop a run that already started."""
        with self.lock:
            if self.gated():
                # Scheduled syncs that were waiting for their turn do not start.
                self.queue = [q for q in self.queue if q[0] not in self.scheduled]
                self.scheduled.clear()
            elif not self.queue and not self.preview_queue:
                for name in self.due_jobs():
                    self.queue.append((name, False))
                    self.scheduled.add(name)
            if not self.queue and not self.preview_queue:
                return False
            previewing = not self.queue
            name, accept = (self.preview_queue if previewing else self.queue)[0]
            try:
                job = self._find(name)
            except ConfigError:
                (self.preview_queue if previewing else self.queue).pop(0)
                return True
            self.running = name
            self.running_kind = "preview" if previewing else "sync"
            self.scheduled.discard(name)
            cfg = self.cfg

        def progress(msg: str) -> None:
            self.progress = msg

        # A finished preview counted the source files: the next read of the same
        # source can show how far it has got against that number.
        hint = (self.previews.get(name) or {}).get("files_total") or None
        track = Progress(hint_files=hint, op="preview" if previewing else "sync")
        with self.lock:
            self.track = track

        if previewing:
            try:
                with tracking(track):
                    rep = preview_job(cfg, job, accept_shrink=accept, progress=progress)
            except Exception as exc:  # noqa: BLE001 - a preview must always end in a sentence
                logger.exception("[%s] a preview failed outside the engine", name)
                rep = PreviewReport(job=name, refused=True, result="failed",
                                    error=f"the preview could not finish: "
                                          f"{type(exc).__name__}: {exc}")
            with self.lock:
                self.preview_queue = [q for q in self.preview_queue if q[0] != name]
                self.running, self.running_kind, self.progress = None, "sync", ""
                self.track = None
                try:
                    self._find(name)
                    self.previews[name] = self._preview_result(job, rep, self.clock())
                except ConfigError:
                    pass          # the folder was removed while it was read
                except Exception as exc:  # noqa: BLE001 - the page must never wait for a result that will not come
                    logger.exception("[%s] a preview result could not be stored", name)
                    self.previews[name] = {
                        "label": "Preview", "changed_nothing": True,
                        "at": self.clock().isoformat(timespec="seconds"),
                        "accept_shrink": False, "would_refuse": True, "guard": "",
                        "result": "failed", "shrink_note": "",
                        "message": f"the preview could not finish: {type(exc).__name__}: {exc}",
                        "added": 0, "updated": 0, "removed": 0, "removed_how": "removed",
                        "bytes": 0, "files_total": 0, "left_alone": 0,
                        "first_run": False, "samples": [], "notes": []}
            return True
        try:
            with tracking(track):
                report = run_job(cfg, job, accept_shrink=accept, progress=progress,
                                 now=self.clock())
        except BaseException:
            # Whatever happens in the engine, the worker is free again: a job left
            # marked as running would make every page say "working" for ever.
            with self.lock:
                self.queue = [q for q in self.queue if q[0] != name]
                self.running, self.progress, self.track = None, "", None
            raise
        with self.lock:
            self.queue = [q for q in self.queue if q[0] != name]
            self.running, self.progress, self.track = None, "", None
            self._record(job, report)
        return True

    def _record(self, job: Job, report: RunReport) -> None:
        st = self.state.setdefault(job.name, {})
        kept = st.get("dest_bytes", 0)
        kept_evicted = st.get("evicted")
        before = st.get("notified_error")
        st.update(report.as_dict())
        if report.evicted is None:
            st["evicted"] = kept_evicted   # a refused run never read iCloud back
        if not report.dest_bytes:
            # A refused run never read the destination back: keep the last size
            # we really measured instead of showing the folder as empty.
            st["dest_bytes"] = kept
        st["last_run"] = report.started
        st["result"] = report.result
        started = _parse(report.started) or self.clock()
        st["history"] = merge_history(
            st.get("history"), started.astimezone(self.tz).date().isoformat(),
            run_outcome(report), self.today(), run_detail(report, job))
        self.previews.pop(job.name, None)   # a real run makes a preview out of date
        if report.result == "ok":
            st["last_success"] = report.started
        if report.result == "ok":
            line = (f"ok in {report.duration_s}s: "
                    f"{change_sentence(report.as_dict(), job)}")
            level = "info"
        elif report.cancelled:
            line = f"stopped by you: {report.error}"
            level = "info"        # a person's own choice is not an alarm
        else:
            line = f"{report.result}: {report.error}"
            level = "error"
        self.log_activity(line, job.name, level)
        if report.result != "ok" and report.error != before \
                and not report.cancelled and "failed" in self.cfg.notify_on:
            st["notified_error"] = report.error
            self.notify({"event": "failed", "job": job.name,
                         "result": report.result, "message": report.error,
                         "at": report.started})
        elif report.result == "ok" and (before or st.get("stale_alerted")):
            was_stale = st.pop("stale_alerted", None)
            st.pop("notified_error", None)
            if "recovered" in self.cfg.notify_on:
                self.notify({"event": "recovered", "job": job.name,
                             "message": ("a run succeeded again after no success "
                                         "since " + was_stale["since"][:10]
                                         if isinstance(was_stale, dict)
                                         else "the last run succeeded"),
                             "at": report.started})
        if report.result == "ok":
            st.pop("stale_alerted", None)
        self._save_state()

    def check_stale(self) -> None:
        """Alert, once per outage, when a folder has had no successful run for
        its `stale_days` (never less than twice its own period). The run that
        heals it sends the recovery message (see `_record`). The marker lives in
        the state file, so a restart does not repeat the alert."""
        now = self.clock()
        if self.gated():
            return      # a paused folder is waiting, not silent: no stale alert
        for job in self._jobs():
            if not job.enabled or job.stale_days <= 0:
                continue
            with self.lock:
                st = self.state.setdefault(job.name, {})
                since = _parse(st.get("last_success")) or _parse(st.get("created"))
                if since is None:
                    st["created"] = now.isoformat(timespec="seconds")
                    self._save_state()
                    continue
                limit = max(timedelta(days=job.stale_days), period_of(job) * 2)
                if self._awake(since, now) <= limit or st.get("stale_alerted"):
                    continue
                days = (now - since).days
                st["stale_alerted"] = {"at": now.isoformat(timespec="seconds"),
                                       "since": since.isoformat(timespec="seconds")}
                self._save_state()
            message = (f"no successful run for {days} days"
                       + (f" (last success {since.date().isoformat()})"
                          if st.get("last_success") else " since the folder was added"))
            self.log_activity(message, job.name, "error")
            if "stale" in self.cfg.notify_on:
                self.notify({"event": "stale", "job": job.name, "message": message,
                             "at": now.isoformat(timespec="seconds")})

    def housekeeping(self) -> None:
        self._expire_pause()
        self.check_stale()
        if self.status_publisher:
            self.status_publisher.tick()
        removed = self.cfg.dest_root / REMOVED_DIR
        if removed.is_dir():
            prune_deleted(removed, REMOVED_KEEP_DAYS, self.clock().date())
        self.last_beat = time.monotonic()
