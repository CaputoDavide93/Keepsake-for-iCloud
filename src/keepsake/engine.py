"""The copy engine: one job, one run, judged from the content it left behind.

One-way. The source is opened for reading only. Every write lands inside
``<dest_root>/<job.dest>`` (``<dest_root>/<job.group>/<job.dest>`` for a folder kept
inside a shared destination folder), as a uniquely named temp file created
exclusively and renamed into place, so a killed run leaves either the old file
or the new one. The one other thing written is the small ``.keepsake-group``
marker, put into a shared destination folder Keepsake makes (or is told to take
over) so a folder it did not create is never filled silently.

The destination is untrusted: nothing in it is ever followed if it is a
symlink. Reserved folders (`.deleted`, `.removed`) must be real directories
inside the job folder, temp files are created with O_EXCL|O_NOFOLLOW, and a
replaced file is moved aside only AFTER its replacement has been copied.

Guards, in the order they fire (each refuses the WHOLE run, copying nothing):

  1. the location is not a live mount (missing, empty, a plain leftover folder,
     or without its marker)                       -> skipped
  2. the source escapes its root                  -> failed
  3. the source is empty, unreadable in part, or has shrunk in files, bytes or
     remembered paths by more than the allowed percentage since the last good
     run (a vanished mount looks exactly like this) -> failed
  4. no baseline (first run, lost manifest, re-added job) and the destination
     folder is not empty and not ours: refuse unless `adopt_existing`
  5. the copy would exceed the folder's size cap, or leave the Mac under its
     free-disk reserve                             -> failed

With no baseline the run is a SAFE first run: it copies and verifies, but never
retires anything that is already in the destination.

After the copy the destination is read back and compared with what the source
said it should hold (file count, byte total, a sha256 sample of copied AND
unchanged files). The result comes from that comparison, never from the copy
loop's own bookkeeping.
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import stat as statmod
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from keepsake.config import Config, Job, atomic_write_json, load_json
from keepsake.progress import Cancelled, Progress, tracking
from keepsake.progress import current as current_progress

logger = logging.getLogger("keepsake")

TMP_RE = re.compile(r"^\.icsync-[0-9a-f]{16}\.tmp$")
OWNER_FILE = ".keepsake-owner"
GROUP_FILE = ".keepsake-group"
ANCHOR_FILE = ".keepsake-anchor"
DELETED_DIR = ".deleted"
REMOVED_DIR = ".removed"
RESTORED_DIR = ".restored"
RESERVED = (DELETED_DIR, REMOVED_DIR, RESTORED_DIR)
CHUNK = 1024 * 1024
MTIME_TOLERANCE_NS = 2_000_000_000  # iCloud and SMB round mtimes; 2 s is equal
SAMPLE = 5
SAMPLE_MAX_BYTES = 256 * 1024 * 1024  # do not hash a 20 GB film every run
PATH_SAMPLE = 200
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
REMOVED_KEEP_DAYS = 30
FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERIFY_FILES = 40                      # files a "check a copy" reads back
VERIFY_MAX_BYTES = 1024 ** 3           # ... and at most this many bytes per side
VERIFY_TRIES = 400                     # candidates looked at to find those files
CHANGE_SAMPLES = 50                    # paths remembered per run for "what changed"
DELETED_LIST_MAX = 500                 # rows of the deleted-files view
DELETED_SCAN_MAX = 20000               # entries looked at to build it
GB = 1024 ** 3
RESTORE_BATCH_MAX = 5000               # files one "restore all" will take on
RESTORE_SKIP_NAMES = 5                 # skipped files named in the result sentence
# Files that must never reach iCloud even inside a folder that is fine.
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
                "*.p12", "*.pfx", ".netrc", "*.kdbx")


class Refused(Exception):
    """The run was stopped by a guard. `result` is `failed` or `skipped`."""

    def __init__(self, message: str, result: str = "failed", guard: str = "") -> None:
        super().__init__(message)
        self.result = result
        self.guard = guard      # which guard fired; only a preview reports it


@dataclass
class RunReport:
    job: str
    result: str = "failed"          # ok | failed | skipped
    error: str = ""
    started: str = ""
    duration_s: float = 0.0
    files_copied: int = 0
    bytes_copied: int = 0
    files_moved_to_deleted: int = 0
    files_total: int = 0
    placeholders_skipped: int = 0   # evicted by macOS: counted, not re-copied
    # Evicted placeholders (`.<name>.icloud`) found in the iCloud folder when the
    # run read it back. None when the run never got that far.
    evicted: int | None = None
    secrets_skipped: list[str] = field(default_factory=list)
    changed_during_copy: int = 0
    copy_errors: int = 0
    dest_bytes: int = 0             # everything under the job folder incl .deleted
    verified_files: int = 0
    sampled: int = 0
    unreadable_dirs: int = 0
    dest_errors: int = 0
    left_alone: int = 0             # safe first run: retire candidates not touched
    safe_first_run: bool = False
    cancelled: bool = False         # a person stopped it; nothing half-written
    notes: list[str] = field(default_factory=list)
    # What this run changed: counts plus up to CHANGE_SAMPLES example paths.
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0          # gone from the source: kept in .deleted, or deleted (plain mirror)
    change_samples: list[dict] = field(default_factory=list)

    def note_change(self, kind: str, rel: str) -> None:
        if kind == "added":
            self.files_added += 1
        elif kind == "updated":
            self.files_updated += 1
        else:
            self.files_removed += 1
        if len(self.change_samples) < CHANGE_SAMPLES:
            self.change_samples.append({"kind": kind, "path": rel})

    def as_dict(self) -> dict:
        return dict(self.__dict__)


Entry = tuple[int, int]  # (size, mtime_ns)


# ── names and scanning ─────────────────────────────────────────────────────

def is_secret_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_FILES)


def is_excluded(rel: str, patterns: list[str]) -> bool:
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        if (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat)
                or fnmatch.fnmatch(rel, pat.rstrip("/") + "/*")):
            return True
    return False


def scan_source(src_fd: int, patterns: list[str], report: RunReport,
                trk: Progress | None = None) -> dict[str, Entry]:
    """Regular files only, walked by directory fd with O_NOFOLLOW at every step:
    a folder swapped for a symlink mid-scan is an unreadable folder (the run is
    refused), never a shorter listing. Symlinks are never followed or copied."""
    trk = trk or Progress()
    out: dict[str, Entry] = {}
    stack: list[tuple[str, int]] = [("", os.dup(src_fd))]
    try:
        while stack:
            rel_dir, dfd = stack.pop()
            try:
                trk.touch(rel_dir)
                try:
                    with os.scandir(dfd) as it:
                        entries = list(it)
                except OSError:
                    report.unreadable_dirs += 1
                    continue
                for entry in entries:
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    try:
                        if entry.is_symlink() or is_excluded(rel, patterns):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((rel, os.open(entry.name, DIRFLAGS, dir_fd=dfd)))
                        elif entry.is_file(follow_symlinks=False):
                            if is_secret_name(entry.name):
                                if len(report.secrets_skipped) < 10:
                                    report.secrets_skipped.append(rel)
                                continue
                            st = entry.stat(follow_symlinks=False)
                            out[rel] = (st.st_size, st.st_mtime_ns)
                            trk.add(rel_dir, 1, st.st_size)
                    except OSError:
                        report.unreadable_dirs += 1
            finally:
                os.close(dfd)
    finally:
        for _rel, dfd in stack:
            os.close(dfd)
    return out


def placeholder_for(rel: str) -> str:
    """`a/b/photo.jpg` is evicted as `a/b/.photo.jpg.icloud`."""
    head, _, tail = rel.rpartition("/")
    name = f".{tail}.icloud"
    return f"{head}/{name}" if head else name


def scan_dest(job_fd: int, report: RunReport, clean: bool = True,
              trk: Progress | None = None) -> tuple[dict[str, Entry], set[str]]:
    """(real files, rel paths present only as an iCloud placeholder).

    Walked by directory fd, never through a symlink. Reserved folders, the owner
    file and OUR temp files are not part of the mirror; our own leftover temp
    files are removed only when `clean` (the folder is known to be ours). A read
    error is counted (`dest_errors`) and refuses the run: an unreadable entry
    must not look like an absent one.
    """
    trk = trk or Progress()
    real: dict[str, Entry] = {}
    placeholders: set[str] = set()
    stack: list[tuple[str, int]] = [("", os.dup(job_fd))]
    try:
        while stack:
            rel_dir, dfd = stack.pop()
            try:
                trk.touch(rel_dir)
                try:
                    with os.scandir(dfd) as it:
                        entries = list(it)
                except OSError:
                    report.dest_errors += 1
                    continue
                for entry in entries:
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    if not rel_dir and (entry.name in RESERVED or entry.name == OWNER_FILE):
                        continue
                    try:
                        if entry.is_symlink():
                            if clean and TMP_RE.match(entry.name):
                                os.unlink(entry.name, dir_fd=dfd)  # the link itself
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((rel, os.open(entry.name, DIRFLAGS, dir_fd=dfd)))
                        elif TMP_RE.match(entry.name):
                            if clean:
                                os.unlink(entry.name, dir_fd=dfd)  # our killed run's leftover
                        elif entry.name.startswith(".") and entry.name.endswith(".icloud"):
                            placeholders.add(rel[: -len(entry.name)] + entry.name[1:-7])
                        else:
                            st = entry.stat(follow_symlinks=False)
                            real[rel] = (st.st_size, st.st_mtime_ns)
                            trk.add(rel_dir, 1, st.st_size)
                    except OSError:
                        report.dest_errors += 1
            finally:
                os.close(dfd)
    finally:
        for _rel, dfd in stack:
            os.close(dfd)
    return real, placeholders


def tree_bytes(path: Path, trk: Progress | None = None) -> int:
    total = 0
    for dirpath, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dirpath, d))]
        if trk is not None:
            trk.touch(os.path.relpath(dirpath, path) if dirpath != str(path) else "")
        for name in files:
            try:
                size = os.lstat(os.path.join(dirpath, name)).st_size
                total += size
                if trk is not None:
                    trk.add("", 1, size)
            except OSError:
                pass
    return total


def same(a: Entry, b: Entry) -> bool:
    return a[0] == b[0] and abs(a[1] - b[1]) < MTIME_TOLERANCE_NS


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def inside(base: Path, candidate: Path) -> bool:
    base_r = os.path.realpath(base)
    cand_r = os.path.realpath(candidate)
    return cand_r == base_r or cand_r.startswith(base_r + os.sep)


# ── safe destination primitives ────────────────────────────────────────────
# Every write goes through directory file descriptors opened with O_NOFOLLOW,
# one component at a time, so a folder swapped for a symlink after a check is
# refused by the kernel instead of followed (no check-then-use window).

DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def is_real_dir(path: Path) -> bool:
    """A directory that is not a symlink (lstat, never follows)."""
    try:
        return statmod.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def open_dir_at(fd: int, parts: list[str], create: bool = False) -> int:
    """Open fd/parts as a directory fd, one NOFOLLOW step at a time. A symlink
    or non-directory on the way raises OSError; missing parts are created only
    when asked."""
    cur = os.dup(fd)
    try:
        for part in parts:
            try:
                nxt = os.open(part, DIRFLAGS, dir_fd=cur)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, dir_fd=cur)
                nxt = os.open(part, DIRFLAGS, dir_fd=cur)
            os.close(cur)
            cur = nxt
        return cur
    except BaseException:
        os.close(cur)
        raise


def lexists_at(fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False

def _is_group(group_fd: int) -> bool:
    """True when this folder carries the marker Keepsake writes into a destination
    folder it made (or was told to take over) to hold several jobs' folders."""
    try:
        st = os.stat(GROUP_FILE, dir_fd=group_fd, follow_symlinks=False)
    except OSError:
        return False
    return statmod.S_ISREG(st.st_mode)


