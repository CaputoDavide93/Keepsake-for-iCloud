"""Engine side of the trust batch: what is visible about iCloud (evicted
placeholders), rotating samples for "check a copy", and restoring a whole date or
everything kept. Each guard has a fixture that fails without it."""

import math
import os
import unittest
from unittest import mock

from keepsake import engine as syncer
from keepsake.engine import (
    Refused,
    count_evicted,
    plan_restore_batch,
    restore_batch,
    rotation_key,
    run_job,
    verify_copy,
)
from tests.helpers import make_cfg, write
from tests.test_trust import DAY, NOW, Base, snapshot


class EvictedTests(Base):
    def test_counts_placeholders_and_only_placeholders(self):
        self.run_it()
        self.assertEqual(count_evicted(self.cfg, self.job), 0)
        (self.dest / "2025/img0.jpg").rename(self.dest / "2025/.img0.jpg.icloud")
        (self.dest / "2025/img1.jpg").rename(self.dest / "2025/.img1.jpg.icloud")
        write(self.dest / "2025/.hidden-but-not-evicted", "x")      # a dot file, not a placeholder
        self.assertEqual(count_evicted(self.cfg, self.job), 2)

    def test_counting_writes_nothing_and_follows_no_link(self):
        self.run_it()
        outside = self.tmp / "elsewhere"
        write(outside / ".z.jpg.icloud", "x")
        (self.dest / "linked").symlink_to(outside)
        before = snapshot(self.dest)
        self.assertEqual(count_evicted(self.cfg, self.job), 0)
        self.assertEqual(before, snapshot(self.dest))

    def test_refused_for_a_folder_that_is_not_ours(self):
        self.run_it()
        (self.dest / ".keepsake-owner").write_text('{"job": "someone-else"}')
        with self.assertRaises(Refused):
            count_evicted(self.cfg, self.job)

    def test_a_run_records_how_many_files_it_found_evicted(self):
        self.run_it()
        for i in (0, 1, 2):
            (self.dest / f"2025/img{i}.jpg").rename(self.dest / f"2025/.img{i}.jpg.icloud")
        report = run_job(self.cfg, self.job, now=NOW)
        self.assertEqual((report.result, report.evicted), ("ok", 3))

    def test_a_run_refused_before_it_read_icloud_records_none(self):
        self.run_it()
        for p in self.src.rglob("*.jpg"):
            p.unlink()
        report = run_job(self.cfg, self.job, now=NOW)
        self.assertEqual((report.result, report.evicted), ("failed", None))


class RotationTests(Base):
    def setUp(self):
        super().setUp()
        for i in range(110):
            write(self.src / f"bulk/f{i:03d}.txt", f"content {i}", 1_700_000_000)
        self.run_it()          # 120 files

    def spy(self):
        seen = []
        real = syncer.open_regular_at

        def wrapper(base_fd, rel):
            seen.append(rel)
            return real(base_fd, rel)
        return seen, wrapper

    def test_repeated_checks_cover_every_file(self):
        seen, wrapper = self.spy()
        cursor = {"seed": "s1"}
        with mock.patch.object(syncer, "open_regular_at", wrapper):
            for _ in range(math.ceil(120 / syncer.VERIFY_FILES)):
                self.assertTrue(verify_copy(self.cfg, self.job, NOW, cursor)["ok"])
        sources = {p for p in seen}
        every = {str(p.relative_to(self.src)) for p in self.src.rglob("*") if p.is_file()}
        self.assertTrue(every <= sources, f"never looked at {sorted(every - sources)[:3]}")
        self.assertEqual(cursor["rounds"], 1)

    def test_without_rotation_a_check_does_not_move_on(self):
        a = verify_copy(self.cfg, self.job, NOW)
        b = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual((a["checked"], b["checked"]), (40, 40))

    def test_each_check_carries_on_after_the_last_file_the_previous_one_read(self):
        seen, wrapper = self.spy()
        cursor = {"seed": "s2"}
        with mock.patch.object(syncer, "open_regular_at", wrapper):
            verify_copy(self.cfg, self.job, NOW, cursor)
            first = set(seen)
            seen.clear()
            verify_copy(self.cfg, self.job, NOW, cursor)
        self.assertFalse(first & set(seen), "the second check repeated the first")

    def test_the_order_depends_on_the_path_only(self):
        a = sorted(rotation_key("x", f"p{i}") for i in range(5))
        self.assertEqual(a, sorted(rotation_key("x", f"p{i}") for i in reversed(range(5))))
        self.assertNotEqual(rotation_key("x", "p"), rotation_key("y", "p"))

    def test_a_file_added_between_checks_does_not_make_a_lap_miss_others(self):
        seen, wrapper = self.spy()
        cursor = {"seed": "s3"}
        with mock.patch.object(syncer, "open_regular_at", wrapper):
            verify_copy(self.cfg, self.job, NOW, cursor)
            write(self.src / "new/added.txt", "n", 1_700_000_000)
            self.run_it()
            for _ in range(4):
                verify_copy(self.cfg, self.job, NOW, cursor)
        every = {str(p.relative_to(self.src)) for p in self.src.rglob("*")
                 if p.is_file() and p.name != "added.txt"}
        self.assertTrue(every <= set(seen))

    def test_a_check_that_compared_nothing_still_moves_the_cursor(self):
        cursor = {"seed": "s4"}
        for p in self.src.rglob("*.*"):
            os.utime(p, (1_700_009_000, 1_700_009_000))
        with self.assertRaises(Refused):
            verify_copy(self.cfg, self.job, NOW, cursor)
        self.assertTrue(cursor["after"], "stuck on the same files forever")

    def test_the_byte_budget_ends_a_check_without_skipping_the_rest(self):
        seen, wrapper = self.spy()
        cursor = {"seed": "s5"}
        with mock.patch.object(syncer, "open_regular_at", wrapper), \
                mock.patch.object(syncer, "VERIFY_MAX_BYTES", 40):
            v = verify_copy(self.cfg, self.job, NOW, cursor)
        self.assertLess(v["checked"], 40)
        self.assertEqual(v["skipped"], 0, "files past the budget are for next time, not skipped")


