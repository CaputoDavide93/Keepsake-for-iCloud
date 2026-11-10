"""Trust features in the engine: check a copy, the deleted-files list, restore,
and the per-run change record. Every guard has an attack fixture."""

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from keepsake import engine as syncer
from keepsake.engine import Refused, list_deleted, restore_deleted, run_job, verify_copy
from tests.helpers import make_cfg, make_job, write

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)
DAY = "2026-11-07"


def snapshot(root: Path):
    """Everything under root: kind, size, mtime, content or link target."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        if p.is_symlink():
            out[str(p)] = ("link", os.readlink(p))
        elif p.is_file():
            out[str(p)] = ("file", st.st_size, st.st_mtime_ns, p.read_bytes())
        else:
            out[str(p)] = ("dir", st.st_mtime_ns)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.job = make_job()
        self.src = self.tmp / "sources" / "Photos"
        self.dest = self.tmp / "icloud" / "photos"
        for i in range(10):
            write(self.src / f"2025/img{i}.jpg", f"data{i}" * 10, 1_700_000_000)

    def tearDown(self):
        self._tmp.cleanup()

    def run_it(self, **kw):
        r = run_job(self.cfg, self.job, now=NOW, **kw)
        self.assertEqual(r.result, "ok", r.error)
        return r

    def keep_one(self, name="2025/img9.jpg"):
        """Run, delete one source file, run again: it sits in .deleted/DAY."""
        self.run_it()
        (self.src / name).unlink()
        self.run_it()
        return self.dest / f".deleted/{DAY}/{name}"


class VerifyTests(Base):
    def test_all_match(self):
        self.run_it()
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual((v["state"], v["ok"], v["checked"], v["mismatched"]),
                         ("match", True, 10, 0))
        self.assertEqual(v["message"], "Checked 10 files, all match")

    def test_samples_at_most_forty_files(self):
        for i in range(60):
            write(self.src / f"many/f{i}.txt", f"v{i}", 1_700_000_000)
        self.run_it()
        self.assertEqual(verify_copy(self.cfg, self.job, NOW)["checked"], 40)

    def test_same_size_corruption_is_caught_by_the_content_hash(self):
        self.run_it()
        target = self.dest / "2025/img3.jpg"
        data = target.read_bytes()
        target.write_bytes(b"X" * len(data))   # same size, same-ish mtime irrelevant
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual(v["state"], "mismatch")
        self.assertFalse(v["ok"])
        self.assertEqual(v["first_mismatch"]["path"], "2025/img3.jpg")
        self.assertIn("different content", v["message"])
        self.assertIn("'2025/img3.jpg'", v["message"])

    def test_missing_and_wrong_size_files_are_mismatches(self):
        self.run_it()
        (self.dest / "2025/img1.jpg").unlink()
        (self.dest / "2025/img2.jpg").write_text("short")
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual(v["state"], "mismatch")
        self.assertEqual(v["mismatched"], 2)
        self.assertIn("and 1 more", v["message"])

    def test_check_never_writes_to_either_side(self):
        self.run_it()
        (self.dest / "2025/img3.jpg").write_bytes(b"Y" * 50)   # even with a mismatch
        before = (snapshot(self.src), snapshot(self.dest),
                  snapshot(self.tmp / "data"))
        verify_copy(self.cfg, self.job, NOW)
        after = (snapshot(self.src), snapshot(self.dest),
                 snapshot(self.tmp / "data"))
        self.assertEqual(before, after)

    def test_a_symlink_in_icloud_is_not_followed(self):
        self.run_it()
        outside = self.tmp / "outside.jpg"
        outside.write_text("data4" * 10)           # same content as the real file
        (self.dest / "2025/img4.jpg").unlink()
        (self.dest / "2025/img4.jpg").symlink_to(outside)
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual(v["state"], "mismatch")
        self.assertIn("link or not a plain file", v["message"])

    def test_a_folder_swapped_for_a_symlink_is_not_followed(self):
        self.run_it()
        real = self.tmp / "elsewhere"
        (self.dest / "2025").rename(real)
        (self.dest / "2025").symlink_to(real)
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual(v["state"], "mismatch")   # not "all match" through the link

    def test_a_fifo_in_icloud_cannot_hang_the_check(self):
        self.run_it()
        (self.dest / "2025/img5.jpg").unlink()
        os.mkfifo(self.dest / "2025/img5.jpg")
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual(v["state"], "mismatch")

    def test_a_file_edited_at_the_source_since_the_run_is_skipped_not_a_mismatch(self):
        self.run_it()
        write(self.src / "2025/img0.jpg", "edited", 1_700_000_900)
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual((v["state"], v["checked"], v["skipped"]), ("match", 9, 1))
        self.assertIn("1 other file could not be compared", v["message"])

    def test_an_evicted_placeholder_is_skipped(self):
        self.run_it()
        (self.dest / "2025/img0.jpg").rename(self.dest / "2025/.img0.jpg.icloud")
        v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual((v["state"], v["checked"], v["skipped"]), ("match", 9, 1))

    def test_a_check_that_compared_nothing_is_a_refusal_not_a_match(self):
        self.run_it()
        for p in self.src.rglob("*.jpg"):
            os.utime(p, (1_700_009_000, 1_700_009_000))   # all edited since the run
        with self.assertRaises(Refused) as ctx:
            verify_copy(self.cfg, self.job, NOW)
        self.assertIn("nothing was checked", str(ctx.exception))

    def test_big_files_are_skipped(self):
        self.run_it()
        with mock.patch.object(syncer, "SAMPLE_MAX_BYTES", 10):
            with self.assertRaises(Refused):
                verify_copy(self.cfg, self.job, NOW)

    def test_refused_before_any_run(self):
        with self.assertRaises(Refused) as ctx:
            verify_copy(self.cfg, self.job, NOW)
        self.assertIn("nothing to check yet", str(ctx.exception))

    def test_refused_when_the_source_is_not_mounted(self):
        self.run_it()
        for p in list(self.tmp.joinpath("sources").rglob("*"))[::-1]:
            p.unlink() if p.is_file() else p.rmdir()
        with self.assertRaises(Refused) as ctx:
            verify_copy(self.cfg, self.job, NOW)
        self.assertEqual(ctx.exception.result, "skipped")

    def test_refused_for_a_folder_that_is_not_ours(self):
        self.run_it()
        (self.dest / ".keepsake-owner").write_text('{"job": "someone-else"}')
        with self.assertRaises(Refused):
            verify_copy(self.cfg, self.job, NOW)


class HardeningTests(Base):
    """Found in the second-opinion review of the engine changes."""

    def test_a_source_folder_named_like_a_reserved_one_refuses_the_run(self):
        self.run_it()
        for name in (".restored", ".deleted", ".removed", ".keepsake-owner"):
            write(self.src / name / "x.txt", "x")
            r = run_job(self.cfg, self.job, now=NOW)
            self.assertEqual(r.result, "failed", name)
            self.assertIn("keeps for its own use", r.error)
            self.assertFalse((self.dest / name / "x.txt").exists(), name)
            import shutil
            shutil.rmtree(self.src / name, ignore_errors=True)
            (self.src / name).unlink(missing_ok=True)

    def test_a_restored_file_is_never_replaced_by_a_source_file_of_that_name(self):
        self.keep_one()
        restore_deleted(self.cfg, self.job, DAY, "2025/img9.jpg")
        write(self.src / ".restored" / DAY / "2025/img9.jpg", "from the source")
        r = run_job(self.cfg, self.job, now=NOW)
        self.assertEqual(r.result, "failed")
        self.assertEqual((self.dest / f".restored/{DAY}/2025/img9.jpg").read_text(),
                         "data9" * 10)

    def test_a_destination_read_error_is_a_mismatch_not_a_skip(self):
        self.run_it()
        real = syncer._digest
        calls = {"n": 0}

        def flaky(fd, size):
            calls["n"] += 1
            if calls["n"] == 4:                    # a destination read of the 2nd file
                raise OSError(5, "Input/output error")
            return real(fd, size)

        with mock.patch.object(syncer, "_digest", flaky):
            v = verify_copy(self.cfg, self.job, NOW)
        self.assertEqual(v["state"], "mismatch")
        self.assertIn("could not be read back from iCloud", v["message"])

    def test_the_digest_is_bounded_and_notices_a_file_that_grew_or_shrank(self):
        path = self.tmp / "grow.bin"
        path.write_bytes(b"a" * 100)
        fd = os.open(path, os.O_RDONLY)
        try:
            with self.assertRaises(OSError):
                syncer._digest(fd, 60)             # grew past what was measured
            os.lseek(fd, 0, os.SEEK_SET)
            with self.assertRaises(OSError):
                syncer._digest(fd, 150)            # shorter than measured
            os.lseek(fd, 0, os.SEEK_SET)
            self.assertEqual(len(syncer._digest(fd, 100)), 64)
        finally:
            os.close(fd)

    def test_open_regular_at_cannot_climb_out_of_its_base(self):
        base = os.open(self.tmp / "sources", syncer.DIRFLAGS)
        try:
            for bad in ("../icloud/.keepsake-anchor", "/etc/hosts", "Photos//x", "./x", ""):
                with self.assertRaises(OSError, msg=bad):
                    syncer.open_regular_at(base, bad)
        finally:
            os.close(base)

    def test_a_kept_file_that_grows_during_restore_is_not_restored(self):
        kept = self.keep_one()
        real = os.read
        state = {"done": False}

        def growing(fd, n):
            data = real(fd, n)
            if data and not state["done"]:
                state["done"] = True
                with open(kept, "ab") as handle:
                    handle.write(b"more")
            return data

        with mock.patch.object(syncer.os, "read", growing):
            with self.assertRaises(Refused):
                restore_deleted(self.cfg, self.job, DAY, "2025/img9.jpg")
        self.assertEqual(list((self.dest / f".restored/{DAY}/2025").iterdir()), [])


class ListDeletedTests(Base):
    def test_lists_names_dates_sizes_and_days_left(self):
        self.keep_one()
        out = list_deleted(self.cfg, self.job, date(2026, 11, 17))
        self.assertEqual(out["entries"], [{"date": DAY, "path": "2025/img9.jpg",
                                           "size": 50, "days_left": 20}])

    def test_a_symlink_in_deleted_is_never_listed_or_followed(self):
        kept = self.keep_one()
        secret = self.tmp / "outside.txt"
        secret.write_text("private")
        (kept.parent / "link.txt").symlink_to(secret)
        (kept.parent / "linkdir").symlink_to(self.tmp)
        (self.dest / ".deleted/2026-11-08").symlink_to(self.tmp)   # a dated link
        out = list_deleted(self.cfg, self.job, date(2026, 11, 8))
        self.assertEqual([e["path"] for e in out["entries"]], ["2025/img9.jpg"])
        self.assertEqual(out["skipped_links"], 3)

    def test_things_not_named_like_a_date_are_ignored(self):
        self.keep_one()
        write(self.dest / ".deleted/notes/x.txt", "x")
        write(self.dest / ".deleted/2026-11-07-extra/x.txt", "x")
        out = list_deleted(self.cfg, self.job, date(2026, 11, 7))
        self.assertEqual(len(out["entries"]), 1)

    def test_the_list_is_capped(self):
        self.keep_one()
        with mock.patch.object(syncer, "DELETED_LIST_MAX", 0):
            out = list_deleted(self.cfg, self.job, date(2026, 11, 7))
        self.assertTrue(out["truncated"])

    def test_empty_when_nothing_was_ever_deleted(self):
        self.run_it()
        self.assertEqual(list_deleted(self.cfg, self.job)["entries"], [])

    def test_a_deleted_folder_that_is_a_symlink_is_refused(self):
        self.run_it()
        (self.dest / ".deleted").symlink_to(self.tmp)
        with self.assertRaises(Refused):
            list_deleted(self.cfg, self.job)

    def test_listing_writes_nothing(self):
        self.keep_one()
        before = snapshot(self.dest)
        list_deleted(self.cfg, self.job, date(2026, 11, 9))
        self.assertEqual(before, snapshot(self.dest))


class RestoreTests(Base):
    def restore(self, rel="2025/img9.jpg", day=DAY):
        return restore_deleted(self.cfg, self.job, day, rel)

    def test_restores_into_dot_restored_inside_the_icloud_folder(self):
        kept = self.keep_one()
        src_before = snapshot(self.src)
        msg = self.restore()
        out = self.dest / f".restored/{DAY}/2025/img9.jpg"
        self.assertEqual(out.read_text(), "data9" * 10)
        self.assertEqual(os.stat(out).st_mtime_ns, 1_700_000_000 * 10**9)
        self.assertTrue(kept.exists(), "the kept original stays")
        self.assertEqual(snapshot(self.src), src_before, "the source is never written")
        self.assertIn(".restored/2026-11-07/2025/img9.jpg", msg)
        self.assertIn("source was not touched", msg)
        self.assertFalse(list(self.dest.rglob(".icsync-*")), "no temp file is left")

    def test_nothing_is_ever_written_outside_the_icloud_job_folder(self):
        self.keep_one()
        outside = {k: v for k, v in snapshot(self.tmp).items()
                   if not k.startswith(str(self.dest))}
        self.restore()
        after = {k: v for k, v in snapshot(self.tmp).items()
                 if not k.startswith(str(self.dest))}
        self.assertEqual(outside, after)

    def test_a_name_collision_gets_a_number_and_never_replaces(self):
        self.keep_one()
        self.restore()
        first = self.dest / f".restored/{DAY}/2025/img9.jpg"
        first.write_text("someone edited the restored file")
        msg = self.restore()
        self.assertEqual(first.read_text(), "someone edited the restored file")
        self.assertEqual((self.dest / f".restored/{DAY}/2025/img9 (2).jpg").read_text(),
                         "data9" * 10)
        self.assertIn("img9 (2).jpg", msg)

    def test_path_traversal_is_refused(self):
        self.keep_one()
        write(self.dest / "2025/precious.txt", "keep me")
        for bad in ("../../2025/img0.jpg", "../.keepsake-owner", "/etc/passwd",
                    "2025/../../.keepsake-owner", "2025//img9.jpg", "./2025/img9.jpg",
                    "", None, 5, "2025/img9.jpg\x00x"):
            with self.assertRaises(Refused, msg=repr(bad)):
                self.restore(bad)
        for bad_day in ("..", "../..", "2026-11-07/../2026-11-06", "2026-13-45",
                        "20261107", None, ".deleted"):
            with self.assertRaises(Refused, msg=repr(bad_day)):
                self.restore(day=bad_day)
        self.assertFalse((self.dest / ".restored").exists())

    def test_a_symlink_to_a_file_in_deleted_is_refused(self):
        kept = self.keep_one()
        secret = self.tmp / "outside.txt"
        secret.write_text("private")
        (kept.parent / "evil.txt").symlink_to(secret)
        with self.assertRaises(Refused) as ctx:
            self.restore("2025/evil.txt")
        self.assertIn("link or not a plain file", str(ctx.exception))
        self.assertFalse((self.dest / ".restored").exists())

    def test_a_symlinked_folder_inside_deleted_is_refused(self):
        self.keep_one()
        (self.dest / f".deleted/{DAY}/linked").symlink_to(self.src)
        with self.assertRaises(Refused):
            self.restore("linked/2025/img0.jpg")
        (self.dest / f".deleted/{DAY}").rename(self.dest / ".deleted/2026-11-06")
        (self.dest / f".deleted/{DAY}").symlink_to(self.dest / ".deleted/2026-11-06")
        with self.assertRaises(Refused):
            self.restore()

    def test_a_fifo_in_deleted_is_refused_without_hanging(self):
        kept = self.keep_one()
        os.mkfifo(kept.parent / "pipe")
        with self.assertRaises(Refused):
            self.restore("2025/pipe")

    def test_a_restored_folder_that_is_a_symlink_is_refused(self):
        self.keep_one()
        target = self.tmp / "elsewhere"
        target.mkdir()
        (self.dest / ".restored").symlink_to(target)
        with self.assertRaises(Refused):
            self.restore()
        self.assertEqual(list(target.iterdir()), [])

    def test_a_restore_folder_made_of_a_link_part_way_is_refused(self):
        self.keep_one()
        target = self.tmp / "elsewhere"
        target.mkdir()
        (self.dest / ".restored").mkdir()
        (self.dest / f".restored/{DAY}").symlink_to(target)
        with self.assertRaises(Refused):
            self.restore()
        self.assertEqual(list(target.iterdir()), [])

    def test_a_huge_file_over_the_folder_cap_is_refused(self):
        self.keep_one()
        with mock.patch.object(syncer, "GB", 60):      # 60-byte "gigabyte"
            self.cfg = make_cfg(self.tmp, max_dest_gb=1)
            with self.assertRaises(Refused) as ctx:
                self.restore()
            self.assertIn("over its", str(ctx.exception))
        self.assertFalse((self.dest / ".restored").exists())

    def test_the_free_disk_reserve_is_kept(self):
        self.keep_one()
        self.cfg = make_cfg(self.tmp, min_free_gb=10 ** 9)
        with self.assertRaises(Refused) as ctx:
            self.restore()
        self.assertIn("reserve", str(ctx.exception))

    def test_a_missing_or_expired_file_is_a_plain_refusal(self):
        self.keep_one()
        with self.assertRaises(Refused) as ctx:
            self.restore("2025/nothing.jpg")
        self.assertIn("not in .deleted any more", str(ctx.exception))

    def test_refused_for_a_folder_that_is_not_ours(self):
        self.keep_one()
        (self.dest / ".keepsake-owner").unlink()
        with self.assertRaises(Refused):
            self.restore()

    def test_restored_files_are_not_mirrored_or_retired_by_the_next_run(self):
        self.keep_one()
        self.restore()
        r = self.run_it()
        self.assertEqual((r.files_added, r.files_updated, r.files_removed), (0, 0, 0))
        self.assertTrue((self.dest / f".restored/{DAY}/2025/img9.jpg").exists())
        self.assertFalse((self.dest / f".deleted/{DAY}/.restored").exists())

    def test_restored_files_count_towards_the_folder_size(self):
        self.keep_one()
        before = syncer.tree_bytes(self.dest)
        self.restore()
        self.assertEqual(syncer.tree_bytes(self.dest), before + 50)


class ChangeRecordTests(Base):
    def test_first_run_records_everything_as_added(self):
        r = self.run_it()
        self.assertEqual((r.files_added, r.files_updated, r.files_removed), (10, 0, 0))
        self.assertEqual(len(r.change_samples), 10)
        self.assertEqual({s["kind"] for s in r.change_samples}, {"added"})

    def test_added_updated_and_removed_are_counted_separately(self):
        self.run_it()
        write(self.src / "2025/img1.jpg", "changed-content", 1_700_000_500)
        write(self.src / "2025/new.jpg", "new", 1_700_000_500)
        (self.src / "2025/img9.jpg").unlink()
        r = self.run_it()
        self.assertEqual((r.files_added, r.files_updated, r.files_removed), (1, 1, 1))
        kinds = {s["path"]: s["kind"] for s in r.change_samples}
        self.assertEqual(kinds, {"2025/img1.jpg": "updated", "2025/new.jpg": "added",
                                 "2025/img9.jpg": "removed"})

    def test_a_plain_mirror_deletion_counts_as_removed(self):
        self.job = make_job(mode="mirror")
        self.run_it()
        (self.src / "2025/img9.jpg").unlink()
        r = self.run_it()
        self.assertEqual(r.files_removed, 1)

    def test_samples_stop_at_fifty_but_counts_do_not(self):
        for i in range(70):
            write(self.src / f"bulk/f{i}.txt", f"v{i}", 1_700_000_000)
        r = self.run_it()
        self.assertEqual(r.files_added, 80)
        self.assertEqual(len(r.change_samples), 50)

    def test_a_run_with_no_changes_records_zero(self):
        self.run_it()
        r = self.run_it()
        self.assertEqual((r.files_added, r.files_updated, r.files_removed,
                          r.change_samples), (0, 0, 0, []))

    def test_a_file_that_failed_to_copy_is_not_counted(self):
        real = syncer.stage_copy

        def flaky(ctx, rel, size, mtime_ns, throttle):
            if rel.endswith("img2.jpg"):
                raise OSError(5, "disk said no")
            return real(ctx, rel, size, mtime_ns, throttle)

        with mock.patch.object(syncer, "stage_copy", flaky):
            r = run_job(self.cfg, self.job, now=NOW)
        self.assertEqual(r.result, "failed")
        self.assertEqual(r.files_added, 9)
        self.assertNotIn("2025/img2.jpg", [s["path"] for s in r.change_samples])


if __name__ == "__main__":
    unittest.main()