def _write_group(group_fd: int, now: datetime | None = None) -> None:
    tmp = new_tmp_name()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600,
                 dir_fd=group_fd)
    with os.fdopen(fd, "w") as handle:
        json.dump({"created": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                   "note": "written by Keepsake; this folder holds one folder per source"},
                  handle)
    os.replace(tmp, GROUP_FILE, src_dir_fd=group_fd, dst_dir_fd=group_fd)


def open_job_dir(root_fd: int, job: Job, create: bool = False) -> int:
    """The job's iCloud folder as a directory fd.

    A job kept inside a destination folder (`job.group`) opens that folder first.
    A destination folder that Keepsake did not create is refused, with a plain
    sentence, unless the job says to take it over; one that already holds a single
    job's files directly is always refused, because the copies would be mixed.
    A job straight under Keepsake opens exactly as it always did."""
    if not job.group:
        return open_dir_at(root_fd, [job.dest], create=create)
    existed = lexists_at(root_fd, job.group)
    group_fd = open_dir_at(root_fd, [job.group], create=create)
    try:
        if existed:
            if lexists_at(group_fd, OWNER_FILE):
                raise Refused(f"the iCloud folder {job.group!r} already holds one "
                              f"folder's copy directly, so other folders cannot be "
                              f"put inside it. Nothing was changed. Choose another "
                              f"destination", guard="first-run")
            if not _is_group(group_fd):
                if not job.adopt_existing:
                    raise Refused(f"the iCloud folder {job.group!r} was not created "
                                  f"by Keepsake. Nothing was changed. Choose it "
                                  f"again and tick the box that says you want to "
                                  f"use it anyway, or pick another destination",
                                  guard="first-run")
                if create:
                    _write_group(group_fd)
        elif create:
            _write_group(group_fd)
        return open_dir_at(group_fd, [job.dest], create=create)
    finally:
        os.close(group_fd)



def check_reserved(job_fd: int) -> None:
    """`.deleted` / `.removed` must be real directories in the job folder or absent."""
    for name in RESERVED:
        try:
            st = os.stat(name, dir_fd=job_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not statmod.S_ISDIR(st.st_mode):
            raise Refused(f"{name} in the iCloud folder is a symlink or not a "
                          f"folder; refused so nothing is moved outside it")


def new_tmp_name() -> str:
    return f".icsync-{secrets.token_hex(8)}.tmp"


def open_source(src_fd: int, rel: str) -> int:
    """Open a source file for reading through NOFOLLOW directory steps, so a
    folder swapped for a symlink after the scan cannot lead outside the source."""
    parts = rel.split("/")
    dfd = open_dir_at(src_fd, parts[:-1])
    try:
        return os.open(parts[-1], os.O_RDONLY | NOFOLLOW, dir_fd=dfd)
    finally:
        os.close(dfd)


def stage_copy(ctx: "Ctx", rel: str, size: int, mtime_ns: int,
               throttle: "Throttle") -> tuple[int, str] | None:
    """Copy the source file to a fresh temp file beside its destination
    (O_EXCL|O_NOFOLLOW, 0600), fsync, set the mtime. Returns (parent dir fd,
    temp name) for the caller to commit and close, or None when the source
    changed (before or during the copy). The destination itself is not touched."""
    pfd = open_dir_at(ctx.job_fd, rel.split("/")[:-1], create=True)
    tmp = new_tmp_name()
    try:
        sfd = open_source(ctx.src_fd, rel)
        try:
            st = os.fstat(sfd)
            if st.st_size != size or st.st_mtime_ns != mtime_ns:
                os.close(sfd)
                os.close(pfd)
                return None
            dfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                          0o600, dir_fd=pfd)
        except BaseException:
            try:
                os.close(sfd)
            except OSError:
                pass
            raise
        try:
            with os.fdopen(sfd, "rb") as fin, os.fdopen(dfd, "wb") as fout:
                while True:
                    block = fin.read(CHUNK)
                    if not block:
                        break
                    fout.write(block)
                    throttle.after_chunk(len(block))
                    if ctx.trk is not None:
                        ctx.trk.add("", 0, len(block))   # may raise Cancelled: the except below removes the temp file
                fout.flush()
                os.fsync(fout.fileno())
                after = os.fstat(fin.fileno())
                if after.st_size != size or after.st_mtime_ns != mtime_ns:
                    raise _Changed()
                os.utime(fout.fileno(), ns=(mtime_ns, mtime_ns))
        except _Changed:
            os.unlink(tmp, dir_fd=pfd)
            os.close(pfd)
            return None
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=pfd)
            except OSError:
                pass
            raise
        return pfd, tmp
    except BaseException:
        try:
            os.close(pfd)
        except OSError:
            pass
        raise


class _Changed(Exception):
    pass


def move_aside(job_fd: int, rel: str, stamp: str) -> None:
    """Keep a replaced or vanished file under .deleted/<date>/ (same filesystem),
    through NOFOLLOW directory steps only."""
    dirs, name = rel.split("/")[:-1], rel.rsplit("/", 1)[-1]
    tfd = open_dir_at(job_fd, [DELETED_DIR, stamp, *dirs], create=True)
    sfd = open_dir_at(job_fd, dirs)
    try:
        target = name
        if lexists_at(tfd, target):
            target = f"{name}.{int(time.time())}"
        os.rename(name, target, src_dir_fd=sfd, dst_dir_fd=tfd)
    finally:
        os.close(tfd)
        os.close(sfd)


def prune_deleted_fd(dfd: int, keep_days: int, today: date) -> int:
    """Remove dated folders older than keep_days directly under the directory
    fd. A child that is not a real directory is skipped; removal runs by fd, so
    nothing outside this directory can be reached."""
    removed = 0
    cutoff = today - timedelta(days=keep_days)
    for name in os.listdir(dfd):
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            continue
        if not statmod.S_ISDIR(st.st_mode):
            continue
        found = DATE_RE.search(name)
        if not found:
            continue
        try:
            day = date.fromisoformat(found.group(0))
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(name, dir_fd=dfd, ignore_errors=True)
            removed += 1
    return removed


def prune_deleted(parent: Path, keep_days: int, today: date) -> int:
    """Path entry point: `parent` must itself be a real directory."""
    try:
        dfd = os.open(parent, DIRFLAGS)
    except OSError:
        return 0
    try:
        return prune_deleted_fd(dfd, keep_days, today)
    finally:
        os.close(dfd)


@dataclass
class Ctx:
    src_fd: int
    job_fd: int
    job_dir: Path
    src: Path
    trk: Progress | None = None     # what the run is doing, and whether to stop


def prune_empty_dirs(job_fd: int) -> None:
    """Remove folders left empty by a retire, by directory fd, never through a
    symlink. The job's reserved folders are left alone."""
    def walk(dfd: int, top: bool) -> None:
        for name in os.listdir(dfd):
            if top and name in RESERVED:
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if not statmod.S_ISDIR(st.st_mode):
                    continue
                child = os.open(name, DIRFLAGS, dir_fd=dfd)
            except OSError:
                continue
            try:
                walk(child, False)
            finally:
                os.close(child)
            try:
                os.rmdir(name, dir_fd=dfd)  # only succeeds when empty
            except OSError:
                pass
    walk(job_fd, True)


# ── the source side ────────────────────────────────────────────────────────

