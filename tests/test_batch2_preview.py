"""Preview next sync: the engine's plan mode must decide exactly like a real run
and must never write anywhere. The tripwire below makes any write (a create, a
delete, a rename, a write-mode open) raise, and the snapshots prove that the
iCloud folder, the state, the manifests, the jobs and the activity log are the
same bytes with the same times afterwards."""

import contextlib
import hashlib
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from keepsake import engine
from keepsake.config import ConfigError
from keepsake.service import Service
from tests.helpers import write
from tests.test_service import Base


def snapshot(*roots):
    """Every entry under each root with its type, size, times and a content hash."""
    out = {}
    for root in roots:
        for dirpath, dirs, files in os.walk(root, followlinks=False):
            for name in sorted(dirs + files):
                path = os.path.join(dirpath, name)
                st = os.lstat(path)
                digest = ""
                if os.path.isfile(path) and not os.path.islink(path):
                    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                out[path] = (st.st_mode, st.st_size, st.st_mtime_ns, digest)
    return out


@contextlib.contextmanager
def no_writes():
    """Raise on anything that would change the file system."""
    real_open, real_io_open = os.open, __import__("io").open
    flags_bad = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guard_os_open(path, flags, *a, **k):
        if flags & flags_bad:
            raise AssertionError(f"write attempted: os.open({path!r})")
        return real_open(path, flags, *a, **k)

    def guard_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            raise AssertionError(f"write attempted: open({file!r}, {mode!r})")
        return real_io_open(file, mode, *a, **k)

    def guard_fdopen(fd, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            raise AssertionError(f"write attempted: fdopen({fd}, {mode!r})")
        return real_io_open(fd, mode, *a, **k)

    def blocked(name):
        def fn(*a, **k):
            raise AssertionError(f"write attempted: os.{name}{a!r}")
        return fn

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(os, "open", guard_os_open))
        stack.enter_context(mock.patch.object(os, "fdopen", guard_fdopen))
        stack.enter_context(mock.patch("builtins.open", guard_open))
        stack.enter_context(mock.patch("io.open", guard_open))
        for name in ("mkdir", "makedirs", "unlink", "remove", "rmdir", "rename",
                     "replace", "symlink", "link", "utime", "chmod", "truncate",
                     "write", "ftruncate"):
            stack.enter_context(mock.patch.object(os, name, blocked(name)))
        yield


class PreviewBase(Base):
    def synced(self, **kw):
        self.add(**kw)
        self.assertTrue(self.svc.run_next())
        self.assertEqual(self.view()["result"], "ok")

    def watched(self):
        return snapshot(self.cfg.dest_root, self.cfg.data_dir)

    def run_preview(self, accept_shrink=False, name="photos"):
        self.svc.preview(name, accept_shrink)
        with no_writes():
            self.assertTrue(self.svc.run_next())
        preview = self.svc.preview_view(name)["preview"]
        # The tripwire's AssertionError would otherwise be swallowed into a refusal.
        self.assertNotIn("write attempted", preview["message"])
        return preview


