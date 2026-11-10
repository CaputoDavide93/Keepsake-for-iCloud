import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keepsake.config import ConfigError, Job, JobStore
from keepsake.service import HISTORY_DAYS, Service, clean_history, merge_history, next_due, zone
from tests.helpers import make_cfg, make_job, write

T0 = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class Recorder:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.rec = Recorder()
        self.clock = Clock()
        self.svc = Service(self.cfg, [self.rec], self.clock)
        for i in range(5):
            write(self.tmp / f"sources/Photos/2025/{i}.jpg", f"d{i}", 1_700_000_000)
        write(self.tmp / "sources/Documents/Taxes/a.pdf", "t")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, **kw):
        base = {"name": "photos", "root": "sources", "source": "Photos"}
        base.update(kw)
        return self.svc.add_job(base)

    def view(self):
        return self.svc.status()["jobs"][0]


class ActionTests(Base):
    def test_add_persists_runs_and_reports(self):
        self.add()
        self.assertEqual([j.name for j in JobStore(self.cfg).load()], ["photos"])
        self.assertTrue(self.svc.run_next())
        job = self.view()
        self.assertEqual((job["result"], job["files_copied"]), ("ok", 5))
        self.assertGreater(job["icloud_bytes"], 0)
        self.assertTrue((self.tmp / "icloud/photos/2025/0.jpg").exists())

    def test_a_refusal_raises_with_the_reason_and_changes_nothing(self):
        with self.assertRaises(ConfigError) as ctx:
            self.add(source="../../etc")
        self.assertIn("..", str(ctx.exception))
        self.assertEqual(JobStore(self.cfg).load(), [])

    def test_refused_run_keeps_the_last_measured_size(self):
        self.add()
        self.svc.run_next()
        size = self.view()["icloud_bytes"]
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            p.unlink()
        self.svc.sync_now("photos")
        self.svc.run_next()
        job = self.view()
        self.assertEqual((job["result"], job["icloud_bytes"]), ("failed", size))

    def test_second_sync_now_while_queued_is_refused(self):
        self.add()
        self.svc.queue.clear()
        self.svc.sync_now("photos")
        with self.assertRaises(ConfigError) as ctx:
            self.svc.sync_now("photos")
        self.assertIn("already", str(ctx.exception))
        self.assertEqual(len(self.svc.queue), 1)

    def test_accept_shrink_must_be_the_boolean_true(self):
        self.add()
        self.svc.queue.clear()
        self.svc.sync_now("photos", "false")
        self.assertEqual(self.svc.queue, [("photos", False)])
        self.svc.queue.clear()
        self.svc.sync_now("photos", True)
        self.assertEqual(self.svc.queue, [("photos", True)])

    def test_unknown_and_paused_jobs(self):
        with self.assertRaises(ConfigError):
            self.svc.sync_now("ghost")
        self.add()
        self.svc.update_job("photos", {"enabled": False})
        with self.assertRaises(ConfigError):
            self.svc.sync_now("photos")

    def test_update_validates_persists_and_pins_identity(self):
        self.add()
        self.svc.update_job("photos", {"schedule": {"at": "04:15"}, "keep_days": 7})
        job = JobStore(self.cfg).load()[0]
        self.assertEqual((job.schedule, job.keep_days), ({"at": "04:15"}, 7))
        for bad in ({"mode": "zap"}, {"dest": "elsewhere"}, {"source": "Documents"},
                    {"root": "nope"}):
            with self.assertRaises(ConfigError):
                self.svc.update_job("photos", bad)

    def test_remove_keeps_icloud_copy_unless_asked(self):
        self.add()
        self.svc.run_next()
        self.assertIn("NOT deleted", self.svc.remove_job("photos"))
        self.assertTrue((self.tmp / "icloud/photos/2025/0.jpg").exists())
        self.assertEqual(JobStore(self.cfg).load(), [])

    def test_remove_with_delete_copy_is_a_soft_delete_and_string_true_is_ignored(self):
        self.add()
        self.svc.run_next()
        self.svc.remove_job("photos", delete_copy="true")
        self.assertTrue((self.tmp / "icloud/photos").exists())
        self.add()
        self.svc.remove_job("photos", delete_copy=True)
        self.assertFalse((self.tmp / "icloud/photos").exists())
        self.assertTrue(list((self.tmp / "icloud/.removed").glob("photos_*")))

    def test_activity_log_records_and_orders_newest_first(self):
        self.add()
        self.svc.run_next()
        entries = self.svc.activity()
        self.assertGreaterEqual(len(entries), 2)
        self.assertIn("ok in", entries[0]["message"])
        self.assertIn("added", entries[-1]["message"])


