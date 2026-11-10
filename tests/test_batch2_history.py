"""Per-night detail: what one night recorded, kept bounded, tolerant of the older
state files that have no detail, and honest about which night it belongs to."""

import json
import unittest
from datetime import timedelta

from keepsake.engine import RunReport
from keepsake.service import (
    DETAIL_ERROR_MAX,
    DETAIL_PATH_MAX,
    DETAIL_SAMPLES,
    HISTORY_DAYS,
    Service,
    clean_detail,
    clean_history,
    merge_history,
    run_detail,
)
from tests.helpers import make_job, write
from tests.test_service import Base

TODAY = "2026-11-07"


def report(added=0, updated=0, removed=0, error="", started="2026-11-07T03:00:00+00:00"):
    r = RunReport(job="photos", result="failed" if error else "ok", error=error, started=started)
    for i in range(added):
        r.note_change("added", f"a{i}.jpg")
    for i in range(updated):
        r.note_change("updated", f"u{i}.jpg")
    for i in range(removed):
        r.note_change("removed", f"r{i}.jpg")
    return r


class RunDetail(unittest.TestCase):
    def test_counts_examples_and_how_removed_files_are_kept(self):
        d = run_detail(report(2, 1, 1), make_job())
        self.assertEqual((d["added"], d["updated"], d["removed"]), (2, 1, 1))
        self.assertEqual(d["removed_how"], "moved to .deleted")
        self.assertEqual([x["kind"] for x in d["samples"]], ["added", "added", "updated", "removed"])
        self.assertEqual(run_detail(report(0, 0, 1), make_job(mode="mirror"))["removed_how"], "removed")
        self.assertNotIn("error", d)

    def test_examples_are_capped_and_long_paths_and_errors_are_cut(self):
        r = report()
        r.change_samples = [{"kind": "added", "path": "p" * 900}] * 80
        r.files_added = 80
        r.error = "e" * 900
        d = run_detail(r, make_job())
        self.assertEqual(len(d["samples"]), DETAIL_SAMPLES)
        self.assertEqual(len(d["samples"][0]["path"]), DETAIL_PATH_MAX)
        self.assertEqual(len(d["error"]), DETAIL_ERROR_MAX)
        self.assertEqual(d["added"], 80)       # the count stays whole

    def test_a_refused_run_carries_its_sentence(self):
        d = run_detail(report(error="the source shrank from 20 to 5 files"), make_job())
        self.assertEqual(d["error"], "the source shrank from 20 to 5 files")


class MergeDetail(unittest.TestCase):
    def merged(self, history, outcome, detail=None, day=TODAY):
        return merge_history(history, day, outcome, TODAY, detail)

    def detail(self, **kw):
        return run_detail(report(**kw), make_job())

    def test_a_night_with_no_detail_stays_without_one(self):
        out = self.merged([{"day": "2026-11-05", "outcome": "copied"}], "nothing", None)
        self.assertEqual(out[0], {"day": "2026-11-05", "outcome": "copied"})
        self.assertNotIn("detail", out[0])

    def test_the_latest_run_decides_and_its_detail_goes_with_it(self):
        first = self.merged([], "paused", self.detail(error="refused"))
        self.assertEqual(first[0]["detail"]["error"], "refused")
        second = self.merged(first, "copied", self.detail(added=3))
        self.assertEqual(second[0]["outcome"], "copied")
        self.assertEqual(second[0]["detail"]["added"], 3)
        self.assertNotIn("error", second[0]["detail"])

    def test_a_later_run_that_found_nothing_does_not_hide_the_copy(self):
        copied = self.merged([], "copied", self.detail(added=4))
        out = self.merged(copied, "nothing", self.detail())
        self.assertEqual((out[0]["outcome"], out[0]["detail"]["added"]), ("copied", 4))

    def test_two_copies_on_one_day_add_up_and_say_how_many_runs(self):
        a = self.merged([], "copied", self.detail(added=30))
        b = self.merged(a, "copied", self.detail(added=30, updated=2))
        d = b[0]["detail"]
        self.assertEqual((d["added"], d["updated"], d["runs"]), (60, 2, 2))
        self.assertEqual(len(d["samples"]), DETAIL_SAMPLES)

    def test_a_later_failure_replaces_the_copy_as_the_outcome_does(self):
        a = self.merged([], "copied", self.detail(added=4))
        b = self.merged(a, "paused", self.detail(error="no"))
        self.assertEqual((b[0]["outcome"], b[0]["detail"]["error"]), ("paused", "no"))

    def test_stays_bounded_to_fourteen_entries_of_fifty_examples(self):
        history = []
        for n in range(1, 29):
            history = merge_history(history, f"2026-10-{n:02d}", "copied", f"2026-10-{n:02d}",
                                    run_detail(self.big(), make_job()))
        self.assertEqual(len(history), HISTORY_DAYS)
        self.assertTrue(all(len(e["detail"]["samples"]) <= DETAIL_SAMPLES for e in history))
        self.assertLess(len(json.dumps(history)), 14 * 50 * 400)

    def big(self):
        r = report()
        r.change_samples = [{"kind": "added", "path": f"folder/{'x' * 100}/{i}.jpg"} for i in range(50)]
        r.files_added = 500
        return r


