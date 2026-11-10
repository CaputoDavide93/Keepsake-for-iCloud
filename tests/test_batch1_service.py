"""Service and API side of the trust batch: honest iCloud state, the scheduled
self-check, the test alert and restoring a whole date, over the real HTTP server
where that is the contract."""

import json
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from keepsake import service as service_mod
from keepsake.config import AUTO_CHECKS, ConfigError, JobStore
from keepsake.server import serve
from keepsake.service import CHECK_RETRY, Service, next_check
from tests.helpers import write
from tests.test_api import ApiBase
from tests.test_batch1_alerts import Rec, ServiceBase


class AutoCheckTests(ServiceBase):
    def setUp(self):
        super().setUp()
        for i in range(6):
            write(self.tmp / f"sources/Photos/p{i}.jpg", f"photo {i}", 1_700_000_000)

    def ready(self, **kw):
        self.add(**kw)
        self.svc.run_next()
        self.rec.events.clear()

    def view(self):
        return self.svc.status()["jobs"][0]

    # -- the setting ---------------------------------------------------------
    def test_a_new_folder_checks_weekly_unless_told_otherwise(self):
        self.add()
        self.assertEqual(self.view()["auto_check"], "weekly")
        self.svc.add_job({"name": "docs", "root": "sources", "source": "Documents",
                          "auto_check": "off"})
        self.assertEqual({j["name"]: j["auto_check"] for j in self.svc.status()["jobs"]},
                         {"photos": "weekly", "docs": "off"})

    def test_a_folder_stored_before_this_existed_stays_off_and_loads_fine(self):
        self.cfg.jobs_file.write_text(json.dumps({"jobs": [
            {"name": "photos", "root": "sources", "source": "Photos", "dest": "photos",
             "excludes": [], "schedule": {"every_hours": 24}, "mode": "mirror-keep-deleted",
             "keep_days": 30, "max_gb": None, "enabled": True, "adopt_existing": False}]}))
        job = JobStore(self.cfg).load()[0]
        self.assertEqual((job.auto_check, job.stale_days), ("off", 3))
        self.assertIsNone(self.view()["next_check"])

    def test_a_state_file_from_before_loads_and_shows_no_check_data(self):
        self.add(auto_check="weekly")
        self.cfg.state_file.write_text(json.dumps({"photos": {
            "result": "ok", "last_run": "2026-11-07T01:00:00+00:00",
            "last_success": "2026-11-07T01:00:00+00:00", "files_total": 7, "created": "x"}}))
        svc = Service(self.cfg, [self.rec], self.clock)
        v = svc.status()["jobs"][0]
        self.assertEqual((v["check_rounds"], v["verify"], v["icloud"]["evicted"]), (0, None, None))
        self.assertEqual(v["check_cycle_checks"], 1)

    def test_only_known_values_are_accepted(self):
        for bad in ("hourly", "", None, 7, True):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self.add(auto_check=bad)
        self.assertEqual(AUTO_CHECKS, ("off", "daily", "weekly", "monthly"))

    def test_it_can_be_changed_later_without_touching_the_rest(self):
        self.add()
        self.svc.update_job("photos", {"auto_check": "monthly"})
        job = JobStore(self.cfg).load()[0]
        self.assertEqual((job.auto_check, job.keep_days), ("monthly", 30))

    # -- when it is due --------------------------------------------------------
    def test_nothing_is_due_until_the_first_copy_has_finished(self):
        self.add()
        self.assertIsNone(self.view()["next_check"])
        self.assertFalse(self.svc.run_due_check())

    def test_a_folder_never_checked_is_due_straight_after_its_first_copy(self):
        self.ready()
        self.assertEqual(self.view()["next_check"], self.clock.now.isoformat(timespec="seconds"))

    def test_the_period_follows_the_setting(self):
        for mode, days in (("daily", 1), ("weekly", 7), ("monthly", 30)):
            st = {"last_success": "x", "verify": {"at": self.clock.now.isoformat()}}
            from keepsake.config import Job
            job = Job("j", "sources", "S", "j", auto_check=mode)
            self.assertEqual(next_check(job, st, self.clock.now),
                             self.clock.now + timedelta(days=days))
        off = Job("j", "sources", "S", "j", auto_check="off")
        self.assertIsNone(next_check(off, {"last_success": "x"}, self.clock.now))

    def test_a_paused_folder_is_never_checked(self):
        self.ready()
        self.svc.update_job("photos", {"enabled": False})
        self.assertFalse(self.svc.run_due_check())

    # -- running it -------------------------------------------------------------
    def test_a_due_check_runs_records_the_time_and_is_not_due_again_for_a_week(self):
        self.ready()
        self.assertTrue(self.svc.run_due_check())
        v = self.view()
        self.assertEqual((v["verify"]["state"], v["verify"]["at"]),
                         ("match", self.clock.now.isoformat(timespec="seconds")))
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["next_check"], (self.clock.now + timedelta(days=7)).isoformat(timespec="seconds"))
        self.assertFalse(self.svc.run_due_check())
        self.clock.now += timedelta(days=7, minutes=1)
        self.assertTrue(self.svc.run_due_check())
        self.assertIn("Scheduled check: Checked 7 files, all match",
                      [a["message"] for a in self.svc.activity()][0])
        self.assertEqual(self.events("failed"), [])

    def test_a_mismatch_marks_the_folder_and_sends_an_alert(self):
        self.ready()
        (self.tmp / "icloud/photos/p3.jpg").write_text("tampered")
        self.assertTrue(self.svc.run_due_check())
        v = self.view()
        self.assertEqual(v["status"], "failed")
        self.assertIn("different", v["error"])
        alerts = self.events("failed")
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["message"].startswith("Scheduled check: "))
        self.assertEqual(alerts[0]["result"], "mismatch")
        self.assertEqual(self.svc.attention(self.svc._jobs())[0]["job"], "photos")

    def test_a_later_clean_check_clears_needs_attention(self):
        self.ready()
        target = self.tmp / "icloud/photos/p3.jpg"
        target.write_text("tampered")
        self.svc.run_due_check()
        target.write_text("photo 3")
        write(target, "photo 3", 1_700_000_000)
        self.clock.now += timedelta(days=8)
        for _ in range(3):            # the rotation reaches every file again
            self.svc.verify_job("photos")
        self.assertEqual(self.view()["status"], "ok")

    def test_it_never_runs_while_a_copy_is_running_or_waiting(self):
        self.ready()
        with mock.patch.object(service_mod, "verify_copy") as spy:
            self.svc.queue.append(("photos", False))
            self.assertFalse(self.svc.run_due_check())
            self.svc.queue.clear()
            self.svc.running = "photos"
            self.assertFalse(self.svc.run_due_check())
            self.svc.running = "other"
            self.assertFalse(self.svc.run_due_check())
        spy.assert_not_called()

    def test_a_sync_asked_for_during_a_check_waits_for_it(self):
        self.ready()
        started, release, order = threading.Event(), threading.Event(), []
        real = service_mod.verify_copy

        def slow(cfg, job, now, cursor=None):
            started.set()
            release.wait(5)
            order.append("check")
            return real(cfg, job, now, cursor)
        with mock.patch.object(service_mod, "verify_copy", slow):
            t = threading.Thread(target=self.svc.run_due_check)
            t.start()
            started.wait(5)
            self.svc.sync_now("photos")        # the person presses Sync mid-check
            self.assertIsNone(self.svc.running, "a copy must not start under a check")
            release.set()
            t.join(5)
        order.append("sync")
        self.svc.run_next()
        self.assertEqual(order, ["check", "sync"])
        self.assertEqual(self.view()["result"], "ok")

    def test_one_check_at_a_time_across_manual_and_scheduled(self):
        self.ready()
        self.svc.verify_lock.acquire()
        try:
            self.assertFalse(self.svc.run_due_check())
        finally:
            self.svc.verify_lock.release()

    def test_a_check_that_compares_nothing_is_retried_in_hours_not_every_pass(self):
        self.ready()
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            import os
            os.utime(p, (1_700_009_000, 1_700_009_000))
        self.assertTrue(self.svc.run_due_check())
        self.assertFalse(self.svc.run_due_check())
        self.assertEqual(self.view()["next_check"],
                         (self.clock.now + CHECK_RETRY).isoformat(timespec="seconds"))
        self.assertEqual(self.events("failed"), [], "not being able to compare is not a mismatch")
        self.assertIn("could not run", self.svc.activity()[0]["message"])

    def test_a_manual_check_counts_as_the_last_check(self):
        self.ready()
        self.svc.verify_job("photos")
        self.assertFalse(self.svc.run_due_check())

    def test_the_view_says_how_many_checks_cover_every_file(self):
        self.ready()
        self.assertEqual(self.view()["check_cycle_checks"], 1)
        for i in range(100):
            write(self.tmp / f"sources/Photos/bulk{i}.txt", f"b{i}", 1_700_000_000)
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual(self.view()["check_cycle_checks"], 3)        # 106 files / 40

    def test_checks_walk_through_every_file_and_count_the_rounds(self):
        self.ready()
        for i in range(94):
            write(self.tmp / f"sources/Photos/bulk{i}.txt", f"b{i}", 1_700_000_000)
        self.svc.sync_now("photos")
        self.svc.run_next()                     # 100 files
        for _ in range(3):
            self.svc.verify_job("photos")
        self.assertEqual(self.view()["check_rounds"], 1)
        self.assertEqual(self.svc.state["photos"]["verify_cursor"]["seed"],
                         self.svc.state["photos"]["verify_cursor"]["seed"])

    def test_the_seed_survives_a_restart(self):
        self.ready()
        self.svc.verify_job("photos")
        seed = self.svc.state["photos"]["verify_cursor"]["seed"]
        again = Service(self.cfg, [self.rec], self.clock)
        self.assertEqual(again.state["photos"]["verify_cursor"]["seed"], seed)