class AnchorTests(Base):
    def test_unconfirmed_icloud_folder_runs_nothing_until_a_person_confirms(self):
        (self.tmp / "icloud/.keepsake-anchor").unlink()
        self.add()
        st = self.svc.status()
        self.assertFalse(st["anchor_ok"])
        self.assertIn("not been confirmed", st["anchor_note"])
        self.svc.run_next()
        self.assertEqual(self.view()["result"], "skipped")
        self.assertEqual(list((self.tmp / "icloud").iterdir()), [])
        self.assertIn("confirmed", self.svc.confirm_icloud())
        self.assertTrue(self.svc.status()["anchor_ok"])
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual(self.view()["result"], "ok")

    def test_confirming_twice_is_harmless(self):
        self.assertIn("already", self.svc.confirm_icloud())


class BrowseTests(Base):
    def test_lists_subfolders_only_inside_a_root(self):
        write(self.tmp / "sources/Keys/.ssh/id", "k")
        top = self.svc.browse("sources")
        names = {f["name"]: f for f in top["folders"]}
        self.assertEqual(set(names), {"Photos", "Documents", "Keys"})
        self.assertIsNone(top["parent"])
        sub = self.svc.browse("sources", "Photos")
        self.assertEqual([f["path"] for f in sub["folders"]], ["Photos/2025"])
        self.assertEqual(sub["parent"], "")

    def test_flags_secret_looking_names(self):
        write(self.tmp / "sources/my-passwords/x", "k")
        flags = {f["name"]: f["warn"] for f in self.svc.browse("sources")["folders"]}
        self.assertTrue(flags["my-passwords"])
        self.assertFalse(flags["Photos"])

    def test_traversal_unknown_root_and_symlinks_are_refused_or_hidden(self):
        for root, path in (("sources", "../"), ("sources", "../../etc"),
                           ("sources", "/etc"), ("nope", ""), ("sources", "Missing")):
            with self.assertRaises(ConfigError, msg=f"{root}:{path}"):
                self.svc.browse(root, path)
        (self.tmp / "sources/Link").symlink_to(self.tmp)
        names = [f["name"] for f in self.svc.browse("sources")["folders"]]
        self.assertNotIn("Link", names)
        with self.assertRaises(ConfigError):
            self.svc.browse("sources", "Link")

    def test_unavailable_root_is_refused_in_words(self):
        cfg = make_cfg(self.tmp, roots={"sources": self.tmp / "gone"})
        with self.assertRaises(ConfigError) as ctx:
            Service(cfg, [], self.clock).browse("sources")
        self.assertIn("unavailable", str(ctx.exception))


class ScheduleTests(Base):
    def test_every_n_hours(self):
        job = make_job(schedule={"every_hours": 6})
        self.assertEqual(next_due(job, T0, True, T0, zone("UTC")), T0 + timedelta(hours=6))

    def test_failure_retries_within_the_hour(self):
        job = make_job(schedule={"every_hours": 24})
        self.assertEqual(next_due(job, T0, False, T0, zone("UTC")), T0 + timedelta(hours=1))

    def test_at_time_uses_wall_clock_zone_across_dst(self):
        london = zone("Europe/London")
        job = make_job(schedule={"at": "03:30"})
        last = datetime(2026, 10, 25, 4, 0, tzinfo=london)
        due = next_due(job, last, True, last, london)
        self.assertEqual((due.hour, due.minute, due.day), (3, 30, 26))

    def test_never_run_is_due_now_and_runs_once(self):
        JobStore(self.cfg).save([Job(name="photos", root="sources", source="Photos",
                                     dest="photos")])
        self.assertEqual(self.svc.due_jobs(), ["photos"])
        self.svc.run_next()
        self.assertEqual(self.svc.due_jobs(), [])
        self.clock.now = T0 + timedelta(hours=25)
        self.assertEqual(self.svc.due_jobs(), ["photos"])


