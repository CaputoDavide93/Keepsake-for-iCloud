"""What a run is doing right now, so a person can tell working from stuck.

A preview or a copy over a slow network share can go quiet for a long time, and a
page that only says "Reading" cannot tell the two apart. `Progress` is the one
small object the engine feeds while it works and the service reads while it
answers: the phase, how many files and bytes it has seen, the folder it is in, how
long it has run and, above all, when anything last happened.

It also carries the one thing that lets a person stop a run: `cancel()` sets a
flag, and the engine looks at it between files and between chunks of a file
(`check()`), never in the middle of a write, so a stop leaves no half-written file
behind (a copy is a temp file renamed into place, and a stopped one is removed).
A read that is hung inside the operating system cannot be interrupted; it ends
when the share answers, and the page says so.

Nothing here touches a file. It holds numbers and one short folder name.
"""

from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

STALL_AFTER_S = 30            # no activity for this long: say so, plainly
CANCEL_OFFER_AFTER_S = 300    # no activity for this long: offer to stop the run

LABELS = {
    "starting": "Starting",
    "reading-source": "Reading your folder",
    "reading-icloud": "Reading the iCloud folder",
    "comparing": "Comparing the two",
    "measuring": "Measuring the iCloud copy",
    "copying": "Copying",
    "tidying": "Tidying up",
    "verifying": "Reading the iCloud copy back",
    "checking": "Comparing some files with the originals",
}
READING = ("reading-source", "reading-icloud", "measuring", "verifying")
# The same phase reads differently for different operations.
OP_LABELS = {("restore", "copying"): "Restoring files",
             ("check", "checking"): "Comparing files with the originals"}


_current: "contextvars.ContextVar[Progress | None]" = contextvars.ContextVar(
    "keepsake_progress", default=None)


def current() -> "Progress | None":
    """The tracker of the operation running in THIS thread, if the service set one.
    The engine's helpers (hashing a file, copying a restored one) read it here, so
    nothing in between has to carry it as a parameter."""
    return _current.get()


@contextmanager
def tracking(track: "Progress") -> Iterator["Progress"]:
    token = _current.set(track)
    try:
        yield track
    finally:
        _current.reset(token)


class Cancelled(Exception):
    """A person asked this run to stop. Raised only between files and chunks."""


def short_path(rel: str, limit: int = 60) -> str:
    """The end of a relative path, short enough for one line. Whole folders from
    the right, with a leading ellipsis when something was cut."""
    rel = (rel or "").strip("/")
    if len(rel) <= limit:
        return rel
    parts = rel.split("/")
    out = parts[-1]
    if len(out) > limit - 2:
        return "…" + out[-(limit - 1):]
    for part in reversed(parts[:-1]):
        if len(out) + len(part) + 1 > limit - 2:
            break
        out = part + "/" + out
    return "…/" + out


class Progress:
    """Thread-safe: the worker writes, the web thread reads a snapshot."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 hint_files: int | None = None, op: str = "sync") -> None:
        self.op = op                # sync, preview, check or restore
        self._clock = clock
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self.started = clock()
        self.last_activity = self.started
        self.phase = "starting"
        self.files = 0
        self.bytes = 0
        self.total_files: int | None = None
        self.total_bytes: int | None = None
        self.estimated = False
        self.path = ""
        # The number of source files a finished preview counted, used as a
        # best guess for how far the next read of the source has got.
        self.hint_files = hint_files if hint_files and hint_files > 0 else None

    # -- fed by the engine --------------------------------------------------
    def phase_start(self, phase: str, total_files: int | None = None,
                    total_bytes: int | None = None) -> None:
        """A new phase begins: its own counters, from zero."""
        self.check()
        with self._lock:
            self.phase = phase
            self.files = 0
            self.bytes = 0
            self.path = ""
            self.estimated = False
            if phase == "reading-source" and total_files is None and self.hint_files:
                total_files, self.estimated = self.hint_files, True
            self.total_files = total_files
            self.total_bytes = total_bytes
            self.last_activity = self._clock()

    def add(self, path: str = "", files: int = 0, nbytes: int = 0) -> None:
        """Something happened: count it, remember where, and honour a stop."""
        self.check()
        with self._lock:
            self.files += files
            self.bytes += nbytes
            if path:
                self.path = path
            self.last_activity = self._clock()

    def touch(self, path: str = "") -> None:
        self.add(path)

    def check(self) -> None:
        """Between files and chunks: stop here if a person asked."""
        if self._cancel.is_set():
            raise Cancelled()

    # -- fed by the page ----------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelling(self) -> bool:
        return self._cancel.is_set()

    # -- read by the service ------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            idle = max(0.0, now - self.last_activity)
            percent = None
            if self.total_bytes:
                percent = 100.0 * self.bytes / self.total_bytes
            elif self.total_files:
                percent = 100.0 * self.files / self.total_files
            if percent is not None:
                percent = max(0.0, min(99.0 if self.estimated else 100.0, percent))
            return {
                "op": self.op,
                "phase": self.phase,
                "label": OP_LABELS.get((self.op, self.phase),
                                       LABELS.get(self.phase, self.phase)),
                "reading": self.phase in READING,
                "files": self.files,
                "bytes": self.bytes,
                "total_files": self.total_files,
                "total_bytes": self.total_bytes,
                "estimated": self.estimated,
                "percent": None if percent is None else round(percent, 1),
                "path": short_path(self.path),
                "elapsed_s": round(max(0.0, now - self.started), 1),
                "idle_s": round(idle, 1),
                "stalled": idle >= STALL_AFTER_S,
                "can_cancel": idle >= CANCEL_OFFER_AFTER_S,
                "cancelling": self._cancel.is_set(),
            }
