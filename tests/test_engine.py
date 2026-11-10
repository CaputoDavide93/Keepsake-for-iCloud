import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from keepsake import engine as syncer
from keepsake.engine import run_job
from tests.helpers import make_cfg, make_job, write

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)


class SyncerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.src = self.tmp / "sources" / "Photos"
        self.dest = self.tmp / "icloud" / "photos"
        for i in range(10):
            write(self.src / f"2025/img{i}.jpg", f"data{i}" * 10, 1_700_000_000)

    def tearDown(self):
        self._tmp.cleanup()

    def run_it(self, job=None, **kw):
        return run_job(self.cfg, job or make_job(), now=NOW, **kw)


class CopyTests(SyncerBase):
    def test_first_run_copies_everything_and_verifies(self):
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual(r.files_copied, 10)
        self.assertEqual(r.verified_files, 10)
        self.assertGreater(r.sampled, 0)
        self.assertEqual((self.dest / "2025/img3.jpg").read_text(), "data3" * 10)
        self.assertEqual(os.stat(self.dest / "2025/img3.jpg").st_mtime_ns,
                         1_700_000_000 * 10**9)

    def test_second_run_copies_nothing(self):
        self.run_it()
        r = self.run_it()
        self.assertEqual((r.result, r.files_copied), ("ok", 0))

    def test_changed_file_is_updated_and_old_copy_kept(self):
        self.run_it()
        write(self.src / "2025/img1.jpg", "changed-content", 1_700_000_500)
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual(r.files_copied, 1)
        self.assertEqual((self.dest / "2025/img1.jpg").read_text(),
                         "changed-content")
        kept = self.dest / ".deleted/2026-11-07/2025/img1.jpg"
        self.assertEqual(kept.read_text(), "data1" * 10)

    def test_deleted_source_file_moves_to_dot_deleted(self):
        self.run_it()
        (self.src / "2025/img9.jpg").unlink()
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertFalse((self.dest / "2025/img9.jpg").exists())
        self.assertTrue((self.dest / ".deleted/2026-11-07/2025/img9.jpg").exists())

    def test_plain_mirror_deletes_for_real(self):
        job = make_job(mode="mirror")
        self.run_it(job)
        (self.src / "2025/img9.jpg").unlink()
        r = self.run_it(job)
        self.assertEqual(r.result, "ok", r.error)
        self.assertFalse((self.dest / ".deleted").exists())

    def test_source_is_never_modified(self):
        before = {p: (p.stat().st_mtime_ns, p.read_text())
                  for p in self.src.rglob("*") if p.is_file()}
        self.run_it()
        (self.src / "2025/img9.jpg").unlink()
        self.run_it()
        for p, (m, t) in before.items():
            if p.exists():
                self.assertEqual((p.stat().st_mtime_ns, p.read_text()), (m, t))

    def test_excludes(self):
        write(self.src / "cache/thumb.bin", "t")
        write(self.src / "2025/skip.tmp", "t")
        r = self.run_it(make_job(excludes=["cache", "*.tmp"]))
        self.assertEqual(r.result, "ok", r.error)
        self.assertFalse((self.dest / "cache").exists())
        self.assertFalse((self.dest / "2025/skip.tmp").exists())

    def test_leftover_temp_from_killed_run_is_removed(self):
        self.run_it()
        stale = write(self.dest / "2025/.icsync-0123456789abcdef.tmp", "half")
        r = self.run_it()
        self.assertEqual(r.result, "ok")
        self.assertFalse(stale.exists())

    def test_old_deleted_folders_are_pruned(self):
        self.run_it()
        old = write(self.dest / ".deleted/2026-09-01/a.txt")
        recent = write(self.dest / ".deleted/2026-11-01/b.txt")
        self.run_it(make_job(keep_days=30))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_secret_files_are_skipped_and_reported(self):
        write(self.src / "2025/.env", "KEY=1")
        write(self.src / "2025/id_rsa", "k")
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertFalse((self.dest / "2025/.env").exists())
        self.assertEqual(len(r.secrets_skipped), 2)

    def test_symlinks_are_not_followed(self):
        outside = write(self.tmp / "outside.txt", "secret")
        os.symlink(outside, self.src / "link.txt")
        os.symlink(self.tmp, self.src / "linkdir")
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertFalse((self.dest / "link.txt").exists())
        self.assertFalse((self.dest / "linkdir").exists())

    def test_only_its_own_folder_is_touched(self):
        other = write(self.tmp / "icloud/other-stuff/keep.txt", "mine")
        self.run_it()
        self.assertEqual(other.read_text(), "mine")
        top = {p.name for p in (self.tmp / "icloud").iterdir()}
        self.assertEqual(top, {"photos", "other-stuff", ".keepsake-anchor"})

    def test_changed_during_copy_is_a_failed_run_not_an_ok_one(self):
        """Finding 9: a file that changed mid-copy must not read as ok."""
        real_stage = syncer.stage_copy
        def racing(ctx, rel, size, mtime_ns, throttle):
            if rel.endswith("img2.jpg"):
                (ctx.src / rel).write_text("different-size-now!!")
            return real_stage(ctx, rel, size, mtime_ns, throttle)
        syncer.stage_copy = racing
        try:
            r = self.run_it()
        finally:
            syncer.stage_copy = real_stage
        self.assertEqual(r.changed_during_copy, 1)
        self.assertEqual(r.result, "failed")
        self.assertIn("changed while being read", r.error)
        self.assertFalse((self.dest / "2025/img2.jpg").exists())
        self.assertEqual([p for p in self.dest.rglob("*") if syncer.TMP_RE.match(p.name)], [])
        r2 = self.run_it()
        self.assertEqual(r2.result, "ok", r2.error)
        self.assertTrue((self.dest / "2025/img2.jpg").exists())

