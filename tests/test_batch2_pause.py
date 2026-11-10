"""Pause everything until a time, and daily quiet hours. A pause or a quiet
window stops what the schedule would start, never a run that is already going,
never what a person asks for (Sync now, Preview), and a paused folder is never
reported as late, stale or failing."""

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from keepsake import service as service_mod
from keepsake.config import Config, ConfigError
from keepsake.server import serve
from keepsake.service import Service, quiet_end_after, quiet_overlap, zone
from tests.helpers import make_cfg, write
from tests.test_api import Client
from tests.test_service import Clock, Recorder

UTC = timezone.utc
T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)      # a Saturday, midday UTC


class PauseBase(unittest.TestCase):
    tz = "UTC"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp, tz=self.tz)
        self.rec = Recorder()
        self.clock = Clock()
        self.clock.now = T0
        self.svc = Service(self.cfg, [self.rec], self.clock)
        for i in range(3):
            write(self.tmp / f"sources/Photos/{i}.jpg", f"p{i}", 1_700_000_000)

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, **kw):
        self.svc.add_job({"name": "photos", "root": "sources", "source": "Photos", **kw})

    def view(self):
        return self.svc.status()["jobs"][0]

    def pause(self, **spec):
        return self.svc.update_settings({"pause": spec})

    def quiet(self, start, end):
        return self.svc.update_settings({"quiet_hours": {"start": start, "end": end}})

    def at(self, hour, minute=0, days=0):
        self.clock.now = T0.replace(hour=hour, minute=minute) + timedelta(days=days)

    def alerts(self, kind=None):
        return [e for e in self.rec.events if kind is None or e["event"] == kind]