class PreviewWritesNothing(PreviewBase):
    def change_source(self):
        src = self.tmp / "sources/Photos/2025"
        write(src / "new.jpg", "brand new", 1_700_000_100)
        write(src / "0.jpg", "changed bytes!!", 1_700_000_200)
        (src / "1.jpg").unlink()

    def test_the_destination_and_every_state_file_are_untouched(self):
        self.synced()
        self.change_source()
        before = self.watched()
        self.assertGreater(len(before), 8)
        preview = self.run_preview()
        self.assertEqual(before, self.watched())
        self.assertEqual((preview["added"], preview["updated"], preview["removed"]), (1, 1, 1))
        self.assertFalse(preview["would_refuse"])

    def test_it_reports_what_the_next_real_run_then_does(self):
        self.synced()
        self.change_source()
        preview = self.run_preview()
        self.svc.sync_now("photos")
        self.svc.run_next()
        real = self.view()["changes"]
        self.assertEqual((preview["added"], preview["updated"], preview["removed"]),
                         (real["added"], real["updated"], real["removed"]))
        self.assertEqual(preview["samples"], real["samples"])
        self.assertEqual(preview["bytes"], self.view()["bytes_copied"])

    def test_a_folder_that_does_not_exist_yet_is_not_created(self):
        self.add()
        self.svc.queue.clear()          # the add woke the worker; no run yet
        dest = self.cfg.dest_root / "photos"
        self.assertFalse(dest.exists())
        before = self.watched()
        preview = self.run_preview()
        self.assertFalse(dest.exists(), "a preview must not create the iCloud folder")
        self.assertEqual(before, self.watched())
        self.assertEqual(preview["added"], 5)
        self.assertTrue(preview["first_run"])

    def test_our_own_leftover_temp_file_is_left_alone(self):
        self.synced()
        tmp = self.cfg.dest_root / "photos" / ".icsync-0123456789abcdef.tmp"
        tmp.write_text("half a copy")
        before = self.watched()
        preview = self.run_preview()
        self.assertFalse(preview["would_refuse"], preview["message"])
        self.assertTrue(tmp.exists(), "a preview must not clean up")
        self.assertEqual(before, self.watched())
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertFalse(tmp.exists(), "a real run does clean it up")

    def test_no_activity_line_no_history_no_state_change(self):
        self.synced()
        self.change_source()
        state_before = repr(self.svc.state)
        log_before = self.cfg.activity_file.read_bytes()
        history_before = self.view()["history"]
        self.run_preview()
        self.assertEqual(log_before, self.cfg.activity_file.read_bytes())
        self.assertEqual(state_before, repr(self.svc.state))
        self.assertEqual(history_before, self.view()["history"])

    def test_a_missing_manifest_and_an_old_state_file_still_preview_safely(self):
        self.synced()
        (self.cfg.manifest_dir / "photos.json").unlink()
        before = self.watched()
        preview = self.run_preview()
        self.assertTrue(preview["first_run"])
        self.assertEqual(before, self.watched())

    def test_the_tripwire_itself_catches_a_write(self):
        self.synced()
        with no_writes():
            with self.assertRaises(AssertionError):
                (self.tmp / "x").write_text("nope")
            with self.assertRaises(AssertionError):
                os.mkdir(self.tmp / "y")