class GuardTests(SyncerBase):
    def test_source_not_mounted_is_skipped_and_nothing_deleted(self):
        self.run_it()
        cfg = make_cfg(self.tmp, roots={"sources": self.tmp / "no-such-mount"})
        r = run_job(cfg, make_job(), now=NOW)
        self.assertEqual(r.result, "skipped")
        self.assertIn("unavailable", r.error)
        self.assertTrue((self.dest / "2025/img3.jpg").exists())

    def test_icloud_not_mounted_is_skipped(self):
        cfg = make_cfg(self.tmp, dest_root=self.tmp / "no-icloud")
        r = run_job(cfg, make_job(), now=NOW)
        self.assertEqual(r.result, "skipped")

    def test_empty_source_refused_and_copy_kept(self):
        self.run_it()
        for p in self.src.rglob("*.jpg"):
            p.unlink()
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertIn("empty", r.error)
        self.assertEqual(len(list(self.dest.rglob("*.jpg"))), 10)

    def test_shrink_guard_refuses_then_accepts_when_told(self):
        self.run_it()
        for i in range(5):
            (self.src / f"2025/img{i}.jpg").unlink()
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertIn("shrank", r.error)
        self.assertEqual(len(list(self.dest.rglob("*.jpg"))), 10)
        r = self.run_it(accept_shrink=True)
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual(len(list((self.dest / "2025").glob("*.jpg"))), 5)

    def test_small_shrink_within_limit_is_fine(self):
        self.run_it()
        (self.src / "2025/img0.jpg").unlink()  # 10% < 20%
        self.assertEqual(self.run_it().result, "ok")

    def test_missing_source_folder_refused(self):
        r = self.run_it(make_job(source="Nope"))
        self.assertEqual(r.result, "failed")

    def test_traversal_through_symlinked_source_refused(self):
        os.symlink(self.tmp, self.tmp / "sources" / "Escape")
        r = self.run_it(make_job(source="Escape"))
        self.assertEqual(r.result, "failed")
        self.assertIn("outside", r.error)

    def test_size_cap_refuses_before_copying(self):
        cfg = make_cfg(self.tmp)
        job = make_job(max_gb=1)
        orig = syncer.GB
        syncer.GB = 100  # a 1 "GB" cap of 100 bytes against ~500 bytes of data
        try:
            r = run_job(cfg, job, now=NOW)
        finally:
            syncer.GB = orig
        self.assertEqual(r.result, "failed")
        self.assertIn("limit", r.error)
        self.assertEqual(list(self.dest.rglob("*.jpg")), [])

    def test_free_disk_reserve_refuses(self):
        cfg = make_cfg(self.tmp, min_free_gb=10**6)
        r = run_job(cfg, make_job(), now=NOW)
        self.assertEqual(r.result, "failed")
        self.assertIn("reserve", r.error)
        self.assertEqual(list(self.dest.rglob("*.jpg")), [])