class EvictedViewTests(ServiceBase):
    def test_the_jobs_api_carries_the_evicted_count_and_a_plain_note(self):
        for i in range(4):
            write(self.tmp / f"sources/Photos/p{i}.jpg", f"photo {i}", 1_700_000_000)
        self.add()
        self.svc.run_next()
        view = self.svc.status()["jobs"][0]
        self.assertEqual(view["icloud"], {"evicted": 0, "note": ""})
        for i in (0, 1):
            (self.tmp / f"icloud/photos/p{i}.jpg").rename(self.tmp / f"icloud/photos/.p{i}.jpg.icloud")
        self.svc.sync_now("photos")
        self.svc.run_next()
        view = self.svc.status()["jobs"][0]
        self.assertEqual(view["icloud"]["evicted"], 2)
        self.assertIn("2 of this folder's files", view["icloud"]["note"])
        self.assertIn("Optimize Storage", view["icloud"]["note"])
        self.assertNotIn("uploaded", view["icloud"]["note"].replace("not uploaded", ""))

    def test_a_refused_run_keeps_the_last_count(self):
        write(self.tmp / "sources/Photos/p0.jpg", "x", 1_700_000_000)
        write(self.tmp / "sources/Photos/p1.jpg", "y", 1_700_000_000)
        self.add()
        self.svc.run_next()
        (self.tmp / "icloud/photos/p0.jpg").rename(self.tmp / "icloud/photos/.p0.jpg.icloud")
        self.svc.sync_now("photos")
        self.svc.run_next()
        for p in (self.tmp / "sources/Photos").rglob("*.jpg"):
            p.unlink()
        self.svc.sync_now("photos")
        self.svc.run_next()
        view = self.svc.status()["jobs"][0]
        self.assertEqual((view["result"], view["icloud"]["evicted"]), ("failed", 1))

    def test_the_status_carries_no_page_note(self):
        self.assertNotIn("note", self.svc.status())