class PreviewSaysWhatARunWould(PreviewBase):
    def refusal(self, guard, **cfg):
        """Preview, then really run: both must give the same sentence."""
        preview = self.run_preview()
        self.assertTrue(preview["would_refuse"], preview)
        self.assertEqual(preview["guard"], guard)
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual(preview["message"], self.view()["error"])
        return preview

    def test_shrink_guard(self):
        self.synced()
        for i in range(1, 5):
            (self.tmp / f"sources/Photos/2025/{i}.jpg").unlink()
        preview = self.refusal("shrink")
        self.assertIn("shrank", preview["message"])
        self.assertEqual((preview["added"], preview["removed"]), (0, 0))

    def test_accepting_the_shrink_previews_what_would_be_moved(self):
        self.synced()
        for i in range(1, 5):
            (self.tmp / f"sources/Photos/2025/{i}.jpg").unlink()
        before = self.watched()
        preview = self.run_preview(accept_shrink=True)
        self.assertFalse(preview["would_refuse"])
        self.assertEqual(preview["removed"], 4)
        self.assertEqual(preview["removed_how"], "moved to .deleted")
        self.assertIn("would refuse this run without 'accept shrink'", preview["shrink_note"])
        self.assertEqual(before, self.watched())

    def test_only_the_json_boolean_true_accepts_a_shrink(self):
        self.synced()
        for i in range(1, 5):
            (self.tmp / f"sources/Photos/2025/{i}.jpg").unlink()
        for bad in ("true", 1, "yes", None):
            self.svc.preview("photos", bad)
            self.svc.run_next()
            self.assertTrue(self.svc.preview_view("photos")["preview"]["would_refuse"], bad)

    def test_size_cap(self):
        self.synced()
        self.svc.update_job("photos", {"max_gb": 1})
        self.svc.cfg = replace(self.svc.cfg, max_dest_gb=1)
        write(self.tmp / "sources/Photos/2025/big.bin", "x")
        job = self.svc._find("photos")
        with mock.patch.object(engine, "tree_bytes", return_value=2 * engine.GB):
            preview = engine.preview_job(self.svc.cfg, job)
        self.assertTrue(preview.refused)
        self.assertEqual(preview.guard, "size cap")
        self.assertIn("over its 1 GB limit", preview.error)

    def test_disk_reserve(self):
        self.synced()
        cfg = replace(self.svc.cfg, min_free_gb=10 ** 7)
        preview = engine.preview_job(cfg, self.svc._find("photos"))
        self.assertTrue(preview.refused)
        self.assertEqual(preview.guard, "disk reserve")
        self.assertIn("reserve", preview.error)

    def test_first_run_rule_a_folder_that_is_not_ours(self):
        self.add()
        self.svc.queue.clear()
        write(self.cfg.dest_root / "photos/stranger.txt", "not ours")
        preview = self.refusal("first-run")
        self.assertIn("did not put there", preview["message"])

    def test_first_run_over_an_owned_folder_says_nothing_would_be_retired(self):
        self.synced()
        (self.cfg.manifest_dir / "photos.json").unlink()
        write(self.cfg.dest_root / "photos/extra.txt", "kept by an earlier run")
        preview = self.run_preview()
        self.assertTrue(preview["first_run"])
        self.assertEqual(preview["removed"], 0)
        self.assertEqual(preview["left_alone"], 1)
        self.assertTrue(any("nothing already in iCloud is retired" in n
                            for n in preview["notes"]))

    def test_an_unconfirmed_icloud_folder_is_skipped_like_a_run(self):
        self.synced()
        (self.cfg.dest_root / ".keepsake-anchor").unlink()
        preview = self.run_preview()
        self.assertTrue(preview["would_refuse"])
        self.assertEqual((preview["result"], preview["guard"]), ("skipped", "anchor"))
        self.assertIn("has not been confirmed", preview["message"])

    def test_a_source_that_is_gone_is_a_mount_refusal(self):
        self.synced()
        import shutil
        shutil.rmtree(self.tmp / "sources/Photos")
        preview = self.run_preview()
        self.assertTrue(preview["would_refuse"])
        self.assertIn("does not exist", preview["message"])

    def test_nothing_new_is_said_plainly(self):
        self.synced()
        preview = self.run_preview()
        self.assertEqual(preview["message"], "A sync started now would find nothing new.")
        self.assertEqual(preview["label"], "Preview")
        self.assertTrue(preview["changed_nothing"])

    def test_at_most_fifty_examples_but_the_counts_are_complete(self):
        self.synced()
        for i in range(70):
            write(self.tmp / f"sources/Photos/2025/n{i:02d}.jpg", f"n{i}", 1_700_000_300)
        preview = self.run_preview()
        self.assertEqual(preview["added"], 70)
        self.assertEqual(len(preview["samples"]), 50)

    def test_plain_mirror_says_removed_not_moved(self):
        self.synced(mode="mirror")
        (self.tmp / "sources/Photos/2025/0.jpg").unlink()
        write(self.tmp / "sources/Photos/2025/z.jpg", "z", 1_700_000_300)
        preview = self.run_preview()
        self.assertEqual(preview["removed_how"], "removed")
        self.assertIn("1 removed", preview["message"])


