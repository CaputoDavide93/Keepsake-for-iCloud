"""Locations Keepsake may read, editable from the page inside broad read-only mounts.

A container cannot mount a new path by itself. So the deployment mounts a broad parent
folder read-only at a fixed prefix (``/hostfs`` by default, ``KEEPSAKE_LOCATION_PREFIXES``),
and the person chooses, on the page, which folders under it count as a location.
``KEEPSAKE_ROOTS`` seeds the list and those entries are "built in": the page cannot
remove them. Added ones are stored by name in ``locations.json`` in the data folder.

Every rule is enforced here, on the server, whatever the page sends:

  * the folder is reached one NOFOLLOW step at a time from a prefix, the same way the
    engine walks a source, so a symlink anywhere on the way is refused;
  * it must lie inside a mounted prefix, and that mount must be read-only (the file
    system itself says so), so a location can never be written;
  * it may not be, hold or sit inside the iCloud folder or the data folder, nor
    overlap another location (two names for one tree would defeat the check that
    stops the same files being copied twice);
  * names are letters, digits and hyphens, unique whatever their case;
  * a location a folder job uses cannot be removed, and the refusal names the job.
"""

from __future__ import annotations

import os
import stat as statmod
from pathlib import Path
from typing import Any, Dict, List, Tuple

from keepsake import engine
from keepsake.config import (
    NAME_RE,
    Config,
    ConfigError,
    Job,
    atomic_write_json,
    load_json,
    looks_secret,
)

FILE = "locations.json"
BROWSE_CAP = 500


def stored_file(cfg: Config) -> Path:
    return cfg.data_dir / FILE


def load_stored(cfg: Config) -> Dict[str, str]:
    raw = load_json(stored_file(cfg), {})
    items = raw.get("locations") if isinstance(raw, dict) else None
    out: Dict[str, str] = {}
    for name, path in (items or {}).items():
        if isinstance(name, str) and isinstance(path, str) and NAME_RE.match(name) \
                and path.startswith("/"):
            out[name] = path
    return out


def save_stored(cfg: Config, stored: Dict[str, str]) -> None:
    atomic_write_json(stored_file(cfg), {"locations": dict(sorted(stored.items()))})


def merge_stored(cfg: Config) -> None:
    """Put the stored locations beside the built-in ones. A built-in name wins."""
    for name, path in load_stored(cfg).items():
        cfg.roots.setdefault(name, Path(path))


def _norm(path: str) -> str:
    return "/" + "/".join(p for p in path.split("/") if p)


