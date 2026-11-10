"""Shared fixtures. Everything lives in a temp directory: no real iCloud, no real share."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from keepsake.config import Config, Job


def make_cfg(tmp: Path, setup: bool = False, **kw) -> Config:
    """A config on a temp tree. The guided setup is recorded as finished unless a
    test asks for `setup=True`, so tests of everything else are not locked out of
    the REST API the way a brand-new install is."""
    src = tmp / "sources"
    src.mkdir(exist_ok=True)
    dest = tmp / "icloud"
    dest.mkdir(exist_ok=True)
    (tmp / "data").mkdir(exist_ok=True)
    args = dict(data_dir=tmp / "data", dest_root=dest, roots={"sources": src},
                max_dest_gb=10, min_free_gb=0, shrink_pct=20)
    args.update(kw)
    if args["dest_root"] == dest and not (dest / ".keepsake-anchor").exists():
        (dest / ".keepsake-anchor").write_text("{}")  # the one-time confirmation
    if not setup:
        (tmp / "data" / "setup.json").write_text('{"complete": true}')
    return Config(**args)


def make_job(**kw) -> Job:
    args = dict(name="photos", root="sources", source="Photos", dest="photos")
    args.update(kw)
    return Job(**args)


def write(path: Path, text: str = "x", mtime: Optional[int] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path