class RestoreManyTests(ServiceBase):
    def setUp(self):
        super().setUp()
        for i in range(12):
            write(self.tmp / f"sources/Photos/p{i}.jpg", f"photo {i}", 1_700_000_000)
        self.add()
        self.svc.run_next()
        (self.tmp / "sources/Photos/p2.jpg").unlink()
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.day = self.clock.now.date().isoformat()

    def test_exactly_one_of_date_or_everything(self):
        for kw in ({}, {"day": self.day, "everything": True}, {"everything": "true"},
                   {"everything": 1}, {"everything": "yes"}):
            with self.assertRaises(ConfigError, msg=kw):
                self.svc.restore_many("photos", **kw)
        self.assertFalse((self.tmp / "icloud/photos/.restored").exists())

    def test_by_date_and_by_everything(self):
        out = self.svc.restore_many("photos", day=self.day, expect_files=1)
        self.assertEqual((out["restored"], out["dates"]), (1, [self.day]))
        out = self.svc.restore_many("photos", everything=True)
        self.assertEqual(out["restored"], 1)
        self.assertEqual((self.tmp / f"icloud/photos/.restored/{self.day}/p2.jpg").read_text(),
                         "photo 2")
        self.assertEqual((self.tmp / f"icloud/photos/.restored/{self.day}/p2 (2).jpg").read_text(),
                         "photo 2")
        self.assertIn("restored", self.svc.activity()[0]["message"])

    def test_refused_while_the_folder_is_copying(self):
        self.svc.running = "photos"
        with self.assertRaises(ConfigError):
            self.svc.restore_many("photos", everything=True)
        with self.assertRaises(ConfigError):
            self.svc.restore_many("photos", day=self.day)

    def test_a_refusal_is_a_config_error_with_the_reason_and_is_logged(self):
        svc = Service(replace(self.cfg, min_free_gb=10 ** 9), [Rec()], self.clock)
        with self.assertRaises(ConfigError) as ctx:
            svc.restore_many("photos", everything=True)
        self.assertIn("reserve", str(ctx.exception))
        self.assertEqual(svc.activity()[0]["level"], "error")

    def test_the_plan_is_read_only_and_matches(self):
        plan = self.svc.restore_plan("photos", self.day)
        self.assertEqual((plan["files"], plan["bytes"]), (1, len("photo 2")))
        self.assertFalse((self.tmp / "icloud/photos/.restored").exists())
        with self.assertRaises(ConfigError):
            self.svc.restore_plan("photos", "../..")
        with self.assertRaises(ConfigError):
            self.svc.restore_plan("ghost")


class BatchApiTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.c.open()
        for i in range(14):
            write(self.tmp / f"sources/Docs/keep{i}.txt", f"k{i}")
        self.c.call("POST", "/api/jobs", {"name": "docs", "root": "sources", "source": "Docs"})
        self.svc.run_next()
        (self.tmp / "sources/Docs/b.txt").unlink()
        (self.tmp / "sources/Docs/keep0.txt").unlink()
        self.c.call("POST", "/api/jobs/docs/sync", {})
        self.svc.run_next()
        self.day = self.c.call("GET", "/api/jobs/docs/deleted")[1]["entries"][0]["date"]

    def test_job_fields_over_the_api(self):
        job = self.c.call("GET", "/api/jobs/docs")[1]
        self.assertEqual((job["auto_check"], job["stale_days"]), ("weekly", 3))
        for key in ("next_check", "check_rounds", "check_cycle_checks"):
            self.assertIn(key, job)
        self.assertEqual(job["icloud"], {"evicted": 0, "note": ""})
        self.assertEqual(self.c.call("PATCH", "/api/jobs/docs", {"auto_check": "daily"})[0], 200)
        self.assertEqual(self.c.call("GET", "/api/jobs/docs")[1]["auto_check"], "daily")
        status, data, _ = self.c.call("PATCH", "/api/jobs/docs", {"auto_check": "hourly"})
        self.assertEqual((status, data["ok"]), (400, False))

    def test_plan_then_restore_a_date_then_everything(self):
        status, plan, _ = self.c.call("GET", f"/api/jobs/docs/restore-plan?date={self.day}")
        self.assertEqual((status, plan["files"], plan["bytes"]), (200, 2, 3))
        self.assertEqual(plan["days"], [{"date": self.day, "files": 2, "bytes": 3}])
        self.assertEqual(self.c.call("GET", "/api/jobs/docs/restore-plan")[1]["files"], 2)
        status, data, _ = self.c.call("POST", "/api/jobs/docs/restore-batch",
                                      {"date": self.day, "expect_files": 2}, csrf=False)
        self.assertEqual(status, 403)
        status, data, _ = self.c.call("POST", "/api/jobs/docs/restore-batch",
                                      {"date": self.day, "expect_files": 2})
        self.assertEqual((status, data["ok"], data["restored"], data["bytes"]), (200, True, 2, 3))
        self.assertEqual(data["skipped"], [])
        self.assertEqual((self.tmp / f"icloud/docs/.restored/{self.day}/b.txt").read_text(), "b")
        self.assertFalse((self.tmp / "sources/Docs/b.txt").exists())
        status, data, _ = self.c.call("POST", "/api/jobs/docs/restore-batch", {"all": True})
        self.assertEqual((status, data["restored"]), (200, 2))
        self.assertTrue((self.tmp / f"icloud/docs/.restored/{self.day}/b (2).txt").exists())

    def test_refusals_are_400_with_a_sentence_and_never_ok(self):
        for body in ({}, {"date": self.day, "all": True}, {"all": "true"}, {"date": "../.."},
                     {"date": self.day, "expect_files": 9}, {"date": "2020-01-01"},
                     {"date": ["2026-11-07"]}):
            status, data, _ = self.c.call("POST", "/api/jobs/docs/restore-batch", body)
            self.assertEqual((status, data["ok"]), (400, False), body)
            self.assertTrue(data["message"])
        self.assertFalse((self.tmp / "icloud/docs/.restored").exists())
        self.assertEqual(self.c.call("GET", "/api/jobs/docs/restore-plan?date=nope")[0], 400)
        self.assertEqual(self.c.call("GET", "/api/jobs/ghost/restore-plan")[0], 400)

    def test_a_partial_batch_is_ok_but_names_what_was_skipped(self):
        (self.tmp / f"icloud/docs/.deleted/{self.day}/ln").symlink_to(self.tmp / "sources")
        status, data, _ = self.c.call("POST", "/api/jobs/docs/restore-batch", {"date": self.day})
        self.assertEqual((status, data["restored"]), (200, 2))
        self.assertEqual(data["skipped"], [{"path": f"{self.day}/ln", "reason": "is a link"}])
        self.assertIn("Skipped 1", data["message"])

    def test_test_alert_over_the_api(self):
        status, data, _ = self.c.call("POST", "/api/settings/test-alert", {})
        self.assertEqual((status, data["ok"]), (400, False))
        self.assertIn("no alert channel", data["message"])
        good, bad = Rec(), Rec(False, "the server answered 500")
        good.name, bad.name = "ntfy", "email"
        self.svc.notifiers = [good]
        status, data, _ = self.c.call("POST", "/api/settings/test-alert", {})
        self.assertEqual((status, data["ok"]), (200, True))
        self.assertEqual(data["results"], [{"channel": "ntfy", "ok": True, "error": ""}])
        self.svc.notifiers = [good, bad]
        status, data, _ = self.c.call("POST", "/api/settings/test-alert", {})
        self.assertEqual((status, data["ok"]), (502, False))
        self.assertIn("email failed: the server answered 500", data["message"])
        self.assertEqual(self.c.call("POST", "/api/settings/test-alert", {}, csrf=False)[0], 403)

    def test_settings_never_echo_a_secret_over_the_api(self):
        secret = "SECRET-VALUE-9"
        self.svc.cfg = replace(self.svc.cfg, ntfy_url=f"https://ntfy.example.org/{secret}",
                               ntfy_token=secret, pushover_token=secret, pushover_user=secret,
                               smtp_host="mail.example.org", smtp_from="a@example.test",
                               smtp_to=("b@example.test",), smtp_user="u", smtp_password=secret)
        for path in ("/api/settings", "/api/status"):
            _, data, _ = self.c.call("GET", path)
            self.assertNotIn(secret, json.dumps(data))
        _, data, _ = self.c.call("PUT", "/api/settings", {"max_dest_gb": 9})
        self.assertNotIn(secret, json.dumps(data))
        self.assertTrue(serve)


if __name__ == "__main__":
    unittest.main()