class PreviewOnTheWorker(PreviewBase):
    def test_it_waits_in_the_queue_and_never_runs_beside_a_copy(self):
        self.synced()
        self.svc.sync_now("photos")
        with self.assertRaises(ConfigError):
            self.svc.preview("photos")      # busy: refused with a sentence
        self.svc.run_next()
        self.svc.preview("photos")
        self.assertTrue(self.view()["previewing"])
        self.assertEqual(self.view()["status"], "ok")      # not "queued" or "running"
        with self.assertRaises(ConfigError):
            self.svc.sync_now("photos")     # one job at a time
        self.assertTrue(self.svc.run_next())
        self.assertFalse(self.view()["previewing"])
        self.assertIsNone(self.svc.running)

    def test_progress_is_visible_while_it_reads(self):
        self.synced()
        seen = []

        def spy(cfg, job, accept_shrink=False, progress=None):
            progress("scanning the source")
            seen.append((self.svc.running, self.svc.running_kind, self.view()["progress"],
                         self.view()["previewing"], self.view()["status"]))
            return engine.preview_job(cfg, job, accept_shrink=accept_shrink)

        self.svc.preview("photos")
        with mock.patch("keepsake.service.preview_job", spy):
            self.svc.run_next()
        self.assertEqual(seen, [("photos", "preview", "scanning the source", True, "ok")])
        self.assertEqual(self.svc.progress, "")

    def test_a_real_run_makes_the_old_preview_go_away(self):
        self.synced()
        self.run_preview()
        self.assertIsNotNone(self.view()["preview"])
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertIsNone(self.view()["preview"])

    def test_a_paused_folder_can_still_be_previewed(self):
        self.synced()
        self.svc.update_job("photos", {"enabled": False})
        preview = self.run_preview()
        self.assertFalse(preview["would_refuse"])

    def test_removing_the_folder_drops_a_waiting_preview(self):
        self.synced()
        self.svc.preview("photos")
        self.svc.remove_job("photos")
        self.assertEqual(self.svc.preview_queue, [])
        self.assertFalse(self.svc.run_next())

    def test_an_unknown_folder_is_a_sentence(self):
        with self.assertRaises(ConfigError):
            self.svc.preview("nope")

    def test_a_crash_in_the_planner_is_a_refusal_never_a_success(self):
        self.synced()
        with mock.patch.object(engine, "scan_source", side_effect=RuntimeError("boom")):
            preview = self.run_preview()
        self.assertTrue(preview["would_refuse"])
        self.assertIn("unexpected error", preview["message"])


class PreviewApi(unittest.TestCase):
    def setUp(self):
        import tempfile
        import threading

        from keepsake.server import serve
        from tests.helpers import make_cfg
        from tests.test_api import Client
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        write(self.tmp / "sources/Photos/a.jpg", "a")
        cfg = replace(make_cfg(self.tmp), port=0, host="127.0.0.1")
        self.svc = Service(cfg, [])
        self.httpd = serve(self.svc)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.c = Client(self.httpd.server_address[1])
        self.c.open()
        self.c.call("POST", "/api/jobs", {"name": "photos", "root": "sources", "source": "Photos"})
        self.svc.run_next()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def test_post_queues_get_returns_the_result_and_it_is_labelled(self):
        write(self.tmp / "sources/Photos/b.jpg", "b")
        status, data, _ = self.c.call("POST", "/api/jobs/photos/preview", {})
        self.assertEqual((status, data["ok"]), (200, True))
        status, data, _ = self.c.call("GET", "/api/jobs/photos/preview")
        self.assertEqual((status, data["state"], data["preview"]), (200, "queued", None))
        self.svc.run_next()
        status, data, _ = self.c.call("GET", "/api/jobs/photos/preview")
        self.assertEqual(data["state"], "idle")
        self.assertEqual((data["preview"]["label"], data["preview"]["added"]), ("Preview", 1))
        status, job, _ = self.c.call("GET", "/api/jobs/photos")
        self.assertEqual(job["preview"]["added"], 1)

    def test_it_needs_the_csrf_token_like_every_write(self):
        status, data, _ = self.c.call("POST", "/api/jobs/photos/preview", {}, csrf=False)
        self.assertEqual(status, 403)

    def test_a_busy_folder_and_an_unknown_one_are_400_sentences(self):
        self.c.call("POST", "/api/jobs/photos/preview", {})
        status, data, _ = self.c.call("POST", "/api/jobs/photos/preview", {})
        self.assertEqual((status, data["ok"]), (400, False))
        self.assertIn("already running or waiting", data["message"])
        status, data, _ = self.c.call("POST", "/api/jobs/nope/preview", {})
        self.assertEqual((status, data["ok"]), (400, False))


if __name__ == "__main__":
    unittest.main()
