"""Progress and stop: one model for every long operation.

A person must be able to tell working from stuck, so every long operation (a
preview, a copy, a check, a restore) reports the same fields from the engine:
phase, files and bytes so far, the folder it is in, how long it has run and when
anything last happened. The service reads them while the operation runs; the page
shows them and offers to stop. These tests pin the model, that each operation
feeds it, that a stop leaves nothing half-written, and that a worker that hits a
bug still ends the preview in a sentence instead of leaving the page waiting.
"""

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from keepsake import engine
from keepsake.config import ConfigError
from keepsake.engine import TMP_RE, preview_job, restore_batch, run_job, verify_copy
from keepsake.progress import (
    CANCEL_OFFER_AFTER_S,
    STALL_AFTER_S,
    Cancelled,
    Progress,
    current,
    short_path,
)
from keepsake.server import setup_allows
from tests.helpers import make_cfg, make_job, write
from tests.test_service import Base

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ProgressModelTests(unittest.TestCase):
    def test_counters_only_go_up_within_a_phase_and_start_again_in_the_next(self):
        p = Progress()
        p.phase_start("reading-source")
        seen = []
        for _ in range(5):
            p.add("a/b", 1, 100)
            seen.append((p.snapshot()["files"], p.snapshot()["bytes"]))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], (5, 500))
        p.phase_start("copying", total_files=10, total_bytes=1000)
        self.assertEqual((p.snapshot()["files"], p.snapshot()["bytes"]), (0, 0))

    def test_last_activity_moves_with_every_step_and_idle_time_follows_the_clock(self):
        clock = FakeClock()
        p = Progress(clock)
        p.phase_start("reading-source")
        clock.t += 20
        self.assertEqual(p.snapshot()["idle_s"], 20.0)
        p.add("x", 1)
        self.assertEqual(p.snapshot()["idle_s"], 0.0)
        self.assertEqual(p.snapshot()["elapsed_s"], 20.0)

    def test_it_says_stalled_at_30_seconds_and_offers_to_stop_after_5_minutes(self):
        clock = FakeClock()
        p = Progress(clock)
        p.phase_start("reading-icloud")
        clock.t += STALL_AFTER_S - 1
        self.assertFalse(p.snapshot()["stalled"])
        clock.t += 1
        snap = p.snapshot()
        self.assertTrue(snap["stalled"])
        self.assertFalse(snap["can_cancel"])
        clock.t += CANCEL_OFFER_AFTER_S - STALL_AFTER_S
        self.assertTrue(p.snapshot()["can_cancel"])

    def test_the_bar_is_determinate_only_when_a_total_is_known(self):
        p = Progress()
        p.phase_start("reading-source")
        p.add("x", 3, 30)
        self.assertIsNone(p.snapshot()["percent"])
        p.phase_start("copying", total_files=4, total_bytes=200)
        p.add("", 0, 50)
        self.assertEqual(p.snapshot()["percent"], 25.0)
        p.add("", 0, 500)                                   # never past 100
        self.assertEqual(p.snapshot()["percent"], 100.0)

    def test_a_hint_from_a_finished_preview_gives_the_next_read_a_bar_but_never_100(self):
        p = Progress(hint_files=100)
        p.phase_start("reading-source")
        p.add("x", 40)
        snap = p.snapshot()
        self.assertEqual((snap["percent"], snap["estimated"]), (40.0, True))
        p.add("x", 400)                                      # more files than the guess
        self.assertEqual(p.snapshot()["percent"], 99.0)

    def test_a_stop_is_raised_at_the_next_step_and_not_before(self):
        p = Progress()
        p.phase_start("copying", total_files=2)
        p.add("one", 1)
        p.cancel()
        self.assertTrue(p.snapshot()["cancelling"])
        with self.assertRaises(Cancelled):
            p.add("two", 1)

    def test_the_folder_shown_is_the_end_of_the_path_and_short(self):
        self.assertEqual(short_path("a/b/c"), "a/b/c")
        long = "/".join(["folder%02d" % i for i in range(30)]) + "/photo.jpg"
        shown = short_path(long)
        self.assertLessEqual(len(shown), 60)
        self.assertTrue(shown.startswith("…/") and shown.endswith("photo.jpg"))


class EngineFeedsItTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.job = make_job()
        self.src = self.tmp / "sources" / "Photos"
        self.dest = self.tmp / "icloud" / "photos"
        for i in range(6):
            write(self.src / f"2025/img{i}.jpg", "x" * (100 + i), 1_700_000_000)

    def tearDown(self):
        self._tmp.cleanup()

    def phases_of(self, track):
        """Every phase the tracker went through, in order."""
        seen = []
        real = track.phase_start

        def spy(phase, *a, **k):
            seen.append((phase, k.get("total_files"), k.get("total_bytes")))
            return real(phase, *a, **k)

        track.phase_start = spy
        return seen

    def test_a_copy_goes_through_its_phases_and_ends_with_the_counts_it_made(self):
        track = Progress()
        seen = self.phases_of(track)
        report = run_job(self.cfg, self.job, now=NOW, track=track)
        self.assertEqual(report.result, "ok")
        names = [s[0] for s in seen]
        self.assertEqual(names[:2], ["reading-source", "reading-icloud"])
        for phase in ("comparing", "measuring", "copying", "verifying", "checking"):
            self.assertIn(phase, names)
        copying = next(s for s in seen if s[0] == "copying")
        self.assertEqual(copying[1], 6)                      # the files it had to copy
        self.assertEqual(copying[2], sum(100 + i for i in range(6)))

    def test_a_preview_reports_while_it_reads_and_writes_nothing(self):
        track = Progress()
        seen = self.phases_of(track)
        report = preview_job(self.cfg, self.job, track=track)
        self.assertFalse(report.refused)
        self.assertEqual([s[0] for s in seen][:2], ["reading-source", "reading-icloud"])
        self.assertFalse(self.dest.exists())

    def test_a_stop_between_files_leaves_whole_files_only_and_the_next_run_finishes(self):
        class StopAfterOne(Progress):
            def add(self, path="", files=0, nbytes=0):
                if self.phase == "copying" and self.files >= 1:
                    self.cancel()
                super().add(path, files, nbytes)

        before = {p: hashlib.sha1(p.read_bytes()).hexdigest()
                  for p in self.src.rglob("*") if p.is_file()}
        report = run_job(self.cfg, self.job, now=NOW, track=StopAfterOne())
        self.assertTrue(report.cancelled)
        self.assertEqual(report.result, "skipped")
        self.assertIn("stopped at your request", report.error)
        left = [p for p in self.dest.rglob("*") if p.is_file()]
        self.assertTrue(left)                                # some were copied
        self.assertLess(len([p for p in left if p.name.startswith("img")]), 6)
        self.assertFalse([p for p in left if TMP_RE.match(p.name)])   # no temp file left
        for p in left:
            if p.name.startswith("img"):
                self.assertEqual(p.read_bytes(), (self.src / "2025" / p.name).read_bytes())
        self.assertEqual({p: hashlib.sha1(p.read_bytes()).hexdigest()
                          for p in self.src.rglob("*") if p.is_file()}, before)
        again = run_job(self.cfg, self.job, now=NOW)
        self.assertEqual(again.result, "ok")
        self.assertEqual(len(list(self.dest.rglob("img*.jpg"))), 6)

    def test_a_stop_in_the_middle_of_a_big_file_removes_its_temp_file(self):
        write(self.src / "big.bin", "y" * (engine.CHUNK * 3 + 17), 1_700_000_000)

        class StopOnFirstBytes(Progress):
            def add(self, path="", files=0, nbytes=0):
                if self.phase == "copying" and nbytes:
                    self.cancel()
                super().add(path, files, nbytes)

        report = run_job(self.cfg, self.job, now=NOW, track=StopOnFirstBytes())
        self.assertTrue(report.cancelled)
        self.assertFalse([p for p in self.dest.rglob("*") if TMP_RE.match(p.name)])
        self.assertFalse((self.dest / "big.bin").exists())    # never a part of it under its name

    def test_a_stopped_preview_says_so_and_changes_nothing(self):
        track = Progress()
        track.cancel()
        report = preview_job(self.cfg, self.job, track=track)
        self.assertTrue(report.refused and report.cancelled)
        self.assertEqual(report.guard, "cancelled")
        self.assertIn("changed nothing", report.error)
        self.assertFalse(self.dest.exists())

    def test_a_check_reports_the_files_it_compares_and_can_be_stopped(self):
        run_job(self.cfg, self.job, now=NOW)
        track = Progress(op="check")
        seen = self.phases_of(track)
        result = verify_copy(self.cfg, self.job, NOW, None, trk=track)
        self.assertEqual(result["state"], "match")
        self.assertEqual(seen[0][0], "checking")
        self.assertEqual(seen[0][1], 6)                      # up to 40, here all 6
        self.assertGreaterEqual(track.snapshot()["files"], 6)
        stopped = Progress(op="check")
        stopped.cancel()
        with self.assertRaises(Cancelled):
            verify_copy(self.cfg, self.job, NOW, None, trk=stopped)

    def test_a_restore_reports_files_and_bytes_copied(self):
        # Retire one file so there is something kept to restore.
        run_job(self.cfg, self.job, now=NOW)
        (self.src / "2025/img0.jpg").unlink()
        run_job(self.cfg, self.job, now=NOW)
        track = Progress(op="restore")
        seen = self.phases_of(track)
        out = restore_batch(self.cfg, self.job, None, None, trk=track)
        self.assertEqual(out["restored"], 1)
        copying = next(s for s in seen if s[0] == "copying")
        self.assertEqual(copying[1], 1)
        snap = track.snapshot()
        self.assertEqual((snap["op"], snap["label"]), ("restore", "Restoring files"))
        self.assertEqual((snap["files"], snap["bytes"]), (1, out["bytes"]))

    def test_a_restore_that_is_stopped_leaves_no_temp_file(self):
        run_job(self.cfg, self.job, now=NOW)
        (self.src / "2025/img0.jpg").unlink()
        run_job(self.cfg, self.job, now=NOW)

        class StopOnBytes(Progress):
            def add(self, path="", files=0, nbytes=0):
                if self.phase == "copying" and nbytes:
                    self.cancel()
                super().add(path, files, nbytes)

        with self.assertRaises(Cancelled):
            restore_batch(self.cfg, self.job, None, None, trk=StopOnBytes(op="restore"))
        self.assertFalse([p for p in self.dest.rglob("*") if TMP_RE.match(p.name)])