class AttentionAndNotifyTests(Base):
    def setUp(self):
        super().setUp()
        JobStore(self.cfg).save([Job(name="photos", root="sources", source="Photos",
                                     dest="photos")])

    def test_green_run_is_silent(self):
        self.svc.run_next()
        self.assertEqual(self.svc.status()["attention_count"], 0)
        self.assertEqual(self.rec.events, [])

    def test_failure_notifies_once_then_recovery_once(self):
        self.svc.run_next()
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            p.unlink()
        self.clock.now = T0 + timedelta(hours=25)
        self.svc.run_next()
        self.clock.now = T0 + timedelta(hours=26)
        self.svc.run_next()  # same problem again: not a second message
        self.assertEqual([e["event"] for e in self.rec.events], ["failed"])
        self.assertIn("empty", self.rec.events[0]["message"])
        write(self.tmp / "sources/Photos/2025/new.jpg", "n")
        self.clock.now = T0 + timedelta(hours=28)
        self.svc.sync_now("photos", True)
        self.svc.run_next()
        self.assertEqual([e["event"] for e in self.rec.events], ["failed", "recovered"])

    def test_a_day_of_failures_is_an_alarm_in_plain_words(self):
        self.svc.run_next()
        self.svc.cfg.roots["sources"].rename(self.tmp / "sources-away")
        self.clock.now = T0 + timedelta(hours=49)
        self.svc.run_next()
        s = self.svc.status()
        self.assertEqual(s["jobs"][0]["result"], "skipped")
        self.assertEqual(s["attention_count"], 1)
        self.assertIn("not mounted", s["attention"][0]["reason"])

    def test_silent_service_is_noticed(self):
        self.svc.run_next()
        self.clock.now = T0 + timedelta(hours=60)
        self.assertEqual(self.svc.status()["attention"][0]["reason"], "has not run on time")

    def test_a_broken_notifier_never_stops_the_run(self):
        class Boom:
            def send(self, _e):
                raise RuntimeError("down")
        svc = Service(self.cfg, [Boom()], self.clock)
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            p.unlink()
        svc.run_next()
        self.assertEqual(svc.status()["jobs"][0]["result"], "failed")