class Tolerance(unittest.TestCase):
    def test_older_and_damaged_details_never_break_the_strip(self):
        junk = [{"day": "2026-11-05", "outcome": "copied", "detail": "text"},
                {"day": "2026-11-06", "outcome": "copied", "detail": {"added": -1, "updated": 0, "removed": 0}},
                {"day": TODAY, "outcome": "copied", "detail": {"added": True, "updated": 0, "removed": 0}},
                {"day": "2026-11-04", "outcome": "nothing", "detail": {"added": 0, "updated": 0, "removed": 0,
                                                                       "samples": "nope"}}]
        out = clean_history(junk, TODAY)
        self.assertEqual([e["day"] for e in out], ["2026-11-04", "2026-11-05", "2026-11-06", TODAY])
        self.assertEqual([("detail" in e) for e in out], [True, False, False, False])
        self.assertEqual(out[0]["detail"]["samples"], [])

    def test_an_oversized_stored_detail_is_cut_down_on_read(self):
        raw = {"added": 1, "updated": 0, "removed": 0, "runs": 1,
               "samples": [{"kind": "added", "path": "q" * 5000}] * 200}
        d = clean_detail(raw)
        self.assertEqual(len(d["samples"]), DETAIL_SAMPLES)
        self.assertEqual(len(d["samples"][0]["path"]), DETAIL_PATH_MAX)

    def test_unknown_sample_kinds_and_non_text_paths_are_dropped(self):
        d = clean_detail({"added": 0, "updated": 0, "removed": 0,
                          "samples": [{"kind": "exec", "path": "x"}, {"kind": "added", "path": 3},
                                      {"kind": "added", "path": "ok"}, 7]})
        self.assertEqual(d["samples"], [{"kind": "added", "path": "ok"}])


class EndToEnd(Base):
    def test_a_real_run_records_the_night_with_its_files(self):
        self.add()
        self.svc.run_next()
        history = self.view()["history"]
        self.assertEqual(len(history), 1)
        d = history[0]["detail"]
        self.assertEqual((d["added"], d["updated"], d["removed"], d["runs"]), (5, 0, 0, 1))
        self.assertEqual(len(d["samples"]), 5)
        self.assertTrue(all(x["path"].startswith("2025/") for x in d["samples"]))

    def test_a_state_file_from_before_has_nights_with_no_detail(self):
        self.add()
        self.svc.run_next()
        state = json.loads(self.cfg.state_file.read_text())
        for entry in state["photos"]["history"]:
            entry.pop("detail")
        state["photos"]["history"].insert(0, {"day": "2026-11-05", "outcome": "copied"})
        self.cfg.state_file.write_text(json.dumps(state))
        svc = Service(self.cfg, [self.rec], self.clock)
        history = svc.status()["jobs"][0]["history"]
        self.assertEqual([e["day"] for e in history], ["2026-11-05", "2026-11-07"])
        self.assertTrue(all("detail" not in e for e in history))
        # ... and the next run after the upgrade records detail for its own night only.
        write(self.tmp / "sources/Photos/2025/new.jpg", "n", 1_700_000_100)
        self.clock.now += timedelta(days=1)
        svc.sync_now("photos")
        svc.run_next()
        history = svc.status()["jobs"][0]["history"]
        self.assertEqual([("detail" in e) for e in history], [False, False, True])

    def test_a_refused_run_records_the_sentence_for_its_night(self):
        self.add()
        self.svc.run_next()
        for i in range(1, 5):
            (self.tmp / f"sources/Photos/2025/{i}.jpg").unlink()
        self.clock.now += timedelta(days=1)
        self.svc.sync_now("photos")
        self.svc.run_next()
        last = self.view()["history"][-1]
        self.assertEqual(last["outcome"], "paused")
        self.assertIn("shrank", last["detail"]["error"])


if __name__ == "__main__":
    unittest.main()