def _within(inner: str, outer: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def prefixes(cfg: Config) -> List[str]:
    return [_norm(p) for p in cfg.location_prefixes if p.strip()]


def _clean(path: Any) -> Tuple[str, List[str]]:
    if not isinstance(path, str) or not path.strip():
        raise ConfigError("choose a folder")
    if "\x00" in path or "\\" in path:
        raise ConfigError("that folder name contains a character that is not allowed")
    if not path.startswith("/"):
        raise ConfigError("the folder must be given as a full path inside a mounted location")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ConfigError("the folder path may not contain '.' or '..'")
    return "/" + "/".join(parts), parts


def _prefix_for(cfg: Config, path: str) -> str:
    for prefix in prefixes(cfg):
        if _within(path, prefix):
            return prefix
    where = ", ".join(prefixes(cfg)) or "none"
    raise ConfigError(f"that folder is outside the places Keepsake can read (mounted: "
                      f"{where}). Mount the parent folder read-only first; see the docs")


def open_inside(cfg: Config, path: str) -> Tuple[int, str, str]:
    """Open `path` as a directory fd, walking from its prefix one NOFOLLOW step at a
    time. Returns (fd, normalised path, prefix). The caller closes the fd."""
    clean, parts = _clean(path)
    prefix = _prefix_for(cfg, clean)
    try:
        pfd = os.open(prefix, engine.DIRFLAGS)
    except OSError:
        raise ConfigError(f"{prefix} is not mounted here, or is a link, so nothing can "
                          f"be chosen under it") from None
    try:
        rel = clean[len(prefix):].split("/")
        rel = [p for p in rel if p]
        try:
            return engine.open_dir_at(pfd, rel), clean, prefix
        except OSError:
            raise ConfigError(f"{clean} could not be opened as a plain folder (it may "
                              f"not exist, or a link is on the way); refused") from None
    finally:
        os.close(pfd)


def read_only(fd: int) -> bool:
    try:
        return bool(os.statvfs(fd).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _identity(path: Path) -> Tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def check_new(cfg: Config, name: Any, path: Any, existing: Dict[str, Path]) -> Tuple[str, str]:
    """The (name, path) of a location that may be added, or ConfigError in plain words."""
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ConfigError("the name must be 1-40 characters: letters, digits and "
                          "hyphens (no spaces), starting with a letter or digit")
    clash = next((k for k in existing if k.lower() == name.lower()), None)
    if clash:
        raise ConfigError(f"a location named {clash!r} already exists (names that "
                          f"differ only by case count as the same)")
    fd, clean, _prefix = open_inside(cfg, path)
    try:
        st = os.fstat(fd)
        if not statmod.S_ISDIR(st.st_mode):
            raise ConfigError(f"{clean} is not a folder")
        if cfg.location_require_ro and not read_only(fd):
            raise ConfigError(f"{clean} is not on a read-only mount, so Keepsake will "
                              f"not use it: a location must be mounted read-only so it "
                              f"can never be changed")
        ident = (st.st_dev, st.st_ino)
    finally:
        os.close(fd)
    # Compared on real paths: the walk above followed no link inside the mount, but the
    # mount's own parents (and the paths set in the environment) may be links.
    mine = _norm(os.path.realpath(clean))
    for label, folder in (("the iCloud folder", cfg.dest_root), ("the Keepsake data folder", cfg.data_dir)):
        real = _norm(os.path.realpath(folder))
        if _within(mine, real) or _within(real, mine) or ident == _identity(folder):
            raise ConfigError(f"{clean} is {label}, holds it, or sits inside it; a "
                              f"location can never overlap where copies are written")
    for other, other_path in existing.items():
        theirs = _norm(os.path.realpath(other_path))
        if _within(mine, theirs) or _within(theirs, mine) or ident == _identity(other_path):
            raise ConfigError(f"{clean} overlaps the location {other!r}; two locations "
                              f"over the same files would defeat the check that stops "
                              f"one folder being copied twice")
    return name, clean


def browse(cfg: Config, path: str = "") -> dict:
    """Subfolders for the 'Add location' dialog, limited to the mounted prefixes."""
    roots = prefixes(cfg)
    if not path:
        found = []
        for prefix in roots:
            try:
                fd = os.open(prefix, engine.DIRFLAGS)
            except OSError:
                continue
            try:
                found.append({"name": prefix, "path": prefix, "warn": False,
                              "readonly": read_only(fd)})
            finally:
                os.close(fd)
        return {"path": "", "parent": None, "folders": found, "truncated": False,
                "readonly": None, "prefixes": roots}
    fd, clean, prefix = open_inside(cfg, path)
    try:
        ro = read_only(fd)
        try:
            with os.scandir(fd) as it:
                entries = sorted((e for e in it if not e.name.startswith(".")),
                                 key=lambda e: e.name.lower())
        except OSError as exc:
            raise ConfigError(f"that folder cannot be read: {exc.strerror}") from exc
        folders: List[dict] = []
        truncated = False
        for e in entries:
            try:
                if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            sub = f"{clean}/{e.name}"
            folders.append({"name": e.name, "path": sub, "warn": looks_secret(sub)})
            if len(folders) >= BROWSE_CAP:
                truncated = True
                break
    finally:
        os.close(fd)
    parent = clean.rsplit("/", 1)[0] or "/"
    return {"path": clean, "parent": parent if clean != prefix else "",
            "folders": folders, "truncated": truncated, "readonly": ro, "prefixes": roots}


def describe(cfg: Config, builtin: frozenset, jobs: List[Job]) -> dict:
    """The location list with what each one is, for the Locations card."""
    out = []
    for name in sorted(cfg.roots):
        live, why = engine.root_live(cfg, name)
        out.append({"name": name, "path": str(cfg.roots[name]),
                    "builtin": name in builtin, "available": live,
                    "why": "" if live else why,
                    "jobs": sorted(j.name for j in jobs if j.root == name)})
    mounted = []
    for prefix in prefixes(cfg):
        try:
            fd = os.open(prefix, engine.DIRFLAGS)
        except OSError:
            mounted.append({"path": prefix, "mounted": False, "readonly": None})
            continue
        try:
            mounted.append({"path": prefix, "mounted": True, "readonly": read_only(fd)})
        finally:
            os.close(fd)
    usable = [m for m in mounted if m["mounted"] and (m["readonly"] or not cfg.location_require_ro)]
    why = ""
    if not usable:
        why = ("No folder is mounted for choosing yet. Add a read-only mount at "
               + (prefixes(cfg)[0] if prefixes(cfg) else "/hostfs")
               + " in docker-compose.yml, then reload this page.")
    return {"locations": out, "prefixes": mounted, "can_add": bool(usable), "why": why}