class TrustTests(Base):
    def ready(self):
        self.add()
        self.svc.run_next()
        return self.tmp / "icloud/photos"

    def test_check_stores_a_result_and_a_difference_needs_attention(self):
        dest = self.ready()
        out = self.svc.verify_job("photos")
        self.assertEqual((out["state"], out["checked"]), ("match", 5))
        job = self.view()
        self.assertEqual(job["verify"]["message"], "Checked 5 files, all match")
        self.assertEqual(job["status"], "ok")
        (dest / "2025/1.jpg").write_text("zz")          # same size as "d1", other bytes
        out = self.svc.verify_job("photos")
        self.assertEqual(out["state"], "mismatch")
        job = self.view()
        self.assertEqual(job["status"], "failed")        # "Needs attention" in the page
        self.assertIn("2025/1.jpg", job["error"])
        self.assertEqual(self.svc.status()["attention_count"], 1)
        self.assertIn("2025/1.jpg", self.svc.status()["attention"][0]["reason"])
        (dest / "2025/1.jpg").write_text("d1")
        self.svc.verify_job("photos")
        self.assertEqual(self.view()["status"], "ok")    # a passing check clears it

    def test_a_mismatch_is_notified_and_logged_as_a_problem(self):
        dest = self.ready()
        (dest / "2025/2.jpg").write_text("zz")
        self.svc.verify_job("photos")
        self.assertEqual(self.rec.events[-1]["event"], "failed")
        self.assertEqual(self.svc.activity(1)[0]["level"], "error")

    def test_a_refusal_raises_and_stores_nothing(self):
        self.add()
        with self.assertRaises(ConfigError) as ctx:
            self.svc.verify_job("photos")                # no run has finished
        self.assertIn("nothing to check yet", str(ctx.exception))
        self.assertIsNone(self.view()["verify"])
        with self.assertRaises(ConfigError):
            self.svc.verify_job("nope")

    def test_a_job_that_is_queued_or_running_cannot_be_checked(self):
        self.ready()
        self.svc.sync_now("photos")
        with self.assertRaises(ConfigError) as ctx:
            self.svc.verify_job("photos")
        self.assertIn("copying or waiting", str(ctx.exception))

    def test_a_run_that_lands_during_the_check_discards_the_result(self):
        self.ready()
        from unittest import mock
        real = __import__("keepsake.service", fromlist=["x"]).verify_copy

        def slow(cfg, job, now, cursor=None):
            out = real(cfg, job, now, cursor)
            self.svc.state["photos"]["last_run"] = "2026-11-07T04:00:00+00:00"
            return out
        with mock.patch("keepsake.service.verify_copy", slow):
            with self.assertRaises(ConfigError) as ctx:
                self.svc.verify_job("photos")
        self.assertIn("result was not kept", str(ctx.exception))
        self.assertIsNone(self.view()["verify"])

    def test_one_check_at_a_time(self):
        self.ready()
        self.svc.verify_lock.acquire()
        try:
            with self.assertRaises(ConfigError):
                self.svc.verify_job("photos")
        finally:
            self.svc.verify_lock.release()

    def test_what_changed_is_a_sentence_without_file_names(self):
        self.add()
        self.svc.run_next()
        job = self.view()
        self.assertEqual(job["changes"]["summary"], "5 added")
        self.assertEqual(self.svc.activity(1)[0]["message"].split(": ", 1)[1], "5 added")
        (self.tmp / "sources/Photos/2025/4.jpg").unlink()
        write(self.tmp / "sources/Photos/2025/0.jpg", "newer", 1_700_000_900)
        self.svc.sync_now("photos")
        self.svc.run_next()
        job = self.view()
        self.assertEqual(job["changes"]["summary"], "1 updated, 1 moved to .deleted")
        self.assertEqual({s["kind"] for s in job["changes"]["samples"]}, {"updated", "removed"})
        line = self.svc.activity(1)[0]["message"]
        self.assertNotIn("jpg", line)
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual(self.view()["changes"]["summary"], "nothing changed")

    def test_deleted_files_and_restore(self):
        dest = self.ready()
        (self.tmp / "sources/Photos/2025/4.jpg").unlink()
        self.svc.sync_now("photos")
        self.svc.run_next()
        listing = self.svc.deleted_files("photos")
        self.assertEqual([(e["date"], e["path"], e["days_left"]) for e in listing["entries"]],
                         [("2026-11-07", "2025/4.jpg", 30)])
        msg = self.svc.restore_file("photos", "2026-11-07", "2025/4.jpg")
        self.assertIn(".restored/2026-11-07/2025/4.jpg", msg)
        self.assertEqual((dest / ".restored/2026-11-07/2025/4.jpg").read_text(), "d4")
        with self.assertRaises(ConfigError):
            self.svc.restore_file("photos", "2026-11-07", "../../.keepsake-owner")
        self.assertEqual(self.svc.activity(1)[0]["level"], "error")
        self.svc.sync_now("photos")
        with self.assertRaises(ConfigError):
            self.svc.restore_file("photos", "2026-11-07", "2025/4.jpg")