class ServiceShowsItTests(Base):
    def test_a_running_copy_shows_its_progress_and_operation_from_server_state(self):
        self.add()
        seen = []
        real = engine.run_job

        def spy(cfg, job, **kw):
            seen.append((self.view()["operation"], self.view()["progress_detail"]))
            return real(cfg, job, **kw)

        self.svc.sync_now("photos")
        with mock.patch("keepsake.service.run_job", spy):
            self.svc.run_next()
        op, detail = seen[0]
        self.assertEqual(op, "sync")
        self.assertEqual(detail["op"], "sync")
        self.assertIn("phase", detail)
        self.assertIsNone(self.view()["operation"])           # and nothing once it has finished
        self.assertIsNone(self.view()["progress_detail"])

    def test_a_running_preview_shows_in_the_preview_answer_too(self):
        self.add()
        seen = []
        real = engine.preview_job

        def spy(cfg, job, **kw):
            seen.append(self.svc.preview_view("photos"))
            return real(cfg, job, **kw)

        self.svc.preview("photos")
        with mock.patch("keepsake.service.preview_job", spy):
            self.svc.run_next()
        self.assertEqual(seen[0]["state"], "running")
        self.assertEqual(seen[0]["progress_detail"]["op"], "preview")

    def test_a_next_read_of_the_source_is_measured_against_the_last_previews_count(self):
        self.add()
        self.svc.preview("photos")
        self.svc.run_next()
        hints = []
        real = engine.run_job

        def spy(cfg, job, **kw):
            hints.append(self.svc.track.hint_files)
            return real(cfg, job, **kw)

        self.svc.sync_now("photos")
        with mock.patch("keepsake.service.run_job", spy):
            self.svc.run_next()
        self.assertEqual(hints, [5])

    def test_a_check_is_visible_while_it_runs_and_a_stop_ends_it_in_a_sentence(self):
        self.add()
        self.svc.sync_now("photos")
        self.svc.run_next()
        seen = []

        def slow(cfg, job, now, cursor, **kw):
            seen.append((self.view()["operation"], self.view()["progress_detail"]["op"]))
            self.svc.cancel("photos")
            current().check()       # the tracker the service made is the current one: it honours the stop
            raise AssertionError("not reached")

        with mock.patch("keepsake.service.verify_copy", slow):
            with self.assertRaises(ConfigError) as ctx:
                self.svc.verify_job("photos")
        self.assertEqual(seen, [("check", "check")])
        self.assertIn("stopped at your request", str(ctx.exception))
        self.assertIsNone(self.view()["operation"])

    def test_a_restore_is_visible_while_it_runs(self):
        self.add()
        self.svc.sync_now("photos")
        self.svc.run_next()
        seen = []

        def spy(cfg, job, day, expect, **kw):
            seen.append(self.view()["operation"])
            return {"restored": 0, "bytes": 0, "skipped": [], "dates": [], "message": "x"}

        with mock.patch("keepsake.service.restore_batch", spy):
            self.svc.restore_many("photos", everything=True)
        self.assertEqual(seen, ["restore"])
        self.assertIsNone(self.view()["operation"])

    def test_cancel_stops_a_running_job_and_takes_a_waiting_one_off_the_list(self):
        self.add()
        self.svc.sync_now("photos")
        self.assertIn("taken off the waiting list", self.svc.cancel("photos"))
        self.assertEqual(self.svc.queue, [])
        with self.assertRaises(ConfigError):
            self.svc.cancel("photos")                         # nothing to stop

    def test_a_stopped_copy_is_not_an_alarm(self):
        self.add()
        self.svc.sync_now("photos")
        real = engine.run_job

        def stop_first(cfg, job, **kw):
            self.svc.cancel("photos")
            return real(cfg, job, **kw)

        with mock.patch("keepsake.service.run_job", stop_first):
            self.svc.run_next()
        self.assertEqual(self.rec.events, [])                 # nobody is told it failed
        self.assertTrue(self.svc.state["photos"]["cancelled"])
        self.assertTrue(any("stopped by you" in e["message"] for e in self.svc.activity(10)))