class PlaceholderTests(SyncerBase):
    def evict(self, rel):
        (self.dest / rel).unlink()
        ph = self.dest / syncer.placeholder_for(rel)
        write(ph, "placeholder")

    def test_evicted_file_is_not_recopied(self):
        self.run_it()
        self.evict("2025/img4.jpg")
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual(r.files_copied, 0)
        self.assertEqual(r.placeholders_skipped, 1)
        self.assertFalse((self.dest / "2025/img4.jpg").exists())

    def test_evicted_file_changed_at_source_is_recopied(self):
        self.run_it()
        self.evict("2025/img4.jpg")
        write(self.src / "2025/img4.jpg", "new-version-longer", 1_700_001_000)
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual(r.files_copied, 1)
        self.assertFalse((self.dest / ".2025/.img4.jpg.icloud").exists())
        self.assertEqual((self.dest / "2025/img4.jpg").read_text(),
                         "new-version-longer")

    def test_evicted_file_deleted_at_source_retires_the_placeholder(self):
        self.run_it()
        self.evict("2025/img4.jpg")
        (self.src / "2025/img4.jpg").unlink()
        self.assertEqual(self.run_it().result, "ok")
        self.assertFalse((self.dest / "2025/.img4.jpg.icloud").exists())


class HelperTests(unittest.TestCase):
    def test_placeholder_names(self):
        self.assertEqual(syncer.placeholder_for("a/b/p.jpg"), "a/b/.p.jpg.icloud")
        self.assertEqual(syncer.placeholder_for("p.jpg"), ".p.jpg.icloud")

    def test_excludes_match_dirs_and_basenames(self):
        self.assertTrue(syncer.is_excluded("a/cache", ["cache"]))
        self.assertTrue(syncer.is_excluded("a/b.tmp", ["*.tmp"]))
        self.assertFalse(syncer.is_excluded("a/b.txt", ["*.tmp"]))

    def test_retire_job_folder_soft_deletes(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = make_cfg(tmp)
            write(cfg.dest_root / "photos/a.txt", "a")
            write(cfg.dest_root / "photos" / syncer.OWNER_FILE, '{"job": "photos"}')
            msg = syncer.retire_job_folder(cfg, make_job(), NOW)
            self.assertIn("30 days", msg)
            self.assertTrue((cfg.dest_root / ".removed/photos_2026-11-07/a.txt")
                            .exists())


class AttackTests(SyncerBase):
    """One fixture per Codex finding on #1568: each attack is refused or kept safe."""

    def victim(self, name="victim"):
        v = self.tmp / name
        v.mkdir()
        write(v / "precious.txt", "do not touch")
        return v

    # 1. reserved folders that are symlinks
    def test_deleted_symlink_is_refused_and_nothing_moves_outside(self):
        self.run_it()
        v = self.victim()
        write(self.dest / "2025/img1.jpg", "stale-copy", 1_600_000_000)  # force a replace
        os.symlink(v, self.dest / ".deleted")
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertIn("symlink", r.error)
        self.assertEqual(sorted(p.name for p in v.iterdir()), ["precious.txt"])

    def test_prune_never_follows_a_symlinked_parent_or_child(self):
        v = self.victim()
        old = write(v / "2020-01-01" / "f.txt", "x")
        os.symlink(v, self.tmp / "linked-parent")
        self.assertEqual(syncer.prune_deleted(self.tmp / "linked-parent", 30, NOW.date()), 0)
        self.assertTrue(old.exists())
        real = self.tmp / "real-parent"
        real.mkdir()
        os.symlink(v / "2020-01-01", real / "2020-01-01")
        self.assertEqual(syncer.prune_deleted(real, 30, NOW.date()), 0)
        self.assertTrue(old.exists())

    def test_removed_symlink_is_refused_when_soft_deleting(self):
        v = self.victim()
        os.symlink(v, self.tmp / "icloud/.removed")
        write(self.tmp / "icloud/photos/a.txt", "a")
        write(self.tmp / "icloud/photos" / syncer.OWNER_FILE, '{"job": "photos"}')
        msg = syncer.retire_job_folder(self.cfg, make_job(), NOW)
        self.assertIn("not a real folder", msg)
        self.assertTrue((self.tmp / "icloud/photos/a.txt").exists())
        self.assertEqual(sorted(p.name for p in v.iterdir()), ["precious.txt"])

    def test_a_symlinked_subfolder_in_the_destination_is_not_written_through(self):
        self.run_it()
        v = self.victim()
        for p in (self.dest / "2025").glob("*"):
            p.unlink()
        (self.dest / "2025").rmdir()
        os.symlink(v, self.dest / "2025")
        r = self.run_it()
        self.assertEqual(sorted(p.name for p in v.iterdir()), ["precious.txt"])
        self.assertNotEqual(r.result, "ok")

    # 2. temp-file symlinks
    def test_planted_temp_symlink_never_truncates_its_target(self):
        self.run_it()
        v = self.victim()
        target = v / "precious.txt"
        os.symlink(target, self.dest / "2025/img1.jpg.icsync.tmp")
        os.symlink(target, self.dest / "2025/.icsync-0123456789abcdef.tmp")
        write(self.src / "2025/img1.jpg", "changed-content", 1_700_000_500)
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual(target.read_text(), "do not touch")

    def test_temp_file_is_created_exclusively_even_if_the_name_is_planted(self):
        v = self.victim()
        planted = self.dest / "2025"
        planted.mkdir(parents=True)
        fixed = ".icsync-aaaaaaaaaaaaaaaa.tmp"
        os.symlink(v / "precious.txt", planted / fixed)
        orig = syncer.new_tmp_name
        syncer.new_tmp_name = lambda: fixed
        job_fd = os.open(self.dest, syncer.DIRFLAGS)
        src_fd = os.open(self.src, syncer.DIRFLAGS)
        st = (self.src / "2025/img1.jpg").stat()
        try:
            ctx = syncer.Ctx(src_fd=src_fd, job_fd=job_fd, job_dir=self.dest, src=self.src)
            with self.assertRaises(FileExistsError):
                syncer.stage_copy(ctx, "2025/img1.jpg", st.st_size, st.st_mtime_ns,
                                  syncer.Throttle(self.cfg))
        finally:
            syncer.new_tmp_name = orig
            os.close(job_fd)
            os.close(src_fd)
        self.assertEqual((v / "precious.txt").read_text(), "do not touch")

    def test_a_symlink_at_the_destination_path_is_replaced_not_followed(self):
        self.run_it()
        v = self.victim()
        (self.dest / "2025/img3.jpg").unlink()
        os.symlink(v / "precious.txt", self.dest / "2025/img3.jpg")
        write(self.src / "2025/img3.jpg", "new-content-longer", 1_700_000_900)
        self.run_it()
        self.assertEqual((v / "precious.txt").read_text(), "do not touch")

    # round 2: symlink races, a symlinked destination root, owner on remove
    def test_a_symlinked_icloud_root_is_refused(self):
        v = self.victim()
        link = self.tmp / "icloud-link"
        os.symlink(v, link)
        cfg = make_cfg(self.tmp, dest_root=link)
        r = run_job(cfg, make_job(), now=NOW)
        self.assertNotEqual(r.result, "ok")
        self.assertEqual(sorted(p.name for p in v.iterdir()), ["precious.txt"])

    def test_a_parent_swapped_for_a_symlink_after_staging_is_not_followed(self):
        self.run_it()
        v = self.victim()
        write(self.src / "2025/img1.jpg", "changed-content", 1_700_000_500)
        real_stage = syncer.stage_copy
        def swap(ctx, rel, size, mtime_ns, throttle):
            out = real_stage(ctx, rel, size, mtime_ns, throttle)
            (self.dest / "2025").rename(self.dest / "2025-moved")
            os.symlink(v, self.dest / "2025")
            return out
        syncer.stage_copy = swap
        try:
            self.run_it()
        finally:
            syncer.stage_copy = real_stage
        self.assertEqual(sorted(p.name for p in v.iterdir()), ["precious.txt"])

    def test_a_source_folder_swapped_for_a_symlink_is_not_copied_from(self):
        out = self.victim("outside-src")
        write(out / "img1.jpg", "SECRET-FROM-OUTSIDE" )
        real_stage = syncer.stage_copy
        def swap(ctx, rel, size, mtime_ns, throttle):
            if rel == "2025/img1.jpg":
                (self.src / "2025").rename(self.src / "2025-moved")
                os.symlink(out, self.src / "2025")
            return real_stage(ctx, rel, size, mtime_ns, throttle)
        syncer.stage_copy = swap
        try:
            r = self.run_it()
        finally:
            syncer.stage_copy = real_stage
        self.assertNotEqual(r.result, "ok")
        for p in self.dest.rglob("*"):
            if p.is_file():
                self.assertNotIn("SECRET", p.read_text())

    def test_plain_mirror_keeps_the_placeholder_when_the_commit_fails(self):
        job = make_job(mode="mirror")
        self.run_it(job)
        self.evict("2025/img4.jpg")
        write(self.src / "2025/img4.jpg", "new-version-longer", 1_700_001_000)
        real_replace = os.replace
        def fail_commit(src, dst, *a, **k):
            if str(src).startswith(".icsync-") and dst != syncer.OWNER_FILE:
                raise OSError(20, "Not a directory")
            return real_replace(src, dst, *a, **k)
        syncer.os.replace = fail_commit
        try:
            r = self.run_it(job)
        finally:
            syncer.os.replace = real_replace
        self.assertEqual(r.result, "failed")
        self.assertTrue((self.dest / "2025/.img4.jpg.icloud").exists())

    def test_delete_copy_refuses_a_folder_that_is_not_marked_as_this_job(self):
        write(self.tmp / "icloud/photos/theirs.txt", "theirs")
        msg = syncer.retire_job_folder(self.cfg, make_job(), NOW)
        self.assertIn("not marked", msg)
        self.assertTrue((self.tmp / "icloud/photos/theirs.txt").exists())
        write(self.tmp / "icloud/photos" / syncer.OWNER_FILE, '{"job": "other"}')
        self.assertIn("not marked", syncer.retire_job_folder(self.cfg, make_job(), NOW))

    def test_an_unowned_folder_is_not_cleaned_before_adoption_is_refused(self):
        keep = write(self.dest / ".icsync-0123456789abcdef.tmp", "someone's file")
        write(self.dest / "theirs.txt", "t")
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertTrue(keep.exists())

    def test_a_mount_that_vanishes_before_the_first_write_changes_nothing(self):
        self.run_it()
        write(self.src / "2025/img1.jpg", "changed-content", 1_700_000_500)
        calls = []
        real = syncer.root_live
        def flaky(cfg, name):
            calls.append(1)
            return real(cfg, name) if len(calls) == 1 else (False, "went away")
        syncer.root_live = flaky
        try:
            r = self.run_it()
        finally:
            syncer.root_live = real
        self.assertEqual(r.result, "skipped")
        self.assertEqual((self.dest / "2025/img1.jpg").read_text(), "data1" * 10)

    def test_a_source_path_with_a_symlink_on_the_way_is_refused(self):
        outside = self.victim("outside-tree")
        write(outside / "sub/f.txt", "secret")
        os.symlink(outside, self.tmp / "sources" / "Linked")
        r = self.run_it(make_job(name="linked", source="Linked/sub", dest="linked"))
        self.assertNotEqual(r.result, "ok")
        self.assertFalse((self.tmp / "icloud/linked/f.txt").exists())

    def test_delete_copy_does_not_move_a_folder_swapped_after_the_owner_check(self):
        write(self.tmp / "icloud/photos/a.txt", "a")
        write(self.tmp / "icloud/photos" / syncer.OWNER_FILE, '{"job": "photos"}')
        real = syncer._read_owner
        def swap_after(fd):
            out = real(fd)
            (self.tmp / "icloud/photos").rename(self.tmp / "icloud/photos-orig")
            (self.tmp / "icloud/photos").mkdir()
            write(self.tmp / "icloud/photos/unrelated.txt", "u")
            return out
        syncer._read_owner = swap_after
        try:
            msg = syncer.retire_job_folder(self.cfg, make_job(), NOW)
        finally:
            syncer._read_owner = real
        self.assertIn("changed while", msg)
        self.assertTrue((self.tmp / "icloud/photos/unrelated.txt").exists())

    # 3. a leftover mount directory
    def test_an_empty_mount_directory_is_unavailable(self):
        self.run_it()
        empty = self.tmp / "emptyshare"
        empty.mkdir()
        cfg = make_cfg(self.tmp, roots={"sources": empty})
        r = run_job(cfg, make_job(), now=NOW)
        self.assertEqual(r.result, "skipped")
        self.assertIn("empty", r.error)
        self.assertTrue((self.dest / "2025/img3.jpg").exists())

    def test_a_plain_folder_where_a_share_should_be_is_unavailable(self):
        cfg = make_cfg(self.tmp, fs_roots=frozenset({"sources"}))  # tmp dir inode is far above 2
        r = run_job(cfg, make_job(), now=NOW)
        self.assertEqual(r.result, "skipped")
        self.assertIn("not the mounted share", r.error)

    def test_a_root_without_its_marker_is_unavailable(self):
        cfg = make_cfg(self.tmp, root_markers={"sources": ".keepsake-root"})
        self.assertEqual(run_job(cfg, make_job(), now=NOW).result, "skipped")
        write(self.tmp / "sources/.keepsake-root", "")
        self.assertEqual(run_job(cfg, make_job(), now=NOW).result, "ok")

    def test_a_mount_that_vanishes_mid_run_retires_nothing(self):
        self.run_it()
        (self.src / "2025/img9.jpg").unlink()
        real_stage = syncer.stage_copy
        write(self.src / "2025/new.jpg", "n")
        def vanish(src, dst, size, mtime_ns, throttle):
            out = real_stage(src, dst, size, mtime_ns, throttle)
            (self.tmp / "sources").rename(self.tmp / "sources-gone")
            return out
        syncer.stage_copy = vanish
        try:
            r = self.run_it()
        finally:
            syncer.stage_copy = real_stage
        self.assertEqual(r.result, "skipped")
        self.assertTrue((self.dest / "2025/img9.jpg").exists())

    # 4. shrink by bytes and by paths
    def test_shrink_by_bytes_with_the_same_file_count_is_refused(self):
        write(self.src / "big.bin", "x" * 5000, 1_700_000_000)
        self.run_it()
        write(self.src / "big.bin", "x", 1_700_000_100)
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertIn("GB", r.error)
        self.assertEqual((self.dest / "big.bin").stat().st_size, 5000)

    def test_a_swapped_tree_with_the_same_counts_is_refused_by_path_overlap(self):
        self.run_it()
        for i in range(10):
            (self.src / f"2025/img{i}.jpg").rename(self.src / f"2025/other{i}.jpg")
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertIn("remembered files", r.error)

    def test_accept_shrink_must_be_the_boolean_true(self):
        self.run_it()
        for i in range(5):
            (self.src / f"2025/img{i}.jpg").unlink()
        self.assertEqual(run_job(self.cfg, make_job(), accept_shrink="false", now=NOW).result, "failed")
        self.assertEqual(run_job(self.cfg, make_job(), accept_shrink=1, now=NOW).result, "failed")
        self.assertEqual(run_job(self.cfg, make_job(), accept_shrink=True, now=NOW).result, "ok")

    def test_unreadable_source_folders_refuse_even_the_first_run(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        locked = self.src / "locked"
        write(locked / "a.txt", "a")
        os.chmod(locked, 0)
        try:
            r = self.run_it()
        finally:
            os.chmod(locked, 0o755)
        self.assertEqual(r.result, "failed")
        self.assertIn("could not be read", r.error)

    # 5. a lost manifest, or a destination that is not ours
    def test_lost_manifest_over_a_populated_destination_retires_nothing(self):
        self.run_it()
        (self.cfg.manifest_dir / "photos.json").unlink()
        (self.src / "2025/img9.jpg").unlink()
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertTrue(r.safe_first_run)
        self.assertEqual(r.left_alone, 1)
        self.assertTrue((self.dest / "2025/img9.jpg").exists())
        self.assertFalse((self.dest / ".deleted").exists())
        # the baseline is back, so the next run retires normally
        r2 = self.run_it()
        self.assertFalse(r2.safe_first_run)
        self.assertFalse((self.dest / "2025/img9.jpg").exists())
        self.assertTrue((self.dest / ".deleted/2026-11-07/2025/img9.jpg").exists())

    def test_an_unmarked_populated_destination_is_refused_unless_adopted(self):
        write(self.dest / "someone-elses.txt", "theirs")
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertIn("adopt_existing", r.error)
        self.assertEqual((self.dest / "someone-elses.txt").read_text(), "theirs")
        self.assertFalse((self.dest / "2025").exists())
        r = self.run_it(make_job(adopt_existing=True))
        self.assertEqual(r.result, "ok", r.error)
        self.assertTrue((self.dest / "someone-elses.txt").exists())  # left alone
        self.assertTrue((self.dest / syncer.OWNER_FILE).exists())

    def test_an_unowned_folder_holding_only_a_deleted_tree_is_not_adopted_or_pruned(self):
        old = write(self.dest / ".deleted/client-2020-01-01/f.txt", "theirs")
        r = self.run_it()
        self.assertEqual(r.result, "failed")
        self.assertIn("adopt_existing", r.error)
        self.assertTrue(old.exists())

    def test_a_destination_owned_by_another_job_is_refused(self):
        self.run_it()
        r = run_job(self.cfg, make_job(name="other", dest="photos"), now=NOW)
        self.assertEqual(r.result, "failed")
        self.assertIn("belongs to job", r.error)

    def test_an_existing_empty_destination_needs_no_adoption(self):
        self.dest.mkdir(parents=True)
        self.assertEqual(self.run_it().result, "ok")

    # 6 and 7. placeholders
    def evict(self, rel):
        (self.dest / rel).unlink()
        write(self.dest / syncer.placeholder_for(rel), "placeholder")

    def test_a_failed_copy_over_a_placeholder_keeps_the_placeholder(self):
        self.run_it()
        self.evict("2025/img4.jpg")
        write(self.src / "2025/img4.jpg", "new-version-longer", 1_700_001_000)
        real_stage = syncer.stage_copy
        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        syncer.stage_copy = boom
        try:
            r = self.run_it()
        finally:
            syncer.stage_copy = real_stage
        self.assertEqual(r.result, "failed")
        self.assertTrue((self.dest / "2025/.img4.jpg.icloud").exists())
        self.assertFalse((self.dest / ".deleted").exists())

    def test_a_failed_copy_over_a_real_file_keeps_the_old_version(self):
        self.run_it()
        write(self.src / "2025/img4.jpg", "new-version-longer", 1_700_001_000)
        real_stage = syncer.stage_copy
        def boom(*a, **k):
            raise OSError(5, "Input/output error")
        syncer.stage_copy = boom
        try:
            self.run_it()
        finally:
            syncer.stage_copy = real_stage
        self.assertEqual((self.dest / "2025/img4.jpg").read_text(), "data4" * 10)

    def test_a_replaced_placeholder_is_kept_in_deleted_not_unlinked(self):
        self.run_it()
        self.evict("2025/img4.jpg")
        write(self.src / "2025/img4.jpg", "new-version-longer", 1_700_001_000)
        self.assertEqual(self.run_it().result, "ok")
        self.assertTrue((self.dest / ".deleted/2026-11-07/2025/.img4.jpg.icloud").exists())

    def test_a_removed_placeholder_is_kept_in_deleted_in_keep_mode(self):
        self.run_it()
        self.evict("2025/img4.jpg")
        (self.src / "2025/img4.jpg").unlink()
        self.assertEqual(self.run_it().result, "ok")
        self.assertTrue((self.dest / ".deleted/2026-11-07/2025/.img4.jpg.icloud").exists())

    # 10. unreadable destination entries
    def test_an_unreadable_destination_folder_refuses_the_run(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        self.run_it()
        write(self.src / "2025/img0.jpg", "changed", 1_700_000_999)
        os.chmod(self.dest / "2025", 0)
        try:
            r = self.run_it()
        finally:
            os.chmod(self.dest / "2025", 0o755)
        self.assertEqual(r.result, "failed")
        self.assertIn("could not be read", r.error)

    # 11. legitimate names
    def test_a_source_file_named_like_an_old_temp_suffix_is_preserved(self):
        write(self.src / "2025/notes.icsync.tmp", "mine", 1_700_000_000)
        r = self.run_it()
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual((self.dest / "2025/notes.icsync.tmp").read_text(), "mine")
        self.assertEqual(self.run_it().result, "ok")
        self.assertTrue((self.dest / "2025/notes.icsync.tmp").exists())

    # 12. unchanged files are sampled too
    def test_a_corrupted_unchanged_destination_file_is_caught_by_the_sample(self):
        self.run_it()
        f = self.dest / "2025/img5.jpg"
        st = f.stat()
        f.write_text("X" * st.st_size)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        old = syncer.SAMPLE
        syncer.SAMPLE = 100
        try:
            r = self.run_it()
        finally:
            syncer.SAMPLE = old
        self.assertEqual(r.result, "failed")
        self.assertIn("content check failed", r.error)


class AnchorTests(SyncerBase):
    """The one-time iCloud anchor (Codex round 4, finding 3)."""

    anchor = property(lambda self: self.tmp / "icloud" / syncer.ANCHOR_FILE)

    def test_a_missing_anchor_refuses_with_a_plain_sentence_and_writes_nothing(self):
        self.anchor.unlink()
        r = self.run_it()
        self.assertEqual(r.result, "skipped")
        self.assertIn("has not been confirmed", r.error)
        self.assertIn("Nothing was copied", r.error)
        self.assertEqual([p.name for p in (self.tmp / "icloud").iterdir()], [])

    def test_the_anchor_present_lets_the_job_run(self):
        self.assertEqual(self.run_it().result, "ok")

    def test_an_anchor_that_is_only_a_placeholder_is_refused(self):
        self.anchor.unlink()
        write(self.tmp / "icloud" / f".{syncer.ANCHOR_FILE}.icloud", "placeholder")
        r = self.run_it()
        self.assertEqual(r.result, "skipped")
        self.assertIn("placeholder", r.error)

    def test_an_anchor_that_is_a_symlink_or_folder_is_refused(self):
        self.anchor.unlink()
        self.anchor.mkdir()
        self.assertEqual(self.run_it().result, "skipped")

    def test_init_creates_the_anchor_once_and_refuses_a_placeholder(self):
        self.anchor.unlink()
        msg = syncer.write_anchor(self.tmp / "icloud", NOW)
        self.assertIn("confirmed", msg)
        self.assertTrue(self.anchor.is_file())
        self.assertIn("already", syncer.write_anchor(self.tmp / "icloud", NOW))
        self.anchor.unlink()
        write(self.tmp / "icloud" / f".{syncer.ANCHOR_FILE}.icloud", "p")
        with self.assertRaises(syncer.Refused):
            syncer.write_anchor(self.tmp / "icloud", NOW)
        self.assertFalse(self.anchor.exists())