class RestoreBatchBase(Base):
    def keep_two_days(self):
        """img8 deleted today (DAY), img9 kept under an earlier date."""
        self.keep_one("2025/img9.jpg")
        older = self.dest / ".deleted/2026-11-03/2025/img9.jpg"
        write(older, "old nine", 1_700_000_000)
        write(self.dest / ".deleted/2026-11-03/docs/readme.txt", "hello", 1_700_000_000)

    def batch(self, day=DAY, **kw):
        return restore_batch(self.cfg, self.job, day, **kw)


class RestoreBatchTests(RestoreBatchBase):
    def test_one_date_keeps_relative_paths_under_that_date(self):
        self.keep_two_days()
        out = self.batch("2026-11-03")
        self.assertEqual(out["restored"], 2)
        self.assertEqual((self.dest / ".restored/2026-11-03/docs/readme.txt").read_text(), "hello")
        self.assertEqual((self.dest / ".restored/2026-11-03/2025/img9.jpg").read_text(), "old nine")
        self.assertFalse((self.dest / f".restored/{DAY}").exists(), "another date was touched")
        self.assertIn("restored 2 files", out["message"])
        self.assertIn("source was not touched", out["message"])
        self.assertTrue((self.dest / ".deleted/2026-11-03/docs/readme.txt").exists())
        self.assertFalse(list(self.dest.rglob(".icsync-*")), "no temp file is left")

    def test_everything_kept_covers_every_date(self):
        self.keep_two_days()
        out = self.batch(None)
        self.assertEqual((out["restored"], out["dates"]), (3, ["2026-11-03", DAY]))
        self.assertTrue((self.dest / f".restored/{DAY}/2025/img9.jpg").exists())
        self.assertIn("(2 dates)", out["message"])

    def test_the_source_and_everything_outside_the_icloud_folder_is_untouched(self):
        self.keep_two_days()
        outside = {k: v for k, v in snapshot(self.tmp).items() if not k.startswith(str(self.dest))}
        self.batch(None)
        after = {k: v for k, v in snapshot(self.tmp).items() if not k.startswith(str(self.dest))}
        self.assertEqual(outside, after)

    def test_a_collision_with_a_restored_file_gets_a_number_and_never_replaces(self):
        self.keep_two_days()
        mine = write(self.dest / ".restored/2026-11-03/docs/readme.txt", "I edited this")
        out = self.batch("2026-11-03")
        self.assertEqual(mine.read_text(), "I edited this")
        self.assertEqual((self.dest / ".restored/2026-11-03/docs/readme (2).txt").read_text(), "hello")
        self.assertEqual(out["restored"], 2)

    def test_running_it_twice_never_overwrites_the_first_batch(self):
        self.keep_two_days()
        self.batch("2026-11-03")
        self.batch("2026-11-03")
        self.assertEqual((self.dest / ".restored/2026-11-03/docs/readme (2).txt").read_text(), "hello")
        self.assertEqual((self.dest / ".restored/2026-11-03/docs/readme.txt").read_text(), "hello")

    def test_refused_up_front_for_the_free_disk_reserve_and_writes_nothing(self):
        self.keep_two_days()
        self.cfg = make_cfg(self.tmp, min_free_gb=10 ** 9)
        before = snapshot(self.dest)
        with self.assertRaises(Refused) as ctx:
            self.batch(None)
        self.assertIn("reserve", str(ctx.exception))
        self.assertIn("Nothing was restored", str(ctx.exception))
        self.assertEqual(before, snapshot(self.dest))
        self.assertFalse((self.dest / ".restored").exists())

    def test_the_reserve_is_judged_for_the_whole_batch_not_per_file(self):
        self.keep_two_days()                      # 50 + 8 + 5 bytes kept
        free = 100
        with mock.patch.object(syncer, "GB", 1), \
                mock.patch.object(syncer.shutil, "disk_usage",
                                  return_value=mock.Mock(free=free)):
            self.cfg = make_cfg(self.tmp, min_free_gb=free - 60, max_dest_gb=1000)   # 40 spare: one file fits, all do not
            with self.assertRaises(Refused) as ctx:
                self.batch(None)
        self.assertIn("reserve", str(ctx.exception))
        self.assertFalse((self.dest / ".restored").exists())

    def test_refused_up_front_for_the_folder_size_cap(self):
        self.keep_two_days()
        with mock.patch.object(syncer, "GB", 60):
            self.cfg = make_cfg(self.tmp, max_dest_gb=1)
            with self.assertRaises(Refused) as ctx:
                self.batch(None)
        self.assertIn("over its", str(ctx.exception))
        self.assertFalse((self.dest / ".restored").exists())

    def test_a_symlink_in_the_kept_tree_is_skipped_named_and_never_followed(self):
        self.keep_two_days()
        secret = write(self.tmp / "outside.txt", "private")
        kept = self.dest / ".deleted/2026-11-03"
        (kept / "docs/link.txt").symlink_to(secret)
        (kept / "linkdir").symlink_to(self.tmp)
        out = self.batch("2026-11-03")
        self.assertEqual(out["restored"], 2)
        self.assertEqual(sorted(s["path"] for s in out["skipped"]),
                         ["2026-11-03/docs/link.txt", "2026-11-03/linkdir"])
        self.assertIn("is a link", out["message"])
        names = [p.name for p in (self.dest / ".restored").rglob("*")]
        self.assertNotIn("link.txt", names)
        self.assertNotIn("linkdir", names)
        self.assertNotIn("private", "".join(p.read_text() for p in
                                            (self.dest / ".restored").rglob("*") if p.is_file()))

    def test_a_batch_that_is_all_links_is_a_refusal_not_a_success(self):
        self.run_it()
        (self.dest / ".deleted/2026-11-03").mkdir(parents=True)
        (self.dest / ".deleted/2026-11-03/only-a-link").symlink_to(self.tmp / "sources")
        with self.assertRaises(Refused) as ctx:
            self.batch("2026-11-03")
        self.assertIn("nothing could be restored", str(ctx.exception))

    def test_a_dated_folder_that_is_a_link_is_skipped_and_not_followed(self):
        self.keep_two_days()
        (self.dest / ".deleted/2026-11-04").symlink_to(self.tmp / "sources")
        out = self.batch(None)
        self.assertEqual(out["restored"], 3)
        self.assertIn("2026-11-04", [s["path"] for s in out["skipped"]])

    def test_path_traversal_in_the_date_is_refused(self):
        self.keep_two_days()
        for bad in ("..", "../..", "2026-11-03/../2026-11-07", "2026-13-45", "",
                    "2026-11-03\x00", "/etc", 20261103):
            with self.assertRaises(Refused, msg=repr(bad)):
                self.batch(bad)
        self.assertFalse((self.dest / ".restored").exists())

    def test_names_must_be_plain(self):
        for name in ("", ".", "..", "a/b", "a\x00b"):
            self.assertFalse(syncer._plain_name(name), repr(name))
        self.assertTrue(syncer._plain_name("..hidden"))

    def test_a_name_that_starts_with_dots_is_restored_inside_not_above(self):
        self.keep_two_days()
        write(self.dest / ".deleted/2026-11-03/..odd name", "odd")
        out = self.batch("2026-11-03")
        self.assertEqual(out["restored"], 3)
        self.assertEqual((self.dest / ".restored/2026-11-03/..odd name").read_text(), "odd")
        self.assertFalse((self.dest / ".restored/..odd name").exists())

    def test_the_count_the_person_confirmed_must_still_be_true(self):
        self.keep_two_days()
        with self.assertRaises(Refused) as ctx:
            self.batch("2026-11-03", expect_files=5)
        self.assertIn("not the 5 you were shown", str(ctx.exception))
        self.assertFalse((self.dest / ".restored").exists())
        self.assertEqual(self.batch("2026-11-03", expect_files=2)["restored"], 2)
        for bad in (True, "2", 2.0):
            with self.assertRaises(Refused):
                self.batch("2026-11-03", expect_files=bad)

    def test_a_file_that_changes_while_it_is_read_is_skipped_and_the_rest_carry_on(self):
        self.keep_two_days()
        real = syncer._copy_restored
        calls = {"n": 0}

        def flaky(sfd, st, tfd, name):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(5, "the file changed while it was being read")
            return real(sfd, st, tfd, name)
        with mock.patch.object(syncer, "_copy_restored", flaky):
            out = self.batch("2026-11-03")
        self.assertEqual((out["restored"], len(out["skipped"])), (1, 1))
        self.assertIn("Skipped 1", out["message"])
        self.assertFalse(list(self.dest.rglob(".icsync-*")))

    def test_more_files_than_the_limit_is_refused_with_advice(self):
        self.keep_two_days()
        with mock.patch.object(syncer, "RESTORE_BATCH_MAX", 2):
            with self.assertRaises(Refused) as ctx:
                self.batch(None)
        self.assertIn("one date at a time", str(ctx.exception))

    def test_nothing_kept_is_a_refusal(self):
        self.run_it()
        with self.assertRaises(Refused):
            self.batch(None)
        with self.assertRaises(Refused):
            self.batch("2026-11-03")

    def test_refused_for_a_folder_that_is_not_ours(self):
        self.keep_two_days()
        (self.dest / ".keepsake-owner").write_text('{"job": "someone-else"}')
        with self.assertRaises(Refused):
            self.batch(None)

    def test_a_restored_folder_that_is_a_symlink_is_refused(self):
        self.keep_two_days()
        (self.dest / ".restored").symlink_to(self.tmp)
        with self.assertRaises(Refused):
            self.batch(None)
        self.assertEqual(list(self.tmp.glob("2026-*")), [])

    def test_restored_files_are_not_mirrored_back_or_retired_by_the_next_run(self):
        self.keep_two_days()
        self.batch(None)
        r = run_job(self.cfg, self.job, now=NOW)
        self.assertEqual(r.result, "ok", r.error)
        self.assertTrue((self.dest / ".restored/2026-11-03/docs/readme.txt").exists())
        self.assertFalse((self.src / "docs").exists())


class PlanTests(RestoreBatchBase):
    def test_plan_counts_per_date_and_writes_nothing(self):
        self.keep_two_days()
        before = snapshot(self.dest)
        plan = plan_restore_batch(self.cfg, self.job, None)
        self.assertEqual((plan["files"], plan["bytes"]), (3, 50 + 8 + 5))
        self.assertEqual([(d["date"], d["files"]) for d in plan["days"]],
                         [("2026-11-03", 2), (DAY, 1)])
        self.assertNotIn("_files", plan)
        one = plan_restore_batch(self.cfg, self.job, "2026-11-03")
        self.assertEqual((one["files"], one["bytes"]), (2, 13))
        self.assertEqual(before, snapshot(self.dest))

    def test_plan_names_links_it_would_skip(self):
        self.keep_two_days()
        (self.dest / ".deleted/2026-11-03/ln").symlink_to(self.tmp)
        plan = plan_restore_batch(self.cfg, self.job, "2026-11-03")
        self.assertEqual(plan["skipped"], [{"path": "2026-11-03/ln", "reason": "is a link"}])


if __name__ == "__main__":
    unittest.main()