class HistoryTests(Base):
    """The nights strip: one outcome per local day, honest about failures."""

    def run_once(self):
        self.svc.sync_now("photos")
        self.svc.run_next()

    def test_a_copy_then_a_quiet_run_then_a_failed_run(self):
        self.add()
        self.svc.run_next()
        self.assertEqual([(e["day"], e["outcome"]) for e in self.view()["history"]], [("2026-11-07", "copied")])
        self.clock.now += timedelta(days=1)
        self.run_once()
        self.assertEqual((self.view()["history"][-1]["day"], self.view()["history"][-1]["outcome"]), ("2026-11-08", "nothing"))
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            p.unlink()
        self.clock.now += timedelta(days=1)
        self.run_once()
        job = self.view()
        self.assertEqual(job["result"], "failed")
        self.assertEqual([e["outcome"] for e in job["history"]], ["copied", "nothing", "paused"])

    def test_a_failed_run_is_never_green(self):
        self.add()
        self.svc.run_next()
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            p.unlink()
        self.clock.now += timedelta(days=1)
        self.run_once()
        self.assertEqual(self.view()["history"][-1]["outcome"], "paused")
        # a failure later the same day also turns that day amber, even after a copy
        write(self.tmp / "sources/Photos/2025/new.jpg", "n")
        self.run_once()
        self.assertEqual(self.view()["history"][-1]["outcome"], "paused")

    def test_old_state_file_without_history_renders_empty(self):
        self.add()
        self.svc.run_next()
        state = json.loads(self.cfg.state_file.read_text())
        del state["photos"]["history"]
        self.cfg.state_file.write_text(json.dumps(state))
        svc = Service(self.cfg, [self.rec], self.clock)
        self.assertEqual(svc.status()["jobs"][0]["history"], [])
        svc.sync_now("photos")
        svc.run_next()
        self.assertEqual(len(svc.status()["jobs"][0]["history"]), 1)

    def test_junk_history_is_ignored(self):
        today = "2026-11-07"
        junk = [None, "x", {"day": "nope", "outcome": "copied"},
                {"day": today, "outcome": "green"}, {"day": today}]
        self.assertEqual(clean_history(junk, today), [])
        self.assertEqual(clean_history("not a list", today), [])
        self.assertEqual(clean_history(junk + [{"day": today, "outcome": "nothing"}], today),
                         [{"day": today, "outcome": "nothing"}])

    def test_day_rollover_adds_one_entry_per_local_day(self):
        self.add()
        self.svc.run_next()
        self.run_once()
        self.assertEqual(len(self.view()["history"]), 1)   # same day: still one entry
        self.assertEqual(self.view()["history"][0]["outcome"], "copied")  # a quiet run keeps the copy
        self.clock.now += timedelta(days=1)
        self.run_once()
        self.assertEqual([e["day"] for e in self.view()["history"]], ["2026-11-07", "2026-11-08"])

    def test_the_day_is_the_local_day_not_utc(self):
        cfg = make_cfg(self.tmp, tz="Pacific/Auckland")   # UTC+13 in November
        self.clock.now = T0 + timedelta(hours=12)         # 15:00 UTC is 04:00 next day there
        svc = Service(cfg, [], self.clock)
        svc.add_job({"name": "photos", "root": "sources", "source": "Photos"})
        svc.run_next()
        self.assertEqual(svc.status()["jobs"][0]["history"][0]["day"], "2026-11-08")
        self.assertEqual(svc.status()["today"], "2026-11-08")

    def test_bounded_to_fourteen_days(self):
        self.add()
        for _ in range(HISTORY_DAYS + 6):
            self.run_once() if self.view()["last_run"] else self.svc.run_next()
            self.clock.now += timedelta(days=1)
        history = self.svc.state["photos"]["history"]
        self.assertEqual(len(history), HISTORY_DAYS)
        self.assertEqual(history[0]["day"], "2026-11-13")
        # merge alone also trims, and entries older than the window drop out of the view
        out = merge_history([{"day": f"2026-10-{d:02d}", "outcome": "copied"} for d in range(1, 29)],
                            "2026-11-07", "nothing", "2026-11-07")
        self.assertEqual([e["day"] for e in out], ["2026-10-25", "2026-10-26", "2026-10-27",
                                                   "2026-10-28", "2026-11-07"])
        self.assertEqual(clean_history(history, "2026-12-30"), [])

    def test_history_survives_a_save_and_reload(self):
        self.add()
        self.svc.run_next()
        again = Service(self.cfg, [self.rec], self.clock)
        self.assertEqual(again.status()["jobs"][0]["history"], self.view()["history"])


class SettingsTests(Base):
    def test_update_persists_and_applies(self):
        self.svc.update_settings({"max_dest_gb": 7, "webhook_url": "https://h.example/secret-token"})
        self.assertEqual(self.svc.cfg.max_dest_gb, 7)
        stored = json.loads(self.cfg.settings_file.read_text())
        self.assertEqual(stored["max_dest_gb"], 7)
        again = self.cfg.with_settings()
        self.assertEqual(again.max_dest_gb, 7)
        view = self.svc.settings_view()
        self.assertTrue(view["webhook_set"])
        self.assertNotIn("secret-token", json.dumps(view))
        with self.assertRaises(ConfigError):
            self.svc.update_settings({"shrink_pct": 500})
        self.assertEqual(self.svc.cfg.shrink_pct, 20)


if __name__ == "__main__":
    unittest.main()