def root_live(cfg: Config, name: str) -> tuple[bool, str]:
    """Is this root a live mount with content, and not a leftover folder?"""
    root = cfg.roots[name]
    try:
        st = os.stat(root)
    except OSError:
        return False, f"{root} is not mounted"
    if not statmod.S_ISDIR(st.st_mode):
        return False, f"{root} is not a folder"
    if name in cfg.fs_roots and st.st_ino > 2:
        # The root of a mounted filesystem has inode 1 or 2 (measured on the
        # SMB share through Docker Desktop); a leftover plain folder does not.
        return False, f"{root} is a plain folder, not the mounted share"
    marker = cfg.root_markers.get(name)
    if marker and not (root / marker).is_file():
        return False, f"{root} does not hold its marker file {marker}"
    try:
        visible = [n for n in os.listdir(root) if not n.startswith(".")]
    except OSError:
        return False, f"{root} cannot be listed"
    if not visible:
        return False, f"{root} is empty (an unmounted share leaves exactly this)"
    return True, ""


def anchor_problem(dest_root: Path) -> str:
    """The one-time iCloud anchor: a marker the service wrote earlier, so an empty
    Keepsake that Docker created because iCloud was not mounted at start is never
    taken for the real iCloud folder. '' when it is present as a real file."""
    try:
        st = os.lstat(dest_root / ANCHOR_FILE)
        if statmod.S_ISREG(st.st_mode):
            return ""
        return f"{ANCHOR_FILE} in the iCloud folder is not a plain file"
    except FileNotFoundError:
        pass
    if os.path.lexists(dest_root / f".{ANCHOR_FILE}.icloud"):
        return (f"{ANCHOR_FILE} is only an iCloud placeholder right now (not "
                f"downloaded), so this may not be the real iCloud folder yet")
    return (f"the iCloud folder has not been confirmed: {ANCHOR_FILE} is missing. "
            f"If iCloud Drive is mounted and Keepsake is the right folder, "
            f"confirm it once (Settings, Confirm iCloud folder), "
            f"otherwise this is an empty folder Docker made")


def write_anchor(dest_root: Path, now: datetime) -> str:
    """The explicit init step. Refuses a symlink root, an existing anchor, or a
    placeholder; creates the file exclusively."""
    problem = anchor_problem(dest_root)
    if not problem:
        return "the iCloud folder was already confirmed"
    if "placeholder" in problem or "not a plain file" in problem:
        raise Refused(problem)
    try:
        root_fd = os.open(dest_root, DIRFLAGS)
    except OSError:
        raise Refused(f"the iCloud folder {dest_root} is not mounted or is a symlink") from None
    try:
        fd = os.open(ANCHOR_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                     0o600, dir_fd=root_fd)
        with os.fdopen(fd, "w") as handle:
            json.dump({"confirmed": now.isoformat(timespec="seconds"),
                       "note": "written once by Keepsake after a person confirmed "
                               "this is the real iCloud Keepsake folder"}, handle)
    finally:
        os.close(root_fd)
    return "the iCloud folder is confirmed; folders can now be copied into it"


def resolve_source(cfg: Config, job: Job) -> Path:
    live, why = root_live(cfg, job.root)
    if not live:
        raise Refused(f"{job.root} is unavailable: {why}, so nothing was copied "
                      f"or deleted", "skipped")
    root = cfg.roots[job.root]
    src = root / job.source
    if not src.exists():
        raise Refused(f"the folder {job.source!r} does not exist on {job.root} "
                      f"(renamed, or the share is not fully mounted); the "
                      f"iCloud copy was left alone")
    if not inside(root, src):
        raise Refused(f"{job.source!r} resolves outside {job.root}; refused")
    if not src.is_dir():
        raise Refused(f"{job.source!r} is not a folder")
    return src


class Throttle:
    def __init__(self, cfg: Config) -> None:
        self.pause = cfg.pause_ms / 1000.0
        self.bps = cfg.max_mbps * 125_000  # megabit/s -> bytes/s
        self.t0 = time.monotonic()
        self.sent = 0

    def after_file(self) -> None:
        if self.pause:
            time.sleep(self.pause)

    def after_chunk(self, n: int) -> None:
        if not self.bps:
            return
        self.sent += n
        ahead = self.sent / self.bps - (time.monotonic() - self.t0)
        if ahead > 0:
            time.sleep(min(ahead, 5))


# ── one run ────────────────────────────────────────────────────────────────

def run_job(cfg: Config, job: Job, *, accept_shrink: bool = False,
            progress: Callable[[str], None] | None = None,
            now: datetime | None = None,
            track: Progress | None = None) -> RunReport:
    """Run one job. Never raises for a guard: the report carries the verdict.
    `track` receives what the run is doing and can ask it to stop."""
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    report = RunReport(job=job.name, started=now.isoformat(timespec="seconds"))
    say = progress or (lambda _m: None)
    trk = track or current_progress() or Progress()
    try:
        _run(cfg, job, report, accept_shrink is True, say, now, trk)
    except Cancelled:
        report.cancelled = True
        report.result = "skipped"
        report.error = (f"stopped at your request after copying "
                        f"{report.files_copied} files. Nothing was left half-written "
                        f"and your originals were not touched. It tries again on "
                        f"its schedule, or press Sync")
    except Refused as refusal:
        report.result = refusal.result
        report.error = str(refusal)
    except Exception as exc:  # noqa: BLE001 - a crash must be a report, not silence
        logger.exception("[%s] unexpected failure", job.name)
        report.result = "failed"
        report.error = f"unexpected error: {type(exc).__name__}: {exc}"
    report.duration_s = round(time.monotonic() - started, 1)
    return report


def _read_owner(job_fd: int) -> dict | None:
    try:
        fd = os.open(OWNER_FILE, os.O_RDONLY | NOFOLLOW, dir_fd=job_fd)
    except OSError:
        return None
    try:
        with os.fdopen(fd) as handle:
            return json.loads(handle.read(65536))
    except (OSError, ValueError):
        return None


def _write_owner(job_fd: int, job: Job, now: datetime) -> None:
    try:
        st = os.stat(OWNER_FILE, dir_fd=job_fd, follow_symlinks=False)
        if not statmod.S_ISREG(st.st_mode):
            raise Refused(f"{OWNER_FILE} in the iCloud folder is not a plain file")
    except FileNotFoundError:
        pass
    tmp = new_tmp_name()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600,
                 dir_fd=job_fd)
    with os.fdopen(fd, "w") as handle:
        json.dump({"job": job.name, "root": job.root, "source": job.source,
                   "created": now.isoformat(timespec="seconds"),
                   "note": "written by Keepsake; marks this folder as its own"},
                  handle)
    os.replace(tmp, OWNER_FILE, src_dir_fd=job_fd, dst_dir_fd=job_fd)


def _shrink_problem(cfg: Config, baseline: dict, source: dict[str, Entry],
                    src_bytes: int) -> str:
    keep = (100 - cfg.shrink_pct) / 100
    if len(source) < baseline["files"] * keep:
        return (f"the source shrank from {baseline['files']} to {len(source)} "
                f"files (more than {cfg.shrink_pct}%)")
    if baseline.get("bytes") and src_bytes < baseline["bytes"] * keep:
        return (f"the source shrank from {baseline['bytes'] / GB:.2f} GB to "
                f"{src_bytes / GB:.2f} GB (more than {cfg.shrink_pct}%)")
    sample = baseline.get("sample") or []
    if sample:
        present = sum(1 for p in sample if p in source)
        if present < len(sample) * keep:
            return (f"only {present} of {len(sample)} remembered files are still "
                    f"in the source (more than {cfg.shrink_pct}% moved or gone)")
    return ""


