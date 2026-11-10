#!/usr/bin/env python3
"""Start Keepsake on a throwaway fixture tree, for UI tests and screenshots.

Builds generic demo folders (Photos, Documents, Projects) in a temp directory,
runs a few jobs so the page has real states to show, prints `PORT=<n>` and
serves until killed. Never touches a real folder.

    PYTHONPATH=src python3 tools/ui/fixture_server.py [--password PW]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from keepsake.config import Config, atomic_write_json
from keepsake.engine import count_evicted
from keepsake.server import serve
from keepsake.service import Service


def build(tmp: Path, password: str, anchor: bool = True, webhook: str = "",
          jobs_wanted: bool = True, setup_open: bool = False) -> Service:
    src, icloud, data = tmp / "sources", tmp / "icloud", tmp / "data"
    for d in (src, icloud, data):
        d.mkdir(parents=True)
    for i in range(12):
        (src / "Photos/2025/Holidays").mkdir(parents=True, exist_ok=True)
        (src / f"Photos/2025/Holidays/IMG_{i:04d}.jpg").write_bytes(b"p" * (40_000 + i))
    for i in range(6):
        (src / "Documents/Taxes").mkdir(parents=True, exist_ok=True)
        (src / f"Documents/Taxes/return-{2018 + i}.pdf").write_bytes(b"d" * 9_000)
    for i in range(20):
        (src / "Projects/website").mkdir(parents=True, exist_ok=True)
        (src / f"Projects/website/page-{i}.html").write_bytes(b"h" * 2_000)
    (src / "Music/Albums").mkdir(parents=True)
    (src / "Music/Albums/a.mp3").write_bytes(b"m" * 5_000)
    (src / "Keys-and-passwords").mkdir()
    # Two folders with the same name under different parents, and one more to keep:
    # the Add folder page names the copies holidays and holidays-2.
    for year in (2019, 2020):
        (src / f"Archive/{year}/Holidays").mkdir(parents=True)
        (src / f"Archive/{year}/Holidays/note-{year}.txt").write_bytes(b"a" * 1_000)
    (src / "Scans").mkdir()
    (src / "Scans/receipt.pdf").write_bytes(b"s" * 3_000)
    # A folder someone made in iCloud Drive by hand: the Add folder page offers it
    # as a destination only with a tick, because Keepsake did not create it.
    (icloud / "family-stuff").mkdir()
    (icloud / "family-stuff/readme.txt").write_bytes(b"theirs")
    # The broad "mounted" parent the Add location dialog browses (a temp folder here, so
    # it is writable: the fixture turns the read-only check off, the tests cover it).
    hostfs = tmp / "hostfs"
    for sub in ("HomeFolder/Documents", "HomeFolder/Pictures", "ExternalDrive/Backups"):
        (hostfs / sub).mkdir(parents=True)
    (hostfs / "HomeFolder/Documents/note.txt").write_bytes(b"n" * 100)
    cfg = Config(data_dir=data, dest_root=icloud, roots={"sources": src},
                 location_prefixes=(str(hostfs),), location_require_ro=False,
                 min_free_gb=0, max_dest_gb=50, host="127.0.0.1", port=0,
                 password=password, webhook_url=webhook)
    svc = Service(cfg, None if webhook else [])
    if not setup_open:
        # The demo pages are the normal ones: the guided setup is recorded as done.
        # `--setup` leaves it open, so the page is only the wizard.
        svc.setup["complete"] = True
        atomic_write_json(cfg.setup_file, svc.setup)
    if not anchor:
        return svc
    svc.confirm_icloud()
    if not jobs_wanted:
        return svc
    jobs = [
        {"name": "photos", "root": "sources", "source": "Photos", "schedule": {"every_hours": 6}},
        {"name": "documents", "root": "sources", "source": "Documents", "schedule": {"at": "03:30"}},
        {"name": "projects", "root": "sources", "source": "Projects", "schedule": {"every_hours": 12}},
        {"name": "music", "root": "sources", "source": "Music", "enabled": False},
    ]
    for job in jobs:
        svc.add_job(job)
    for _ in range(4):
        svc.run_next()
    # One Documents file is deleted at the source: it is kept in .deleted, which
    # gives the Deleted files page a row to show. Photos gets a copy check.
    (src / "Documents/Taxes/return-2018.pdf").unlink()
    svc.sync_now("documents")
    svc.run_next()
    svc.verify_job("photos")
    # An older date under .deleted, so the page groups by date and "Restore all
    # from this date" has a second group to act on.
    older = icloud / "documents/.deleted" / (date.today() - timedelta(days=3)).isoformat()
    (older / "Taxes").mkdir(parents=True)
    (older / "Taxes/return-2017.pdf").write_bytes(b"d" * 8_000)
    (older / "Taxes/notes.txt").write_bytes(b"n" * 300)
    # macOS Optimize Storage evicted three photos after the last run. The count
    # is read with the engine's helper, so the demo needs no extra run (which
    # would also replace the "12 added" the other pages show).
    for i in (0, 1, 2):
        (icloud / f"photos/2025/Holidays/IMG_{i:04d}.jpg").rename(
            icloud / f"photos/2025/Holidays/.IMG_{i:04d}.jpg.icloud")
    svc.state["photos"]["evicted"] = count_evicted(svc.cfg, svc._find("photos"))
    svc._save_state()
    # Projects loses most of its files: the shrink guard refuses the next run.
    for p in list((src / "Projects/website").glob("*.html"))[:15]:
        p.unlink()
    svc.sync_now("projects")
    svc.run_next()
    seed_history(svc)
    return svc


# Earlier nights for the demo only (c copied, n nothing new, w paused, . no run),
# oldest first, ending the day before today. Today's bar is the real run above.
DEMO_NIGHTS = {
    "photos": "ccc.ccnccccnc",
    "documents": "c.ccccn.cccn.",
    "projects": "n.nccccnccc.n",
}


def demo_detail(name: str, n: int, day: str) -> dict:
    """What a seeded demo night did: generic file names, counts that add up."""
    kinds = ["added"] * (n % 4 + 1) + ["updated"] * (n % 3)
    samples = [{"kind": kind, "path": f"{name.title()}/2025/file-{n:02d}-{i}.txt"}
               for i, kind in enumerate(kinds)]
    return {"at": f"{day}T03:30:00+00:00", "runs": 1,
            "added": kinds.count("added"), "updated": kinds.count("updated"), "removed": 0,
            "removed_how": "moved to .deleted", "samples": samples}


def seed_history(svc: Service) -> None:
    """Earlier nights for the demo. The oldest copied nights carry no detail, as
    nights recorded before details existed do; the newer ones do."""
    today = date.fromisoformat(svc.today())
    outcome = {"c": "copied", "n": "nothing", "w": "paused"}
    for name, pattern in DEMO_NIGHTS.items():
        past = []
        for i, ch in enumerate(pattern):
            if ch not in outcome:
                continue
            day = (today - timedelta(days=len(pattern) - i)).isoformat()
            entry = {"day": day, "outcome": outcome[ch]}
            if ch == "c" and i >= 4:
                entry["detail"] = demo_detail(name, i, day)
            past.append(entry)
        st = svc.state[name]
        st["history"] = past + st["history"]
    svc._save_state()


def demo_worker(svc: Service, live: bool) -> None:
    """Runs what a person asks for (Sync now, Preview) so the page can show the
    result. Without `live` it never starts a scheduled sync or check, so the
    demo's states stay as they were built; with it, the real worker loop runs."""
    while True:
        try:
            if live:
                ran = svc.run_next() or svc.run_due_check()
                svc.housekeeping()
                if ran:
                    continue
            elif svc.queue or svc.preview_queue:
                svc.run_next()
                continue
        except Exception:  # noqa: BLE001 - a demo helper must not die
            pass
        time.sleep(0.25)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--workdir", default="/tmp/keepsake-demo", help="throwaway tree, deleted on exit")
    ap.add_argument("--webhook", default="", help="a webhook address, to show the test alert's result for a channel that fails")
    ap.add_argument("--no-anchor", action="store_true", help="start with the iCloud folder unconfirmed")
    ap.add_argument("--no-jobs", action="store_true", help="confirm the iCloud folder but add no folders")
    ap.add_argument("--setup", action="store_true", help="leave the guided setup open: the page is only the wizard (combine with --no-anchor or --no-jobs)")
    ap.add_argument("--live-worker", action="store_true", help="also run scheduled syncs and checks, like the real worker")
    ap.add_argument("--pause", default="", help="start paused: 1h, tomorrow or 1w")
    args = ap.parse_args()
    # A fixed, obviously fake location so screenshots show a clean path. Concurrent
    # runs get their own folder through --workdir.
    tmp = Path(args.workdir)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        svc = build(tmp, args.password, not args.no_anchor, args.webhook, not args.no_jobs, args.setup)
        if args.pause:
            svc.update_settings({"pause": {"preset": args.pause}})
        svc.cfg = replace(svc.cfg, port=args.port)
        httpd = serve(svc)
        print(f"PORT={httpd.server_address[1]}", flush=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        threading.Thread(target=demo_worker, args=(svc, args.live_worker), daemon=True).start()
        sys.stdin.read()  # run until the caller closes stdin
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