class PauseUntil(PauseBase):
    def test_the_presets(self):
        self.pause(preset="1h")
        self.assertEqual(self.svc.resume_at(), T0 + timedelta(hours=1))
        self.pause(preset="1w")
        self.assertEqual(self.svc.resume_at(), T0 + timedelta(days=7))
        self.pause(preset="tomorrow")
        self.assertEqual(self.svc.resume_at(), datetime(2026, 11, 8, 8, 0, tzinfo=UTC))

    def test_tomorrow_follows_TZ_not_utc(self):
        svc = Service(replace(self.cfg, tz="America/New_York"), [], self.clock)
        # 12:00 UTC on 7 Nov is 07:00 in New York (UTC-5): tomorrow 08:00 is 13:00 UTC on the 8th.
        svc.update_settings({"pause": {"preset": "tomorrow"}})
        self.assertEqual(svc.resume_at(), datetime(2026, 11, 8, 13, 0, tzinfo=UTC))

    def test_a_custom_time_is_read_in_TZ_and_an_offset_wins(self):
        svc = Service(replace(self.cfg, tz="America/New_York"), [], self.clock)
        svc.update_settings({"pause": {"until": "2026-11-09T09:30"}})
        self.assertEqual(svc.resume_at(), datetime(2026, 11, 9, 14, 30, tzinfo=UTC))
        svc.update_settings({"pause": {"until": "2026-11-09T09:30:00+00:00"}})
        self.assertEqual(svc.resume_at(), datetime(2026, 11, 9, 9, 30, tzinfo=UTC))

    def test_bad_requests_are_sentences(self):
        for spec in ({}, {"preset": "1h", "until": "2026-11-09T09:30"}, {"preset": "2d"},
                     {"until": "yesterday"}, {"until": "2026-11-07T11:00"},
                     {"until": "2028-01-01T00:00"}, "1h", 3):
            with self.assertRaises(ConfigError, msg=repr(spec)):
                self.svc.update_settings({"pause": spec})
            self.assertIsNone(self.svc.resume_at())

    def test_it_passes_when_the_resume_time_comes(self):
        self.add()
        self.pause(preset="1h")
        self.assertFalse(self.svc.run_next())
        self.assertTrue(self.svc.pause_view()["paused"])
        self.clock.now = T0 + timedelta(hours=1, seconds=1)
        self.assertFalse(self.svc.pause_view()["paused"])
        self.assertTrue(self.svc.run_next())
        self.assertEqual(self.view()["result"], "ok")

    def test_housekeeping_clears_an_ended_pause_and_remembers_when_it_ended(self):
        self.pause(preset="1h")
        self.clock.now = T0 + timedelta(hours=2)
        self.svc.housekeeping()
        stored = json.loads(self.cfg.settings_file.read_text())
        self.assertEqual(stored["pause_until"], "")
        self.assertEqual(stored["pause_ended"], (T0 + timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertIn("pause ended", self.svc.activity()[0]["message"])

    def test_resume_now_ends_it_at_once(self):
        self.add()
        self.pause(preset="1w")
        self.svc.update_settings({"pause": None})
        self.assertIsNone(self.svc.resume_at())
        self.assertEqual(json.loads(self.cfg.settings_file.read_text())["pause_ended"],
                         T0.isoformat(timespec="seconds"))
        self.assertTrue(self.svc.run_next())

    def test_it_survives_a_restart(self):
        self.pause(preset="1w")
        again = Service(Config.from_env({"KEEPSAKE_DATA": str(self.cfg.data_dir)}
                                        ).with_settings(), [], self.clock)
        self.assertEqual(again.resume_at(), T0 + timedelta(days=7))

    def test_it_is_logged_and_shown_with_a_plain_sentence(self):
        self.pause(preset="1h")
        self.assertIn("everything paused until", self.svc.activity()[0]["message"])
        view = self.svc.status()["pause"]
        self.assertEqual((view["paused"], view["kind"]), (True, "pause"))
        self.assertIn("a sync that is already running is not interrupted", view["message"])
        self.assertIn("Sync now and Preview still work", view["message"])


class QuietHours(PauseBase):
    def test_a_window_across_midnight(self):
        self.quiet("22:00", "07:00")
        for hour, minute, inside in [(21, 59, False), (22, 0, True), (23, 30, True), (0, 0, True),
                                     (6, 59, True), (7, 0, False), (12, 0, False)]:
            self.at(hour, minute)
            self.assertEqual(self.svc.gated(), inside, f"{hour}:{minute:02d}")
        self.at(23, 30)
        self.assertEqual(self.svc.resume_at(), datetime(2026, 11, 8, 7, 0, tzinfo=UTC))
        self.at(3, 0)
        self.assertEqual(self.svc.resume_at(), datetime(2026, 11, 7, 7, 0, tzinfo=UTC))

    def test_a_window_inside_one_day(self):
        self.quiet("13:00", "15:00")
        for hour, inside in [(12, False), (13, True), (14, True), (15, False)]:
            self.at(hour)
            self.assertEqual(self.svc.gated(), inside)
        self.at(14)
        self.assertEqual(self.svc.resume_at(), datetime(2026, 11, 7, 15, 0, tzinfo=UTC))

    def test_the_window_is_in_TZ(self):
        svc = Service(replace(self.cfg, tz="Asia/Tokyo"), [], self.clock)   # UTC+9
        svc.update_settings({"quiet_hours": {"start": "22:00", "end": "07:00"}})
        self.at(14)                     # 23:00 in Tokyo
        self.assertTrue(svc.gated())
        self.at(23)                     # 08:00 in Tokyo
        self.assertFalse(svc.gated())

    def test_bad_windows_are_refused(self):
        for start, end in [("22:00", "22:00"), ("25:00", "07:00"), ("22:00", ""), ("", "07:00"), ("a", "b")]:
            with self.assertRaises(ConfigError, msg=(start, end)):
                self.quiet(start, end)
        with self.assertRaises(ConfigError):
            self.svc.update_settings({"quiet_hours": "night"})
        self.assertIsNone(self.svc.status()["pause"]["quiet"])

    def test_it_can_be_switched_off(self):
        self.quiet("22:00", "07:00")
        self.svc.update_settings({"quiet_hours": None})
        self.at(23)
        self.assertFalse(self.svc.gated())

    def test_scheduled_runs_wait_until_it_ends_and_then_start(self):
        self.add()
        self.quiet("22:00", "07:00")
        self.at(23)
        self.assertFalse(self.svc.run_next())
        self.assertEqual(self.view()["status"], "never")
        self.at(7, 0, days=1)
        self.assertTrue(self.svc.run_next())

    def test_a_pause_that_ends_inside_the_window_waits_for_the_window(self):
        self.quiet("22:00", "07:00")
        self.pause(until="2026-11-08T03:00")
        self.assertEqual(self.svc.resume_at(), datetime(2026, 11, 8, 7, 0, tzinfo=UTC))
        self.assertEqual(self.svc.status()["pause"]["until"], "2026-11-08T07:00:00+00:00")

    def test_helpers(self):
        tz = zone("UTC")
        self.assertIsNone(quiet_end_after(T0, "", "", tz))
        self.assertEqual(quiet_overlap(datetime(2026, 11, 7, 21, 0, tzinfo=UTC),
                                       datetime(2026, 11, 8, 7, 0, tzinfo=UTC), "22:00", "07:00", tz),
                         timedelta(hours=9))
        self.assertEqual(quiet_overlap(T0, T0, "22:00", "07:00", tz), timedelta(0))


class WhatIsStopped(PauseBase):
    def test_a_running_sync_is_never_interrupted(self):
        self.add()
        real = service_mod.run_job
        seen = {}

        def pause_midway(cfg, job, **kw):
            self.svc.update_settings({"pause": {"preset": "1w"}})      # the person pauses mid-run
            seen["running"] = self.svc.running
            return real(cfg, job, **kw)

        with mock.patch.object(service_mod, "run_job", pause_midway):
            self.assertTrue(self.svc.run_next())
        self.assertEqual(seen["running"], "photos")
        self.assertEqual((self.view()["result"], self.view()["files_copied"]), ("ok", 3))
        self.assertIsNone(self.svc.running)

    def test_scheduled_work_that_was_waiting_is_dropped_but_a_request_is_kept(self):
        self.add()
        self.svc.queue.append(("photos", False))
        self.svc.scheduled.add("photos")      # the schedule put it there
        self.pause(preset="1h")
        self.assertFalse(self.svc.run_next())
        self.assertEqual(self.svc.queue, [])
        self.svc.sync_now("photos")           # a person asked: explicit
        self.assertTrue(self.svc.run_next())
        self.assertEqual(self.view()["result"], "ok")

    def test_sync_now_and_preview_still_work_while_paused(self):
        self.add()
        self.pause(preset="1w")
        self.assertFalse(self.svc.run_next())             # the schedule does not start it
        self.svc.sync_now("photos")
        self.assertTrue(self.svc.run_next())
        self.assertEqual(self.view()["files_copied"], 3)
        write(self.tmp / "sources/Photos/new.jpg", "n")
        self.svc.preview("photos")
        self.assertTrue(self.svc.run_next())
        self.assertEqual(self.view()["preview"]["added"], 1)

    def test_nothing_is_due_while_paused_and_it_is_again_after(self):
        self.add()
        self.assertEqual(self.svc.due_jobs(), ["photos"])
        self.pause(preset="1h")
        self.assertEqual(self.svc.due_jobs(), [])
        self.clock.now = T0 + timedelta(hours=1, seconds=1)
        self.assertEqual(self.svc.due_jobs(), ["photos"])

    def test_a_scheduled_check_respects_a_pause(self):
        self.add(auto_check="daily")
        self.svc.run_next()
        self.clock.now = T0 + timedelta(days=2)
        self.pause(preset="1h")
        self.assertFalse(self.svc.run_due_check())
        self.assertIsNone(self.view()["verify"])
        self.clock.now = T0 + timedelta(days=2, hours=2)
        self.assertTrue(self.svc.run_due_check())
        self.assertEqual(self.view()["verify"]["state"], "match")

    def test_a_scheduled_check_respects_quiet_hours(self):
        self.add(auto_check="daily")
        self.svc.run_next()
        self.quiet("22:00", "07:00")
        self.at(23, 0, days=2)
        self.assertFalse(self.svc.run_due_check())
        self.at(7, 0, days=3)
        self.assertTrue(self.svc.run_due_check())

    def test_the_next_run_shown_is_not_before_the_resume_time(self):
        self.add()
        self.svc.run_next()
        self.clock.now = T0 + timedelta(days=2)       # due long ago
        self.pause(preset="1w")
        self.assertEqual(self.view()["next_run"],
                         (T0 + timedelta(days=9)).isoformat(timespec="seconds"))


class NextCheckShown(PauseBase):
    def test_the_next_check_is_not_before_the_resume_time(self):
        self.add(auto_check="daily")
        self.svc.run_next()
        self.svc.verify_job("photos")
        self.clock.now = T0 + timedelta(days=3)
        self.pause(preset="1w")
        self.assertEqual(self.view()["next_check"],
                         (T0 + timedelta(days=10)).isoformat(timespec="seconds"))


class NotLateWhilePaused(PauseBase):
    def old_success(self, days=10):
        self.add()
        self.svc.run_next()
        self.clock.now = T0 + timedelta(days=days)

    def test_the_stale_alert_is_suppressed_while_paused(self):
        self.old_success()
        self.svc.check_stale()                 # control: unpaused, ten days on, it fires
        self.assertEqual(len(self.alerts("stale")), 1)
        self.rec.events.clear()
        self.svc.state["photos"].pop("stale_alerted")
        self.pause(preset="1w")
        self.svc.check_stale()
        self.assertEqual(self.alerts(), [])
        self.assertNotIn("stale_alerted", self.svc.state["photos"])

    def test_a_long_pause_does_not_count_as_a_silent_folder_afterwards(self):
        self.add()
        self.svc.run_next()
        self.pause(until="2026-11-17T12:00")                    # ten days
        self.clock.now = T0 + timedelta(days=10, minutes=1)
        self.svc.housekeeping()                                  # records the end of the pause
        self.assertEqual(self.alerts(), [], "10 days of pause are not 10 days of silence")
        self.assertEqual(self.svc.attention(self.svc._jobs()), [])
        # ... but silence after the pause still counts, in full.
        self.clock.now = T0 + timedelta(days=10, hours=1) + timedelta(days=4)
        self.svc.check_stale()
        self.assertEqual(len(self.alerts("stale")), 1)

    def test_attention_does_not_call_a_paused_folder_late(self):
        self.old_success()
        self.assertEqual([a["reason"] for a in self.svc.attention(self.svc._jobs())],
                         ["has not run on time"])             # control
        self.pause(preset="1w")
        self.assertEqual(self.svc.attention(self.svc._jobs()), [])
        self.assertEqual(self.svc.status()["attention_count"], 0)

    def test_a_real_failure_is_still_reported_while_paused(self):
        self.add()
        self.svc.run_next()
        for i in range(3):
            (self.tmp / f"sources/Photos/{i}.jpg").unlink()
        write(self.tmp / "sources/Photos/x.jpg", "x")
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual(self.view()["result"], "failed")
        self.pause(preset="1w")
        self.clock.now = T0 + timedelta(days=3)
        self.assertEqual(len(self.svc.attention(self.svc._jobs())), 1)

    def test_quiet_hours_are_not_the_folders_fault(self):
        # Hourly folder, quiet 22:00-07:00. Last success 21:00; at 07:00 it has been
        # 10 hours, nine of them quiet, so it is not late (the grace is 6 hours).
        self.add(schedule={"every_hours": 1})
        self.at(21)
        self.svc.run_next()
        self.quiet("22:00", "07:00")
        self.at(7, 0, days=1)
        self.clock.now += timedelta(seconds=30)
        self.assertEqual(self.svc.attention(self.svc._jobs()), [])
        self.clock.now += timedelta(hours=7)                     # genuinely late by 14:00
        self.assertEqual(len(self.svc.attention(self.svc._jobs())), 1)

    def test_a_paused_folder_is_not_failing(self):
        self.add()
        self.svc.run_next()
        self.svc.update_job("photos", {"enabled": False})
        self.clock.now = T0 + timedelta(days=30)
        self.svc.check_stale()
        self.assertEqual((self.view()["status"], self.alerts(), self.svc.attention(self.svc._jobs())),
                         ("paused", [], []))


class PauseApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        cfg = replace(make_cfg(self.tmp), port=0, host="127.0.0.1")
        self.svc = Service(cfg, [])
        self.httpd = serve(self.svc)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.c = Client(self.httpd.server_address[1])
        self.c.open()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def test_pause_and_quiet_hours_over_the_api(self):
        status, data, _ = self.c.call("PUT", "/api/settings", {"pause": {"preset": "1h"}})
        self.assertEqual((status, data["settings"]["pause"]["paused"]), (200, True))
        status, data, _ = self.c.call("GET", "/api/status")
        self.assertEqual((data["pause"]["kind"], data["pause"]["paused"]), ("pause", True))
        status, data, _ = self.c.call("PUT", "/api/settings", {"pause": None,
                                      "quiet_hours": {"start": "22:00", "end": "07:00"}})
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["pause"]["quiet"], {"start": "22:00", "end": "07:00"})
        self.assertIsNone(data["settings"]["pause"]["pause_until"])

    def test_a_bad_pause_is_a_400_with_a_sentence_and_nothing_changes(self):
        status, data, _ = self.c.call("PUT", "/api/settings", {"pause": {"until": "2001-01-01T00:00"}})
        self.assertEqual((status, data["ok"]), (400, False))
        self.assertIn("already passed", data["message"])
        self.assertFalse(self.svc.status()["pause"]["paused"])

    def test_the_usual_settings_still_save_beside_it(self):
        status, data, _ = self.c.call("PUT", "/api/settings", {"shrink_pct": 30, "pause": {"preset": "1h"}})
        self.assertEqual(status, 200)
        self.assertEqual(self.svc.cfg.shrink_pct, 30)
        self.assertEqual(json.loads((self.tmp / "data/settings.json").read_text())["shrink_pct"], 30)


if __name__ == "__main__":
    unittest.main()