class AWorkerThatHitsABugStillEndsTheWaitTests(Base):
    """The page showed 'Reading' with the server idle: whatever goes wrong on the
    worker, the job is freed and a preview ends in a stored sentence."""

    def test_a_preview_that_blows_up_outside_the_engine_stores_its_error(self):
        self.add()
        self.svc.preview("photos")
        with mock.patch("keepsake.service.preview_job", side_effect=RuntimeError("boom")):
            self.svc.run_next()
        self.assertIsNone(self.svc.running)
        view = self.svc.preview_view("photos")
        self.assertEqual(view["state"], "idle")
        self.assertTrue(view["preview"]["would_refuse"])
        self.assertIn("could not finish", view["preview"]["message"])
        self.assertIn("boom", view["preview"]["message"])

    def test_a_preview_result_that_cannot_be_stored_still_leaves_a_sentence(self):
        self.add()
        self.svc.preview("photos")
        with mock.patch.object(self.svc, "_preview_result", side_effect=KeyError("x")):
            self.svc.run_next()
        self.assertIsNone(self.svc.running)
        self.assertIn("could not finish", self.svc.preview_view("photos")["preview"]["message"])

    def test_a_copy_that_blows_up_frees_the_job_and_the_queue(self):
        self.add()
        self.svc.sync_now("photos")
        with mock.patch("keepsake.service.run_job", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.svc.run_next()
        self.assertIsNone(self.svc.running)
        self.assertEqual(self.svc.queue, [])
        self.assertIsNone(self.view()["progress_detail"])

    def test_an_idle_server_with_no_result_says_idle_so_the_page_cannot_say_reading(self):
        self.add()
        view = self.svc.preview_view("photos")
        self.assertEqual(view["state"], "idle")
        self.assertIsNone(view["preview"])
        self.assertFalse(self.view()["previewing"])


class StopIsAllowedWhileSettingUpTests(unittest.TestCase):
    def test_the_wizard_may_stop_a_run_that_has_gone_quiet(self):
        svc = mock.Mock()
        self.assertTrue(setup_allows("POST", ["jobs", "photos", "cancel"], svc))
        self.assertFalse(setup_allows("GET", ["jobs", "photos", "cancel"], svc))


if __name__ == "__main__":
    unittest.main()