def _run(cfg: Config, job: Job, report: RunReport, accept_shrink: bool,
         say: Callable[[str], None], now: datetime, trk: Progress) -> None:
    src = resolve_source(cfg, job)
    dest_root = cfg.dest_root
    if not dest_root.is_dir():
        raise Refused(f"the iCloud folder {dest_root} is not mounted; nothing "
                      f"was copied", "skipped")
    problem = anchor_problem(dest_root)
    if problem:
        raise Refused(f"{problem}. Nothing was copied", "skipped")
    try:
        root_fd = os.open(dest_root, DIRFLAGS)
    except FileNotFoundError:
        raise Refused(f"the iCloud folder {dest_root} is not mounted; nothing "
                      f"was copied", "skipped") from None
    except OSError:
        raise Refused(f"the iCloud folder {dest_root} is a symlink or not a "
                      f"folder; refused") from None
    job_fd = src_fd = -1
    try:
        try:
            job_fd = open_job_dir(root_fd, job, create=True)
        except OSError:
            raise Refused("the iCloud destination folder is a symlink or not a "
                          "plain folder; refused") from None
        try:
            root_src_fd = os.open(cfg.roots[job.root], DIRFLAGS)
            try:
                src_fd = open_dir_at(root_src_fd, job.source.split("/"))
            finally:
                os.close(root_src_fd)
        except OSError:
            raise Refused(f"{job.source!r} could not be opened as a plain folder "
                          f"(a symlink on the way?); refused") from None
        ctx = Ctx(src_fd=src_fd, job_fd=job_fd, job_dir=dest_root / job.dest_path, src=src,
                  trk=trk)
        _run_inner(cfg, job, report, accept_shrink, say, now, ctx, trk)
    finally:
        for fd in (src_fd, job_fd, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


@dataclass
class Plan:
    """What a run would do, worked out from the source and the destination
    without changing either. The real run and the preview share this."""
    source: dict[str, Entry]
    real: dict[str, Entry]
    placeholders: set[str]
    known: dict
    owner: dict | None
    baseline: dict | None
    safe_first: bool
    to_copy: list[str]
    gone: list[str]
    copy_bytes: int
    src_bytes: int
    shrink_problem: str = ""        # set when the shrink guard would refuse


def _plan_sync(cfg: Config, job: Job, report: RunReport, accept_shrink: bool,
               say: Callable[[str], None], src_fd: int, job_fd: int,
               job_dir: Path, read_only: bool = False,
               trk: Progress | None = None) -> Plan:
    """Every guard that decides whether a run may start, and the list of what it
    would copy and retire. Raises Refused. With `read_only` nothing is written,
    not even the clean-up of our own leftover temp files, and `job_fd` may be -1
    for an iCloud folder that does not exist yet (it then reads as empty)."""
    if job_fd >= 0:
        check_reserved(job_fd)

    manifest_path = cfg.manifest_dir / f"{job.name}.json"
    manifest = load_json(manifest_path, {"files": {}, "baseline": None})
    baseline = manifest.get("baseline")

    trk = trk or Progress()
    say("scanning the source")
    trk.phase_start("reading-source")
    source = scan_source(src_fd, job.excludes, report, trk)
    report.files_total = len(source)
    src_bytes = sum(s for s, _ in source.values())

    if not source:
        raise Refused(f"{job.source!r} looks empty right now (an unmounted or "
                      f"emptied share looks exactly like this); the iCloud copy "
                      f"was left alone", guard="source")
    clash = sorted({r.split("/")[0] for r in source
                    if r.split("/")[0] in RESERVED or r.split("/")[0] == OWNER_FILE})
    if clash:
        raise Refused(f"the source holds {clash[0]!r} at its top level, a name "
                      f"Keepsake keeps for its own use in the iCloud folder; "
                      f"nothing was changed. Rename it or exclude it",
                      guard="source")
    if report.unreadable_dirs:
        raise Refused(f"{report.unreadable_dirs} folders could not be read, so the "
                      f"source list is incomplete; nothing was changed. Fix the "
                      f"permissions or exclude them", guard="source")
    shrink_problem = ""
    if baseline:
        shrink_problem = _shrink_problem(cfg, baseline, source, src_bytes)
        if shrink_problem and not accept_shrink:
            raise Refused(f"{shrink_problem}; it may have been deleted or unmounted. "
                          f"Nothing was changed. If this is intended, run Sync "
                          f"now with the 'accept shrink' option", guard="shrink")

    say("reading what iCloud already holds")
    trk.phase_start("reading-icloud")
    owner = _read_owner(job_fd) if job_fd >= 0 else None
    if job_fd >= 0:
        real, placeholders = scan_dest(job_fd, report,
                                       clean=owner is not None and not read_only,
                                       trk=trk)
    else:
        real, placeholders = {}, set()
    if report.dest_errors:
        raise Refused(f"{report.dest_errors} entries in the iCloud folder could "
                      f"not be read; nothing was changed", guard="destination")
    known = manifest.get("files", {})

    # A destination that is not provably ours is never adopted silently.
    # `.deleted` / `.removed` hold someone's data too: a folder with only those
    # is not empty, and its dated folders must not be pruned as ours.
    reserved_content = False
    for name in RESERVED if job_fd >= 0 else ():
        try:
            rfd = open_dir_at(job_fd, [name])
        except OSError:
            continue
        try:
            reserved_content = reserved_content or bool(os.listdir(rfd))
        finally:
            os.close(rfd)
    populated = bool(real or placeholders or reserved_content)
    if owner is not None and owner.get("job") not in (job.name, None):
        raise Refused(f"the iCloud folder belongs to job {owner.get('job')!r}, "
                      f"not {job.name!r}; refused", guard="first-run")
    if owner is None and populated and not job.adopt_existing:
        raise Refused(
            f"the iCloud folder {job.dest_path!r} already holds files that this service "
            f"did not put there. Nothing was changed. Pick another folder name, "
            f"or set adopt_existing on the job to take it over", guard="first-run")
    safe_first = baseline is None
    report.safe_first_run = safe_first
    if safe_first and populated:
        report.notes.append("first run over an existing folder: copying only, "
                            "nothing already in iCloud is retired")

    trk.phase_start("comparing", total_files=len(source))
    to_copy: list[str] = []
    for rel, entry in source.items():
        if rel in real:
            if same(entry, real[rel]):
                continue
            to_copy.append(rel)
        elif rel in placeholders:
            # Evicted by macOS ("Optimize Storage"): the bytes are in iCloud.
            # Not re-copied while the source still matches what we copied.
            old = known.get(rel)
            if old and same(entry, (old[0], old[1])):
                report.placeholders_skipped += 1
                continue
            to_copy.append(rel)
        else:
            to_copy.append(rel)
    gone = [r for r in real if r not in source] + \
           [r for r in placeholders if r not in source and r not in real]
    if safe_first and gone:
        report.left_alone = len(gone)
        gone = []
    copy_bytes = sum(source[r][0] for r in to_copy)

    cap = (job.max_gb or cfg.max_dest_gb) * GB
    trk.phase_start("measuring")
    current = tree_bytes(job_dir, trk)
    if current + copy_bytes > cap:
        raise Refused(
            f"this folder would take {(current + copy_bytes) / GB:.1f} GB in "
            f"iCloud, over its {cap / GB:.0f} GB limit. Nothing was copied. "
            f"Raise the limit for this folder, or exclude something",
            guard="size cap")
    free = shutil.disk_usage(cfg.dest_root).free
    reserve = cfg.min_free_gb * GB
    if free - copy_bytes < reserve:
        raise Refused(
            f"copying {copy_bytes / GB:.1f} GB would leave this Mac with "
            f"{(free - copy_bytes) / GB:.0f} GB free, under the "
            f"{cfg.min_free_gb} GB reserve. Nothing was copied",
            guard="disk reserve")
    report.notes.append(f"plan: {len(to_copy)} to copy "
                        f"({copy_bytes / GB:.2f} GB), {len(gone)} to retire")
    say(report.notes[-1])
    return Plan(source=source, real=real, placeholders=placeholders, known=known,
                owner=owner, baseline=baseline, safe_first=safe_first,
                to_copy=to_copy, gone=gone, copy_bytes=copy_bytes,
                src_bytes=src_bytes, shrink_problem=shrink_problem)


def _run_inner(cfg: Config, job: Job, report: RunReport, accept_shrink: bool,
               say: Callable[[str], None], now: datetime, ctx: Ctx,
               trk: Progress | None = None) -> None:
    job_dir, job_fd = ctx.job_dir, ctx.job_fd
    manifest_path = cfg.manifest_dir / f"{job.name}.json"
    trk = trk or Progress()
    plan = _plan_sync(cfg, job, report, accept_shrink, say, ctx.src_fd, job_fd,
                      job_dir, trk=trk)
    source, real, placeholders, known = (plan.source, plan.real,
                                         plan.placeholders, plan.known)
    owner, baseline, safe_first = plan.owner, plan.baseline, plan.safe_first
    to_copy, gone, src_bytes = plan.to_copy, plan.gone, plan.src_bytes

    # The mount can vanish between the first check and the first write; a stale
    # folder with the same names must not overwrite current iCloud files.
    live, why = root_live(cfg, job.root)
    if not live:
        raise Refused(f"{job.root} went away before copying started ({why}); "
                      f"nothing was changed", "skipped")
    if owner is None:
        _write_owner(job_fd, job, now)

    # Nothing is replaced or retired in this run's own mirror mode before its
    # replacement exists; replacements are kept even in a safe first run.
    keep = job.mode == "mirror-keep-deleted" or safe_first
    stamp = now.date().isoformat()
    throttle = Throttle(cfg)
    copied: list[str] = []
    failed: set[str] = set()
    last_say = time.monotonic()
    trk.phase_start("copying", total_files=len(to_copy), total_bytes=plan.copy_bytes)
    for index, rel in enumerate(to_copy, 1):
        size, mtime_ns = source[rel]
        staged: tuple[int, str] | None = None
        trk.add(rel)            # a stop is honoured here, before a file is begun
        try:
            staged = stage_copy(ctx, rel, size, mtime_ns, throttle)
            if staged is None:
                report.changed_during_copy += 1
                failed.add(rel)
            else:
                pfd, tmp = staged
                name = rel.rsplit("/", 1)[-1]
                # The replacement exists. Only now does the old version leave.
                if rel in real and keep:
                    move_aside(job_fd, rel, stamp)
                    report.files_moved_to_deleted += 1
                ph = placeholder_for(rel)
                ph_here = rel in placeholders and lexists_at(pfd, f".{name}.icloud")
                if ph_here and keep:
                    move_aside(job_fd, ph, stamp)
                    report.files_moved_to_deleted += 1
                os.replace(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
                staged = None
                try:
                    if ph_here and not keep:
                        # plain mirror: the placeholder goes only after the new
                        # copy is in place
                        try:
                            os.unlink(f".{name}.icloud", dir_fd=pfd)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(pfd)
                copied.append(rel)
                report.note_change("updated" if rel in real or rel in placeholders
                                   else "added", rel)
                report.files_copied += 1
                report.bytes_copied += size
        except OSError as exc:
            report.copy_errors += 1
            failed.add(rel)
            logger.warning("[%s] could not copy %s: %s", job.name, rel, exc)
            if report.copy_errors == 1:
                report.error = f"could not copy {rel!r}: {exc.strerror or exc}"
            if staged is not None:
                try:
                    os.unlink(staged[1], dir_fd=staged[0])
                    os.close(staged[0])
                except OSError:
                    pass
            if report.copy_errors > 50:
                raise Refused("more than 50 files failed to copy; stopped. "
                              f"First problem: {report.error}") from exc
        trk.add(rel, 1)
        throttle.after_file()
        if time.monotonic() - last_say > 30:
            say(f"copied {index}/{len(to_copy)} files")
            last_say = time.monotonic()

    # The mount may have gone while we copied: do not retire on a vanished view.
    if gone:
        live, why = root_live(cfg, job.root)
        if not live:
            raise Refused(f"{job.root} went away during the run ({why}); "
                          f"nothing was retired", "skipped")
    for rel in gone:
        try:
            path = rel if rel in real else placeholder_for(rel)
            if keep:
                move_aside(job_fd, path, stamp)
                report.files_moved_to_deleted += 1
            else:
                dfd = open_dir_at(job_fd, path.split("/")[:-1])
                try:
                    os.unlink(path.rsplit("/", 1)[-1], dir_fd=dfd)
                finally:
                    os.close(dfd)
            report.note_change("removed", rel)
        except OSError as exc:
            logger.warning("[%s] could not retire %s: %s", job.name, rel, exc)
    trk.phase_start("tidying")
    prune_empty_dirs(job_fd)
    try:
        dfd = open_dir_at(job_fd, [DELETED_DIR])
    except OSError:
        dfd = -1
    if dfd >= 0:
        try:
            prune_deleted_fd(dfd, job.keep_days, now.date())
        finally:
            os.close(dfd)

    # Verify from CONTENT: read the destination back.
    say("verifying what is in iCloud")
    trk.phase_start("verifying")
    real2, placeholders2 = scan_dest(job_fd, report, clean=True, trk=trk)
    report.evicted = len(placeholders2)
    expected = {r: e for r, e in source.items() if r not in failed}
    missing = [r for r in expected if r not in real2 and r not in placeholders2]
    extra = [] if safe_first else [r for r in real2 if r not in source]
    wrong_size = [r for r, e in expected.items()
                  if r in real2 and real2[r][0] != e[0]]
    report.verified_files = len(expected) - len(missing)
    trk.phase_start("measuring")
    report.dest_bytes = tree_bytes(job_dir, trk)
    pool = [r for r in expected if r in real2 and expected[r][0] <= SAMPLE_MAX_BYTES]
    random.shuffle(pool)
    bad_hash: list[str] = []
    trk.phase_start("checking", total_files=min(SAMPLE, len(pool)))
    for rel in pool[:SAMPLE]:
        trk.add(rel)
        report.sampled += 1
        try:
            if sha256_fd(open_source(ctx.src_fd, rel)) != sha256(job_dir / rel):
                bad_hash.append(rel)
        except OSError:
            bad_hash.append(rel)
        trk.add(rel, 1)
    problems = []
    if report.dest_errors:
        problems.append(f"{report.dest_errors} entries in iCloud could not be read")
    if missing:
        problems.append(f"{len(missing)} files are missing from iCloud "
                        f"(e.g. {missing[0]!r})")
    if extra:
        problems.append(f"{len(extra)} files in iCloud are not in the source")
    if wrong_size:
        problems.append(f"{len(wrong_size)} files have the wrong size "
                        f"(e.g. {wrong_size[0]!r})")
    if bad_hash:
        problems.append(f"content check failed for {bad_hash[0]!r}")
    if report.copy_errors:
        problems.append(f"{report.copy_errors} files could not be copied")
    if report.changed_during_copy:
        problems.append(f"{report.changed_during_copy} files changed while being "
                        f"read, so iCloud holds an older version or none; they "
                        f"are retried next run (exclude files that always change)")
    # What we have really put in step is remembered; a file that failed keeps
    # its previous record, so an evicted older version is still recognised.
    files = {r: [e[0], e[1]] for r, e in source.items() if r not in failed}
    for r in failed:
        if r in known:
            files[r] = known[r]
    if problems:
        report.result = "failed"
        report.error = "; ".join(problems)
        atomic_write_json(manifest_path, {"files": files, "baseline": baseline})
        return

    sample = random.sample(sorted(source), min(PATH_SAMPLE, len(source)))
    atomic_write_json(manifest_path, {
        "files": files,
        "baseline": {"files": len(source), "bytes": src_bytes, "sample": sample},
    })
    report.result = "ok"
    if report.placeholders_skipped:
        report.notes.append(f"{report.placeholders_skipped} files are evicted by "
                            f"macOS (iCloud holds them); left alone")


# ── preview: the same decisions, nothing written ────────────────────────────

@dataclass
class PreviewReport:
    """What the next sync would do. `refused` is True when a guard would stop it;
    `error` is then the same sentence a real run would give."""
    job: str
    refused: bool = False
    error: str = ""
    guard: str = ""                 # shrink, size cap, disk reserve, first-run, ...
    result: str = "ok"              # ok | failed | skipped, as the run would report
    shrink_accepted: bool = False   # the preview assumed 'accept shrink'
    shrink_problem: str = ""        # what the shrink guard would say, if it would
    cancelled: bool = False         # a person stopped the preview
    files_total: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    bytes: int = 0                  # to copy
    safe_first_run: bool = False
    left_alone: int = 0
    samples: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def preview_job(cfg: Config, job: Job, *, accept_shrink: bool = False,
                progress: Callable[[str], None] | None = None,
                track: Progress | None = None) -> PreviewReport:
    """Walk the source and the iCloud folder and report what a sync started now
    would add, update and retire, and whether a guard would refuse it. STRICTLY
    READ-ONLY: it never creates the iCloud folder, never writes the owner marker,
    never removes a leftover temp file, and touches no manifest, state or log.
    Never raises for a guard: the report carries the verdict."""
    out = PreviewReport(job=job.name, shrink_accepted=accept_shrink is True)
    say = progress or (lambda _m: None)
    work = RunReport(job=job.name)      # reused only for its counters and samples
    trk = track or current_progress() or Progress()
    try:
        plan = _preview_plan(cfg, job, work, accept_shrink is True, say, trk)
    except Cancelled:
        out.cancelled = out.refused = True
        out.result, out.guard = "skipped", "cancelled"
        out.error = "The preview was stopped at your request. It changed nothing."
        return out
    except Refused as refusal:
        out.refused = True
        out.result = refusal.result
        out.error = str(refusal)
        out.guard = refusal.guard or ("mount" if refusal.result == "skipped" else "")
        out.files_total = work.files_total
        return out
    except Exception as exc:  # noqa: BLE001 - a crash must be a report, not silence
        logger.exception("[%s] unexpected failure in a preview", job.name)
        out.refused = True
        out.result = "failed"
        out.error = f"unexpected error: {type(exc).__name__}: {exc}"
        return out
    for rel in plan.to_copy:
        work.note_change("updated" if rel in plan.real or rel in plan.placeholders
                         else "added", rel)
    for rel in plan.gone:
        work.note_change("removed", rel)
    out.files_total = work.files_total
    out.added, out.updated, out.removed = (work.files_added, work.files_updated,
                                           work.files_removed)
    out.bytes = plan.copy_bytes
    out.safe_first_run = plan.safe_first
    out.left_alone = work.left_alone
    out.samples = list(work.change_samples)
    out.notes = [n for n in work.notes if not n.startswith("plan:")]
    out.shrink_problem = plan.shrink_problem
    return out


def _preview_plan(cfg: Config, job: Job, report: RunReport, accept_shrink: bool,
                  say: Callable[[str], None], trk: Progress | None = None) -> Plan:
    resolve_source(cfg, job)
    dest_root = cfg.dest_root
    if not dest_root.is_dir():
        raise Refused(f"the iCloud folder {dest_root} is not mounted; nothing "
                      f"was copied", "skipped", "mount")
    problem = anchor_problem(dest_root)
    if problem:
        raise Refused(f"{problem}. Nothing was copied", "skipped", "anchor")
    try:
        root_fd = os.open(dest_root, DIRFLAGS)
    except FileNotFoundError:
        raise Refused(f"the iCloud folder {dest_root} is not mounted; nothing "
                      f"was copied", "skipped", "mount") from None
    except OSError:
        raise Refused(f"the iCloud folder {dest_root} is a symlink or not a "
                      f"folder; refused") from None
    job_fd = src_fd = -1
    try:
        try:
            job_fd = open_job_dir(root_fd, job, create=False)
        except FileNotFoundError:
            job_fd = -1             # not made yet: a real first run would make it
        except OSError:
            raise Refused("the iCloud destination folder is a symlink or not a "
                          "plain folder; refused") from None
        try:
            root_src_fd = os.open(cfg.roots[job.root], DIRFLAGS)
            try:
                src_fd = open_dir_at(root_src_fd, job.source.split("/"))
            finally:
                os.close(root_src_fd)
        except OSError:
            raise Refused(f"{job.source!r} could not be opened as a plain folder "
                          f"(a symlink on the way?); refused") from None
        return _plan_sync(cfg, job, report, accept_shrink, say, src_fd, job_fd,
                          dest_root / job.dest_path, read_only=True, trk=trk)
    finally:
        for fd in (src_fd, job_fd, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def retire_job_folder(cfg: Config, job: Job, now: datetime | None = None) -> str:
    """Soft-delete a job's iCloud copy: moved to .removed/, pruned after 30 days.
    Only a folder that carries THIS job's owner marker is moved."""
    now = now or datetime.now(timezone.utc)
    try:
        root_fd = os.open(cfg.dest_root, DIRFLAGS)
    except OSError:
        return "the iCloud folder is not available, so nothing was moved"
    try:
        try:
            parent_fd = (open_dir_at(root_fd, [job.group]) if job.group
                         else os.dup(root_fd))
        except OSError:
            return "there was no iCloud copy to remove"
        try:
            job_fd = open_job_dir(root_fd, job)
        except (OSError, Refused):
            os.close(parent_fd)
            return "there was no iCloud copy to remove"
        try:
            owner = _read_owner(job_fd)
            opened = os.fstat(job_fd)
        finally:
            os.close(job_fd)
        try:
            named = os.stat(job.dest, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            os.close(parent_fd)
            return "there was no iCloud copy to remove"
        if (named.st_ino, named.st_dev) != (opened.st_ino, opened.st_dev):
            os.close(parent_fd)
            return "the iCloud folder changed while it was being checked, so nothing was moved"
        if not owner or owner.get("job") != job.name:
            os.close(parent_fd)
            return (f"the iCloud folder {job.dest_path!r} is not marked as this "
                    f"service's copy of {job.name!r}, so it was not moved")
        try:
            rfd = open_dir_at(root_fd, [REMOVED_DIR], create=True)
        except OSError:
            os.close(parent_fd)
            return "the .removed folder in iCloud is not a real folder, so nothing was moved"
        try:
            label = f"{job.group}-{job.dest}" if job.group else job.dest
            target = f"{label}_{now.date().isoformat()}"
            if lexists_at(rfd, target):
                target = f"{target}-{int(time.time())}"
            os.rename(job.dest, target, src_dir_fd=parent_fd, dst_dir_fd=rfd)
        finally:
            os.close(rfd)
            os.close(parent_fd)
        return (f"the iCloud copy was moved to {REMOVED_DIR}/{target} and will "
                f"be deleted for good after {REMOVED_KEEP_DAYS} days")
    finally:
        os.close(root_fd)


# ── trust features: check a copy, list and restore deleted files ───────────
# All three go through the same no-symlink, directory-fd rules as a run. Check
# and list are read-only on both sides. Restore writes only inside the job's
# own iCloud folder (.restored/), never into the source.

def open_regular_at(base_fd: int, rel: str) -> tuple[int, os.stat_result]:
    """Open fd/rel for reading, one NOFOLLOW step at a time, and insist on a
    plain file. O_NONBLOCK so a FIFO swapped in cannot hang the reader. Raises
    OSError for a link, a folder, a device or anything missing on the way."""
    parts = rel.split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\x00" in rel:
        raise OSError(errno.EINVAL, "path is not plain and relative")
    dfd = open_dir_at(base_fd, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                     dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not statmod.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a plain file")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _digest(fd: int, size: int) -> str:
    """sha256 of exactly `size` bytes of an open file; does not close it. A file
    that is shorter or longer than it was when opened (growing, truncated) is an
    OSError, so a changing file can neither exceed a byte budget nor be hashed
    half and half."""
    digest = hashlib.sha256()
    left = size
    trk = current_progress()
    while left:
        block = os.read(fd, min(CHUNK, left))
        if not block:
            raise OSError(errno.EIO, "the file got shorter while it was read")
        left -= len(block)
        digest.update(block)
        if trk is not None:
            trk.add("", 0, len(block))      # a stop is honoured between blocks
    if os.read(fd, 1):
        raise OSError(errno.EIO, "the file grew while it was read")
    return digest.hexdigest()


@contextmanager
def _job_fds(cfg: Config, job: Job, need_source: bool) -> Iterator[tuple[int, int]]:
    """(source fd or -1, job folder fd), opened without following links. Never
    creates anything. Refused with a sentence when iCloud, the anchor, the copy
    or (when asked for) the source is not there."""
    src = resolve_source(cfg, job) if need_source else None
    dest_root = cfg.dest_root
    if not dest_root.is_dir():
        raise Refused(f"the iCloud folder {dest_root} is not mounted", "skipped")
    problem = anchor_problem(dest_root)
    if problem:
        raise Refused(problem, "skipped")
    try:
        root_fd = os.open(dest_root, DIRFLAGS)
    except OSError:
        raise Refused(f"the iCloud folder {dest_root} is a symlink or not a folder; "
                      f"refused") from None
    job_fd = src_fd = -1
    try:
        try:
            job_fd = open_job_dir(root_fd, job)
        except FileNotFoundError:
            raise Refused(f"there is no iCloud copy of {job.name!r} yet") from None
        except OSError:
            raise Refused("the iCloud destination folder is a symlink or not a "
                          "plain folder; refused") from None
        if src is not None:
            try:
                root_src_fd = os.open(cfg.roots[job.root], DIRFLAGS)
                try:
                    src_fd = open_dir_at(root_src_fd, job.source.split("/"))
                finally:
                    os.close(root_src_fd)
            except OSError:
                raise Refused(f"{job.source!r} could not be opened as a plain folder "
                              f"(a symlink on the way?); refused") from None
        yield src_fd, job_fd
    finally:
        for fd in (src_fd, job_fd, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _require_owner(job_fd: int, job: Job) -> None:
    owner = _read_owner(job_fd)
    if not owner or owner.get("job") != job.name:
        raise Refused(f"the iCloud folder {job.dest_path!r} is not marked as this "
                      f"service's copy of {job.name!r}, so it was left alone")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def rotation_key(seed: str, rel: str) -> str:
    """Where `rel` sits in the rotation: a hash of the seed and the path. The
    order depends only on the path, so a file added or removed between checks
    moves nobody else."""
    return hashlib.sha256(f"{seed}\0{rel}".encode()).hexdigest()


def count_evicted(cfg: Config, job: Job) -> int:
    """How many files in the job's iCloud folder are only a placeholder
    (`.<name>.icloud`) right now: macOS moved them out of this Mac (Optimize
    Storage). Read-only, by directory fd, never through a link. This is the one
    thing about iCloud's own state that is visible from here; whether Apple has
    uploaded anything is not."""
    with _job_fds(cfg, job, False) as (_, job_fd):
        _require_owner(job_fd, job)
        _real, placeholders = scan_dest(job_fd, RunReport(job=job.name), clean=False)
    return len(placeholders)


def verify_copy(cfg: Config, job: Job, now: datetime | None = None,
                cursor: dict | None = None, trk: Progress | None = None) -> dict:
    """Check a copy (see `_verify_copy`); `trk` is told what it is doing and may stop it."""
    trk = trk or current_progress() or Progress(op="check")
    with tracking(trk):
        return _verify_copy(cfg, job, now, cursor, trk)


def _verify_copy(cfg: Config, job: Job, now: datetime | None, cursor: dict | None,
                 trk: Progress) -> dict:
    """Check a copy: read up to VERIFY_FILES files back from the source and from
    iCloud and compare size and sha256. Read-only on both sides.

    Candidates come from the manifest the last good run left, in a rotating
    order: they are sorted by `rotation_key(seed, path)` and each check carries
    on after the last file the previous check looked at, wrapping round at the
    end, so repeated checks cover every file. `cursor` (`seed`, `after`) is read
    and then updated in place; without one the order starts from the beginning.
    A file whose source changed since then, or that macOS evicted, or that is
    too big, is skipped and counted: it is neither a match nor a mismatch. A
    check that could compare nothing is refused, never reported as a match."""
    now = now or datetime.now(timezone.utc)
    manifest = load_json(cfg.manifest_dir / f"{job.name}.json", {"files": {}})
    known = manifest.get("files") or {}
    if not known:
        raise Refused(f"there is nothing to check yet: no copy of {job.name!r} has "
                      f"finished")
    live, why = root_live(cfg, job.root)
    if not live:
        raise Refused(f"{job.root} is unavailable: {why}, so nothing was checked",
                      "skipped")
    seed = str((cursor or {}).get("seed") or "")
    after = str((cursor or {}).get("after") or "")
    keyed = sorted((rotation_key(seed, rel), rel) for rel in known)
    start = next((i for i, (key, _) in enumerate(keyed) if key > after), 0)
    paths = keyed[start:] + keyed[:start]
    matched = 0
    bad: list[tuple[str, str]] = []
    skipped = 0
    hashed = 0
    examined = 0
    last_key = after
    trk.phase_start("checking", total_files=min(VERIFY_FILES, len(paths)))
    counted = 0
    with _job_fds(cfg, job, True) as (src_fd, job_fd):
        _require_owner(job_fd, job)
        for key, rel in paths[:VERIFY_TRIES]:
            trk.add(rel, matched + len(bad) - counted)   # a stop is honoured here
            counted = matched + len(bad)
            if matched + len(bad) >= VERIFY_FILES:
                break
            size, mtime_ns = known[rel][0], known[rel][1]
            if size <= SAMPLE_MAX_BYTES and hashed and hashed + size > VERIFY_MAX_BYTES:
                break                       # this one is first in line next time
            examined += 1
            last_key = key
            if size > SAMPLE_MAX_BYTES or hashed + size > VERIFY_MAX_BYTES:
                skipped += 1
                continue
            sfd = dfd = -1
            try:
                try:
                    sfd, sst = open_regular_at(src_fd, rel)
                except OSError:
                    skipped += 1            # gone from the source since the run
                    continue
                if not same((sst.st_size, sst.st_mtime_ns), (size, mtime_ns)):
                    skipped += 1            # edited since: the next run copies it
                    continue
                try:
                    dfd, dst = open_regular_at(job_fd, rel)
                except FileNotFoundError:
                    head, _, tail = rel.rpartition("/")
                    try:
                        pfd = open_dir_at(job_fd, head.split("/") if head else [])
                        try:
                            evicted = lexists_at(pfd, f".{tail}.icloud")
                        finally:
                            os.close(pfd)
                    except OSError:
                        evicted = False
                    if evicted:
                        skipped += 1        # macOS holds it in iCloud, not on disk
                    else:
                        bad.append((rel, "is missing from iCloud"))
                    continue
                except OSError:
                    bad.append((rel, "is a link or not a plain file in iCloud"))
                    continue
                hashed += size
                if dst.st_size != sst.st_size:
                    bad.append((rel, "has a different size in iCloud"))
                    continue
                try:
                    src_hash = _digest(sfd, sst.st_size)
                    after_st = os.fstat(sfd)
                except OSError:
                    skipped += 1            # the source changed while it was read
                    continue
                try:
                    dst_hash = _digest(dfd, dst.st_size)
                    dafter = os.fstat(dfd)
                except OSError as exc:
                    bad.append((rel, f"could not be read back from iCloud "
                                     f"({exc.strerror or exc})"))
                    continue
                if (after_st.st_size, after_st.st_mtime_ns) != (sst.st_size, sst.st_mtime_ns):
                    skipped += 1            # changed while it was read
                elif (dafter.st_size, dafter.st_mtime_ns) != (dst.st_size, dst.st_mtime_ns):
                    bad.append((rel, "changed in iCloud while it was being read"))
                elif src_hash != dst_hash:
                    bad.append((rel, "has different content in iCloud"))
                else:
                    matched += 1
            finally:
                for fd in (sfd, dfd):
                    if fd >= 0:
                        os.close(fd)
    trk.add("", matched + len(bad) - counted)
    if cursor is not None:      # moved on even when nothing could be compared
        cursor["after"] = last_key
        cursor["examined"] = int(cursor.get("examined") or 0) + examined
        if cursor["examined"] >= len(keyed):     # every file has had its turn
            cursor["examined"] -= len(keyed)
            cursor["rounds"] = int(cursor.get("rounds") or 0) + 1
    checked = matched + len(bad)
    if checked == 0:
        raise Refused("none of the sampled files could be compared (they changed "
                      "since the last copy, are stored only in iCloud, or are too "
                      "big to read back), so nothing was checked. Try again after "
                      "the next run")
    rest = (f"; {_plural(skipped, 'other file')} could not be compared and "
            f"{'was' if skipped == 1 else 'were'} skipped" if skipped else "")
    if bad:
        first, reason = bad[0]
        more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
        message = (f"Checked {_plural(checked, 'file')}: {first!r} {reason}{more}"
                   f"{rest}")
    else:
        message = f"Checked {_plural(checked, 'file')}, all match{rest}"
    return {"at": now.isoformat(timespec="seconds"),
            "state": "mismatch" if bad else "match", "ok": not bad,
            "checked": checked, "matched": matched, "mismatched": len(bad),
            "skipped": skipped, "message": message,
            "first_mismatch": ({"path": bad[0][0], "reason": bad[0][1]}
                               if bad else None)}


def list_deleted(cfg: Config, job: Job, today: date | None = None) -> dict:
    """What is kept under .deleted/<date>/, read-only, by directory fd. Links are
    never followed or listed; anything not named like a date is ignored."""
    today = today or datetime.now(timezone.utc).date()
    entries: list[dict] = []
    links = scanned = 0
    truncated = False
    with _job_fds(cfg, job, False) as (_, job_fd):
        try:
            top = open_dir_at(job_fd, [DELETED_DIR])
        except FileNotFoundError:
            return {"entries": [], "truncated": False, "skipped_links": 0}
        except OSError:
            raise Refused(f"{DELETED_DIR} in the iCloud folder is a symlink or not a "
                          f"folder; refused") from None
        stack: list[tuple[str, str, date]] = []   # (date folder, path inside it, day)
        try:
            names = []
            for name in os.listdir(top):
                if FULL_DATE_RE.match(name):
                    names.append(name)
                    if len(names) > DELETED_LIST_MAX:
                        truncated = True
                        break
            for name in sorted(names, reverse=True):
                try:
                    day = date.fromisoformat(name)
                    if not statmod.S_ISDIR(os.stat(name, dir_fd=top,
                                                   follow_symlinks=False).st_mode):
                        links += 1
                        continue
                except (ValueError, OSError):
                    continue
                stack.append((name, "", day))
            stack.reverse()
            while stack and not truncated:
                name, rel_dir, day = stack.pop()
                try:
                    dfd = open_dir_at(top, [name, *(rel_dir.split("/") if rel_dir else [])])
                except OSError:
                    continue
                subdirs = []
                try:
                    with os.scandir(dfd) as it:
                        for e in it:
                            scanned += 1
                            if scanned > DELETED_SCAN_MAX or len(entries) >= DELETED_LIST_MAX:
                                truncated = True
                                break
                            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                            try:
                                if e.is_symlink():
                                    links += 1
                                elif e.is_dir(follow_symlinks=False):
                                    subdirs.append((name, rel, day))
                                elif e.is_file(follow_symlinks=False):
                                    entries.append({
                                        "date": name, "path": rel,
                                        "size": e.stat(follow_symlinks=False).st_size,
                                        "days_left": max(0, job.keep_days - (today - day).days)})
                            except OSError:
                                continue
                except OSError:
                    pass
                finally:
                    os.close(dfd)
                stack.extend(reversed(sorted(subdirs)))
        finally:
            os.close(top)
    entries.sort(key=lambda e: (e["date"], e["path"]), reverse=False)
    entries.sort(key=lambda e: e["date"], reverse=True)
    return {"entries": entries, "truncated": truncated, "skipped_links": links}


def _restore_parts(rel: object) -> list[str]:
    """The exact path parts of a file under a dated folder, or a refusal. Names
    are used as given (no trimming); everything that could climb out is refused."""
    if not isinstance(rel, str) or not rel:
        raise Refused("say which file to restore")
    if "\x00" in rel or rel.startswith("/"):
        raise Refused("that path is not allowed: it must be relative and plain")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise Refused("that path is not allowed: no '..', '.' or empty parts")
    return parts


def restore_deleted(cfg: Config, job: Job, day: object, rel: object,
                    trk: Progress | None = None) -> str:
    """Copy one kept file back (see `_restore_deleted`); `trk` may stop it."""
    trk = trk or current_progress() or Progress(op="restore")
    with tracking(trk):
        return _restore_deleted(cfg, job, day, rel, trk)


def _restore_deleted(cfg: Config, job: Job, day: object, rel: object,
                     trk: Progress) -> str:
    """Copy one kept file back, into <dest>/.restored/<date>/<same path>, inside
    the job's iCloud folder. Never into the source: it is mounted read-only by
    design and Keepsake only ever writes inside its own iCloud folder. The kept
    original stays where it is; a name that is already taken gets a number, an
    existing file is never replaced."""
    if not isinstance(day, str) or not FULL_DATE_RE.match(day):
        raise Refused("that is not a date folder from the deleted files list")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise Refused("that is not a date folder from the deleted files list") from None
    parts = _restore_parts(rel)
    with _job_fds(cfg, job, False) as (_, job_fd):
        _require_owner(job_fd, job)
        check_reserved(job_fd)
        try:
            sfd, st = open_regular_at(job_fd, "/".join([DELETED_DIR, day, *parts]))
        except FileNotFoundError:
            raise Refused("that file is not in .deleted any more (it may have "
                          "expired)") from None
        except OSError:
            raise Refused("that entry in .deleted is a link or not a plain file, "
                          "so it was not restored") from None
        try:
            job_dir = cfg.dest_root / job.dest_path
            cap = (job.max_gb or cfg.max_dest_gb) * GB
            trk.phase_start("measuring")
            current = tree_bytes(job_dir, trk)
            if current + st.st_size > cap:
                raise Refused(
                    f"restoring this file ({st.st_size / GB:.2f} GB) would take the "
                    f"folder to {(current + st.st_size) / GB:.1f} GB in iCloud, over "
                    f"its {cap / GB:.0f} GB limit. Nothing was restored")
            free = shutil.disk_usage(cfg.dest_root).free
            if free - st.st_size < cfg.min_free_gb * GB:
                raise Refused(
                    f"restoring this file would leave this Mac with "
                    f"{(free - st.st_size) / GB:.0f} GB free, under the "
                    f"{cfg.min_free_gb} GB reserve. Nothing was restored")
            where = [RESTORED_DIR, day, *parts[:-1]]
            try:
                tfd = open_dir_at(job_fd, where, create=True)
            except OSError:
                raise Refused(f"{RESTORED_DIR} in the iCloud folder is not a real "
                              f"folder, so nothing was restored") from None
            trk.phase_start("copying", total_files=1, total_bytes=st.st_size)
            try:
                target = _copy_restored(sfd, st, tfd, parts[-1])
            except OSError as exc:
                raise Refused(f"could not restore the file: {exc.strerror or exc}") \
                    from exc
            finally:
                os.close(tfd)
        finally:
            os.close(sfd)
    shown = "/".join([RESTORED_DIR, day, *parts[:-1], target])
    return (f"restored a copy to {shown} inside the iCloud copy of {job.name!r}. "
            f"The original stays in {DELETED_DIR} and your source was not touched. "
            f"Finder hides folders that start with a dot: press Command-Shift-Period "
            f"to show them")


def _plain_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "\x00" not in name \
        and "/" not in name


def _day_arg(day: object) -> str:
    if not isinstance(day, str) or not FULL_DATE_RE.match(day):
        raise Refused("that is not a date folder from the deleted files list")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise Refused("that is not a date folder from the deleted files list") from None
    return day


def plan_restore_batch(cfg: Config, job: Job, day: object = None) -> dict:
    """What "restore all" would take on, read-only: the plain files kept under
    `.deleted/<day>/` (every date when `day` is None), their count and bytes,
    and what would be skipped and why. Links are never followed: a link, or a
    folder reached through one, is skipped and named. Refused when there are
    more than RESTORE_BATCH_MAX files, so a batch is always one a person can
    review."""
    only = None if day is None else _day_arg(day)
    with _job_fds(cfg, job, False) as (_, job_fd):
        _require_owner(job_fd, job)
        plan = _plan(job_fd, only)
    return {k: v for k, v in plan.items() if k != "_files"}


def _plan(job_fd: int, only: str | None) -> dict:
    files: list[tuple[str, str, int]] = []        # (date, path, size)
    skipped: list[tuple[str, str]] = []           # (date/path, reason)
    try:
        top = open_dir_at(job_fd, [DELETED_DIR])
    except FileNotFoundError:
        top = -1
    except OSError:
        raise Refused(f"{DELETED_DIR} in the iCloud folder is a symlink or not a "
                      f"folder; refused") from None
    scanned = 0
    try:
        if top >= 0:
            names = sorted(n for n in os.listdir(top) if FULL_DATE_RE.match(n))
            for name in names:
                try:
                    date.fromisoformat(name)
                except ValueError:
                    continue
                if only is not None and name != only:
                    continue
                try:
                    isdir = statmod.S_ISDIR(os.stat(name, dir_fd=top,
                                                    follow_symlinks=False).st_mode)
                except OSError:
                    continue
                if not isdir:
                    skipped.append((name, "is a link, not a folder"))
                    continue
                stack = [""]
                while stack:
                    rel_dir = stack.pop()
                    try:
                        dfd = open_dir_at(top, [name, *(rel_dir.split("/") if rel_dir else [])])
                    except OSError:
                        skipped.append((f"{name}/{rel_dir}", "could not be opened"))
                        continue
                    try:
                        with os.scandir(dfd) as it:
                            entries = sorted(it, key=lambda e: e.name)
                        for e in entries:
                            scanned += 1
                            if scanned > DELETED_SCAN_MAX:
                                raise Refused("there are too many kept entries to "
                                              "restore in one go. Restore one date "
                                              "at a time, or single files")
                            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                            shown = f"{name}/{rel}"
                            try:
                                if not _plain_name(e.name):
                                    skipped.append((shown, "has a name that is not allowed"))
                                elif e.is_symlink():
                                    skipped.append((shown, "is a link"))
                                elif e.is_dir(follow_symlinks=False):
                                    stack.append(rel)
                                elif e.is_file(follow_symlinks=False):
                                    files.append((name, rel,
                                                  e.stat(follow_symlinks=False).st_size))
                                    if len(files) > RESTORE_BATCH_MAX:
                                        raise Refused(
                                            f"more than {RESTORE_BATCH_MAX} files are "
                                            f"kept. Restore one date at a time, or "
                                            f"single files")
                                else:
                                    skipped.append((shown, "is not a plain file"))
                            except OSError:
                                skipped.append((shown, "could not be read"))
                    except OSError:
                        skipped.append((f"{name}/{rel_dir}", "could not be read"))
                    finally:
                        os.close(dfd)
    finally:
        if top >= 0:
            os.close(top)
    if only is not None and not files and not skipped:
        raise Refused(f"nothing is kept under {DELETED_DIR}/{only}")
    if not files and not skipped:
        raise Refused(f"nothing is kept under {DELETED_DIR} to restore")
    days: dict[str, list[int]] = {}
    for d, _rel, size in files:
        entry = days.setdefault(d, [0, 0])
        entry[0] += 1
        entry[1] += size
    return {"files": len(files), "bytes": sum(f[2] for f in files),
            "days": [{"date": d, "files": c, "bytes": b} for d, (c, b) in sorted(days.items())],
            "skipped": [{"path": p, "reason": r} for p, r in skipped],
            "_files": files}


def restore_batch(cfg: Config, job: Job, day: object = None,
                  expect_files: object = None, trk: Progress | None = None) -> dict:
    """Restore a date or everything kept (see `_restore_batch`); `trk` may stop it."""
    trk = trk or current_progress() or Progress(op="restore")
    with tracking(trk):
        return _restore_batch(cfg, job, day, expect_files, trk)


def _restore_batch(cfg: Config, job: Job, day: object, expect_files: object,
                   trk: Progress) -> dict:
    """Copy every kept file of one date (or of every date, when `day` is None)
    into <dest>/.restored/<date>/<same path>, inside the job's iCloud folder.

    The same guards as `restore_deleted`, with the size cap and the free-disk
    reserve judged up front for the WHOLE batch, so a batch is either started
    or refused before anything is written. Never into the source, never over an
    existing file (a taken name gets a number), links are skipped and named, each
    file is written under a temp name and linked into place. A file that cannot
    be copied is skipped with its reason and the rest carry on. `expect_files`,
    when given, is the count the person confirmed: a different count refuses the
    batch. Returns the counts and a plain sentence; raises Refused when nothing
    was restored."""
    only = None if day is None else _day_arg(day)
    if expect_files is not None and (isinstance(expect_files, bool)
                                     or not isinstance(expect_files, int)):
        raise Refused("expect_files must be a whole number")
    with _job_fds(cfg, job, False) as (_, job_fd):
        _require_owner(job_fd, job)
        check_reserved(job_fd)
        trk.phase_start("reading-icloud")
        plan = _plan(job_fd, only)
        files = plan["_files"]
        if not files:
            raise Refused("nothing could be restored: "
                          + _skipped_sentence(plan["skipped"]))
        if expect_files is not None and expect_files != plan["files"]:
            raise Refused(f"{plan['files']} files are kept now, not the "
                          f"{expect_files} you were shown. Nothing was restored; "
                          f"look again")
        total = plan["bytes"]
        job_dir = cfg.dest_root / job.dest_path
        cap = (job.max_gb or cfg.max_dest_gb) * GB
        trk.phase_start("measuring")
        current = tree_bytes(job_dir, trk)
        if current + total > cap:
            raise Refused(
                f"restoring {_plural(plan['files'], 'file')} ({total / GB:.2f} GB) "
                f"would take the folder to {(current + total) / GB:.1f} GB in "
                f"iCloud, over its {cap / GB:.0f} GB limit. Nothing was restored")
        free = shutil.disk_usage(cfg.dest_root).free
        if free - total < cfg.min_free_gb * GB:
            raise Refused(
                f"restoring {_plural(plan['files'], 'file')} would leave this Mac "
                f"with {max(0, free - total) / GB:.0f} GB free, under the "
                f"{cfg.min_free_gb} GB reserve. Nothing was restored")
        skipped = list(plan["skipped"])
        done = done_bytes = 0
        dates: set[str] = set()
        trk.phase_start("copying", total_files=len(files), total_bytes=total)
        for d, rel, size in files:
            trk.add(f"{d}/{rel}")      # a stop is honoured here, before a file is begun
            parts = rel.split("/")
            sfd = tfd = -1
            try:
                try:
                    sfd, st = open_regular_at(job_fd, "/".join([DELETED_DIR, d, *parts]))
                except OSError:
                    skipped.append({"path": f"{d}/{rel}",
                                    "reason": "is gone, or is now a link or not a plain file"})
                    continue
                if st.st_size != size:
                    skipped.append({"path": f"{d}/{rel}",
                                    "reason": "changed size while the batch was starting"})
                    continue
                try:
                    tfd = open_dir_at(job_fd, [RESTORED_DIR, d, *parts[:-1]], create=True)
                except OSError:
                    raise Refused(f"{RESTORED_DIR} in the iCloud folder is not a real "
                                  f"folder; {_plural(done, 'file')} restored before "
                                  f"it stopped") from None
                try:
                    _copy_restored(sfd, st, tfd, parts[-1])
                except OSError as exc:
                    skipped.append({"path": f"{d}/{rel}",
                                    "reason": f"could not be copied ({exc.strerror or exc})"})
                    continue
                done += 1
                done_bytes += size
                dates.add(d)
                trk.add("", 1)
            finally:
                for fd in (sfd, tfd):
                    if fd >= 0:
                        os.close(fd)
    skipped = [s if isinstance(s, dict) else {"path": s[0], "reason": s[1]}
               for s in skipped]
    if not done:
        raise Refused("nothing could be restored: "
                      + _skipped_sentence([(s["path"], s["reason"]) for s in skipped]))
    where = (f"{RESTORED_DIR}/{sorted(dates)[0]}" if len(dates) == 1
             else f"{RESTORED_DIR}/<date> ({len(dates)} dates)")
    message = (f"restored {_plural(done, 'file')} ({done_bytes / 1024 ** 2:.1f} MB) "
               f"into {where} inside the iCloud copy of {job.name!r}")
    if skipped:
        message += (f". Skipped {len(skipped)}: "
                    + _skipped_sentence([(s["path"], s["reason"]) for s in skipped]))
    message += (f". The originals stay in {DELETED_DIR} and your source was not "
                f"touched. Finder hides folders that start with a dot: press "
                f"Command-Shift-Period to show them")
    return {"restored": done, "bytes": done_bytes, "skipped": skipped,
            "dates": sorted(dates), "message": message}


def _skipped_sentence(skipped: list) -> str:
    items = [(s["path"], s["reason"]) if isinstance(s, dict) else s for s in skipped]
    if not items:
        return "nothing was kept"
    shown = "; ".join(f"{p!r} {r}" for p, r in items[:RESTORE_SKIP_NAMES])
    more = len(items) - RESTORE_SKIP_NAMES
    return shown + (f"; and {more} more" if more > 0 else "")


def _copy_restored(sfd: int, st: os.stat_result, tfd: int, name: str) -> str:
    """Copy the open file into the directory fd under a temp name, then link it
    into place without replacing anything that exists. Returns the final name."""
    tmp = new_tmp_name()
    trk = current_progress()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600,
                 dir_fd=tfd)
    try:
        left = st.st_size              # never more than the size the cap was checked for
        with os.fdopen(fd, "wb") as fout:
            while left:
                block = os.read(sfd, min(CHUNK, left))
                if not block:
                    raise OSError(errno.EIO, "the file changed while it was being read")
                fout.write(block)
                left -= len(block)
                if trk is not None:
                    trk.add("", 0, len(block))   # may raise Cancelled: the finally removes the temp file
            if os.read(sfd, 1):
                raise OSError(errno.EIO, "the file changed while it was being read")
            after = os.fstat(sfd)
            if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise OSError(errno.EIO, "the file changed while it was being read")
            fout.flush()
            os.fsync(fout.fileno())
            os.utime(fout.fileno(), ns=(st.st_mtime_ns, st.st_mtime_ns))
        stem, ext = os.path.splitext(name)
        for n in range(1, 1000):
            target = name if n == 1 else f"{stem} ({n}){ext}"
            try:
                os.link(tmp, target, src_dir_fd=tfd, dst_dir_fd=tfd,
                        follow_symlinks=False)
            except FileExistsError:
                continue
            return target
        raise OSError(errno.EEXIST, "too many files with that name already")
    finally:
        try:
            os.unlink(tmp, dir_fd=tfd)
        except OSError:
            pass
