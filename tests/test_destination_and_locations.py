"""Several source folders into ONE destination folder, existing destinations, and the
editable list of locations Keepsake may read.

The rules live on the server, so every test here asks the server, never the page.
"""

import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from keepsake import engine, locations
from keepsake.config import ConfigError, Job, validate_job
from keepsake.engine import run_job
from keepsake.server import serve
from keepsake.service import Service
from tests.helpers import make_cfg, write
from tests.test_api import Client

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.hostfs = self.tmp / "hostfs"
        self.hostfs.mkdir()
        # A temp folder is writable, so the read-only requirement is off here;
        # LocationEnforcement turns it back on.
        self.cfg = replace(make_cfg(self.tmp), location_prefixes=(str(self.hostfs),),
                           location_require_ro=False)
        self.svc = Service(self.cfg, [])
        for i in range(6):
            write(self.tmp / f"sources/Archive/2019/Holidays/p{i}.jpg", f"a{i}", 1_700_000_000)
            write(self.tmp / f"sources/Archive/2020/Holidays/q{i}.jpg", f"b{i}", 1_700_000_000)
        write(self.tmp / "sources/Scans/receipt.pdf", "r", 1_700_000_000)

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, **kw):
        base = {"root": "sources", "auto_check": "off"}
        base.update(kw)
        return self.svc.add_job(base)

    @property
    def icloud(self):
        return self.tmp / "icloud"


class OneDestinationManySources(Base):
    def test_a_folder_in_a_destination_lives_in_its_own_subfolder_with_every_guard(self):
        self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="Backup")
        self.add(name="Holidays-2", source="Archive/2020/Holidays", dest="Holidays-2", group="Backup")
        for name in ("Holidays", "Holidays-2"):
            r = run_job(self.cfg, self.svc._find(name), now=NOW)
            self.assertEqual(r.result, "ok", r.error)
        one, two = self.icloud / "Backup/Holidays", self.icloud / "Backup/Holidays-2"
        self.assertEqual((one / "p3.jpg").read_text(), "a3")
        self.assertEqual((two / "q3.jpg").read_text(), "b3")
        # the destination is marked as Keepsake's, each folder keeps its own owner marker
        self.assertTrue((self.icloud / "Backup" / engine.GROUP_FILE).is_file())
        self.assertTrue((one / engine.OWNER_FILE).is_file() and (two / engine.OWNER_FILE).is_file())
        # nothing is copied into the destination itself or the source
        self.assertFalse((self.icloud / "Backup/p3.jpg").exists())
        self.assertEqual(sorted(p.name for p in (self.tmp / "sources/Archive/2019/Holidays").iterdir())[0], "p0.jpg")

    def test_the_thirty_day_deleted_folder_and_the_shrink_guard_work_inside_a_destination(self):
        self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="Backup")
        job = self.svc._find("Holidays")
        self.assertEqual(run_job(self.cfg, job, now=NOW).result, "ok")
        (self.tmp / "sources/Archive/2019/Holidays/p0.jpg").unlink()
        r = run_job(self.cfg, job, now=NOW)
        self.assertEqual(r.result, "ok", r.error)
        kept = self.icloud / "Backup/Holidays/.deleted/2026-11-07/p0.jpg"
        self.assertTrue(kept.is_file(), "a deleted file is kept for 30 days, inside the subfolder")
        for i in range(1, 5):
            (self.tmp / f"sources/Archive/2019/Holidays/p{i}.jpg").unlink()
        r = run_job(self.cfg, job, now=NOW)
        self.assertEqual(r.result, "failed")
        self.assertIn("shrank", r.error)

    def test_a_single_folder_into_a_new_destination_is_exactly_as_before(self):
        self.add(name="Scans", source="Scans")
        job = self.svc._find("Scans")
        self.assertEqual((job.group, job.dest, job.dest_path), ("", "Scans", "Scans"))
        self.assertEqual(run_job(self.cfg, job, now=NOW).result, "ok")
        self.assertTrue((self.icloud / "Scans/receipt.pdf").is_file())
        self.assertFalse((self.icloud / "Scans" / engine.GROUP_FILE).exists())

    def test_removing_a_job_moves_only_its_subfolder_to_removed(self):
        self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="Backup")
        self.add(name="Holidays-2", source="Archive/2020/Holidays", dest="Holidays-2", group="Backup")
        for name in ("Holidays", "Holidays-2"):
            run_job(self.cfg, self.svc._find(name), now=NOW)
        self.svc.remove_job("Holidays", delete_copy=True)
        self.assertFalse((self.icloud / "Backup/Holidays").exists())
        self.assertTrue((self.icloud / "Backup/Holidays-2/q3.jpg").is_file())
        moved = list((self.icloud / ".removed").iterdir())
        self.assertEqual([m.name.split("_")[0] for m in moved], ["Backup-Holidays"])

    def test_names_that_differ_only_by_case_are_the_same_name(self):
        self.add(name="Photos", source="Scans", dest="Photos")
        with self.assertRaises(ConfigError) as ctx:
            self.add(name="photos", source="Archive/2019/Holidays")
        self.assertIn("differ only by case", str(ctx.exception))
        with self.assertRaises(ConfigError) as ctx:
            self.add(name="Other", source="Archive/2019/Holidays", dest="PHOTOS")
        self.assertIn("already used", str(ctx.exception))
        self.add(name="Mixed", source="Archive/2020/Holidays", dest="Sub", group="Dest")
        with self.assertRaises(ConfigError):                     # a flat folder over a destination, another case
            self.add(name="Flat", source="Archive/2019/Holidays", dest="dest")
        with self.assertRaises(ConfigError):                     # and the other way round
            self.add(name="Under", source="Archive/2019/Holidays", dest="Sub2", group="photos")

    def test_camel_case_names_are_accepted_and_keep_their_case(self):
        self.add(name="PhotoLibrary", source="Scans", dest="PhotoLibrary")
        self.assertEqual(self.svc._find("PhotoLibrary").dest, "PhotoLibrary")
        for bad in ("Photo Library", "photo_library", "-Photo", "Photo/Library"):
            with self.assertRaises(ConfigError):
                validate_job({"name": bad, "root": "sources", "source": "Scans"}, self.cfg, [])


class DestinationFolders(Base):
    def kinds(self):
        return {f["name"]: f["kind"] for f in self.svc.destinations()["folders"]}

    def test_the_list_says_what_each_folder_is(self):
        self.add(name="Scans", source="Scans")
        self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="Backup")
        run_job(self.cfg, self.svc._find("Scans"), now=NOW)
        run_job(self.cfg, self.svc._find("Holidays"), now=NOW)
        (self.icloud / "FamilyStuff").mkdir()
        write(self.icloud / "FamilyStuff/readme.txt", "theirs")
        self.assertEqual(self.kinds(), {"Backup": "group", "FamilyStuff": "other", "Scans": "job"})

    def test_a_folder_a_job_will_make_is_listed_before_it_exists(self):
        self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="Backup")
        self.add(name="Scans", source="Scans")
        self.assertEqual(self.kinds(), {"Backup": "group", "Scans": "job"})
        self.assertFalse((self.icloud / "Backup").exists())

    def test_a_destination_keepsake_did_not_create_is_refused_until_the_person_says_so(self):
        (self.icloud / "FamilyStuff").mkdir()
        write(self.icloud / "FamilyStuff/readme.txt", "theirs")
        with self.assertRaises(ConfigError) as ctx:
            self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="FamilyStuff")
        self.assertIn("was not created by Keepsake", str(ctx.exception))
        self.assertEqual(self.svc.store.load(), [])
        # the engine refuses too, whatever was stored
        job = Job(name="Holidays", root="sources", source="Archive/2019/Holidays",
                  dest="Holidays", group="FamilyStuff")
        r = run_job(self.cfg, job, now=NOW)
        self.assertEqual(r.result, "failed")
        self.assertIn("was not created by Keepsake", r.error)
        self.assertEqual((self.icloud / "FamilyStuff/readme.txt").read_text(), "theirs")
        self.assertFalse((self.icloud / "FamilyStuff/Holidays").exists())
        # said out loud, it is allowed, and their file is left alone
        self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays",
                 group="FamilyStuff", adopt_existing=True)
        r = run_job(self.cfg, self.svc._find("Holidays"), now=NOW)
        self.assertEqual(r.result, "ok", r.error)
        self.assertEqual((self.icloud / "FamilyStuff/readme.txt").read_text(), "theirs")
        self.assertTrue((self.icloud / "FamilyStuff" / engine.GROUP_FILE).is_file())

    def test_a_folder_that_holds_one_jobs_files_cannot_also_hold_other_folders(self):
        self.add(name="Scans", source="Scans")
        run_job(self.cfg, self.svc._find("Scans"), now=NOW)
        with self.assertRaises(ConfigError) as ctx:
            self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="Scans")
        self.assertIn("holds the copy of job 'Scans' directly", str(ctx.exception))
        job = Job(name="Holidays", root="sources", source="Archive/2019/Holidays",
                  dest="Holidays", group="Scans", adopt_existing=True)
        r = run_job(self.cfg, job, now=NOW)
        self.assertEqual(r.result, "failed")
        self.assertIn("already holds one folder's copy directly", r.error)

    def test_a_second_batch_goes_into_an_existing_destination_without_a_tick(self):
        self.add(name="Holidays", source="Archive/2019/Holidays", dest="Holidays", group="Backup")
        run_job(self.cfg, self.svc._find("Holidays"), now=NOW)
        self.add(name="Holidays-2", source="Archive/2020/Holidays", dest="Holidays-2", group="Backup")
        r = run_job(self.cfg, self.svc._find("Holidays-2"), now=NOW)
        self.assertEqual(r.result, "ok", r.error)


class LocationEnforcement(Base):
    def make(self, *parts, files=True):
        folder = self.hostfs.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True)
        if files:
            write(folder / "x.txt", "x")
        return folder

    def add_loc(self, name, path):
        return self.svc.add_location({"name": name, "path": str(path)})

    def test_a_folder_inside_a_mounted_place_is_added_stored_and_survives_a_restart(self):
        home = self.make("Home")
        self.add_loc("HomeFolder", home)
        self.assertEqual(self.cfg.roots["HomeFolder"], home)
        again = Service(replace(make_cfg(self.tmp), location_prefixes=(str(self.hostfs),),
                                location_require_ro=False), [])
        self.assertEqual(again.cfg.roots["HomeFolder"], home)
        view = {loc["name"]: loc for loc in again.locations_view()["locations"]}
        self.assertFalse(view["HomeFolder"]["builtin"])
        self.assertTrue(view["sources"]["builtin"])

    def test_a_folder_outside_every_mounted_place_is_refused(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        with self.assertRaises(ConfigError) as ctx:
            self.add_loc("Elsewhere", outside)
        self.assertIn("outside the places Keepsake can read", str(ctx.exception))
        for sneaky in (f"{self.hostfs}/../elsewhere", f"{self.hostfs}/Home/../../elsewhere", "relative/path"):
            with self.assertRaises(ConfigError):
                self.add_loc("Sneaky", sneaky)
        self.assertNotIn("Elsewhere", self.cfg.roots)

    def test_a_symlink_that_escapes_is_refused_wherever_it_sits(self):
        outside = self.tmp / "secret-place"
        outside.mkdir()
        write(outside / "k.txt", "k")
        self.make("Home")
        os.symlink(outside, self.hostfs / "Home/Link")
        os.symlink(outside, self.hostfs / "TopLink")
        for target in (self.hostfs / "TopLink", self.hostfs / "Home/Link", self.hostfs / "Home/Link/k"):
            with self.assertRaises(ConfigError) as ctx:
                self.add_loc("Escape", target)
            self.assertIn("could not be opened as a plain folder", str(ctx.exception))
        listing = locations.browse(self.cfg, str(self.hostfs / "Home"))
        self.assertEqual([f["name"] for f in listing["folders"]], [], "a link is not even offered")

    def test_nothing_that_is_or_holds_or_sits_in_the_icloud_or_data_folder_is_added(self):
        cfg = replace(make_cfg(self.tmp, dest_root=self.hostfs / "iCloudDrive",
                               data_dir=self.hostfs / "Data"), location_prefixes=(str(self.hostfs),),
                      location_require_ro=False)
        cfg.dest_root.mkdir(parents=True, exist_ok=True)
        (cfg.dest_root / ".keepsake-anchor").write_text("{}")
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        svc = Service(cfg, [])
        (cfg.dest_root / "Inner").mkdir()
        for name, path in (("TheCopy", cfg.dest_root), ("TheWorld", self.hostfs),
                           ("Inside", cfg.dest_root / "Inner"), ("Settings", cfg.data_dir)):
            with self.assertRaises(ConfigError) as ctx:
                svc.add_location({"name": name, "path": str(path)})
            self.assertIn("never overlap where copies are written", str(ctx.exception), name)
        self.assertEqual(sorted(cfg.roots), ["sources"])

    def test_names_are_unique_whatever_their_case_and_locations_may_not_overlap(self):
        self.add_loc("HomeFolder", self.make("Home"))
        with self.assertRaises(ConfigError) as ctx:
            self.add_loc("homefolder", self.make("Other"))
        self.assertIn("differ only by case", str(ctx.exception))
        for bad in ("Home Folder", "", "-x", "a/b", "x" * 41):
            with self.assertRaises(ConfigError):
                self.add_loc(bad, self.make("Other"))
        with self.assertRaises(ConfigError) as ctx:
            self.add_loc("Docs", self.make("Home", "Documents"))        # inside an existing one
        self.assertIn("overlaps the location 'HomeFolder'", str(ctx.exception))
        with self.assertRaises(ConfigError):
            self.add_loc("Same", self.hostfs / "Home")                  # the very same folder

    def test_a_location_must_be_on_a_read_only_mount(self):
        strict = replace(self.cfg, location_require_ro=True)
        folder = self.make("Home")
        with self.assertRaises(ConfigError) as ctx:
            locations.check_new(strict, "HomeFolder", str(folder), strict.roots)
        self.assertIn("not on a read-only mount", str(ctx.exception))
        with mock.patch.object(locations, "read_only", return_value=True):
            name, path = locations.check_new(strict, "HomeFolder", str(folder), strict.roots)
        self.assertEqual((name, path), ("HomeFolder", str(folder)))

    def test_a_location_a_job_uses_cannot_be_removed_and_the_job_is_named(self):
        home = self.make("Home")
        self.make("Home", "Docs")
        self.add_loc("HomeFolder", home)
        self.svc.add_job({"name": "DocsCopy", "root": "HomeFolder", "source": "Docs", "auto_check": "off"})
        with self.assertRaises(ConfigError) as ctx:
            self.svc.remove_location("HomeFolder")
        self.assertIn("'DocsCopy'", str(ctx.exception))
        self.assertIn("HomeFolder", self.cfg.roots)
        self.svc.remove_job("DocsCopy")
        self.assertIn("removed", self.svc.remove_location("HomeFolder"))
        self.assertNotIn("HomeFolder", self.cfg.roots)
        self.assertEqual(locations.load_stored(self.cfg), {})

    def test_a_built_in_location_cannot_be_removed(self):
        with self.assertRaises(ConfigError) as ctx:
            self.svc.remove_location("sources")
        self.assertIn("built in", str(ctx.exception))
        self.assertIn("sources", self.cfg.roots)

    def test_browsing_lists_the_mounted_places_then_folders_and_never_outside(self):
        self.make("Home", "Documents")
        top = locations.browse(self.cfg, "")
        self.assertEqual([f["path"] for f in top["folders"]], [str(self.hostfs)])
        inside = locations.browse(self.cfg, str(self.hostfs / "Home"))
        self.assertEqual([f["name"] for f in inside["folders"]], ["Documents"])
        with self.assertRaises(ConfigError):
            locations.browse(self.cfg, str(self.tmp))
        with self.assertRaises(ConfigError):
            locations.browse(self.cfg, f"{self.hostfs}/Home/..")

    def test_nothing_mounted_says_so_in_words(self):
        bare = Service(replace(self.cfg, location_prefixes=("/no/such/mount",)), [])
        view = bare.locations_view()
        self.assertFalse(view["can_add"])
        self.assertIn("Add a read-only mount", view["why"])


class RestApi(Base):
    """The same rules, through the routes the page uses."""

    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, port=0, host="127.0.0.1")
        self.svc = Service(self.cfg, [])
        self.httpd = serve(self.svc)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.c = Client(self.httpd.server_address[1])
        self.c.open()
        (self.hostfs / "Home/Documents").mkdir(parents=True)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def test_locations_add_list_browse_and_remove(self):
        status, data, _ = self.c.call("GET", "/api/locations/browse?path=")
        self.assertEqual((status, [f["path"] for f in data["folders"]]), (200, [str(self.hostfs)]))
        status, data, _ = self.c.call("POST", "/api/locations", {"name": "HomeFolder", "path": str(self.hostfs / "Home")})
        self.assertEqual(status, 200, data)
        status, data, _ = self.c.call("GET", "/api/locations")
        self.assertEqual([loc["name"] for loc in data["locations"]], ["HomeFolder", "sources"])
        self.assertTrue(data["can_add"])
        status, data, _ = self.c.call("POST", "/api/locations/HomeFolder/remove", {})
        self.assertEqual(status, 200, data)
        status, data, _ = self.c.call("POST", "/api/locations/sources/remove", {})
        self.assertEqual(status, 400)
        self.assertIn("built in", data["message"])

    def test_every_refusal_comes_back_as_a_sentence_never_a_success(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        for body in ({"name": "Out", "path": str(outside)}, {"name": "Bad Name", "path": str(self.hostfs / "Home")},
                     {"name": "Up", "path": f"{self.hostfs}/Home/.."}):
            status, data, _ = self.c.call("POST", "/api/locations", body)
            self.assertEqual(status, 400, body)
            self.assertFalse(data["ok"])
            self.assertTrue(data["message"])
        status, _, _ = self.c.call("POST", "/api/locations", {"name": "Home", "path": str(self.hostfs / "Home")}, csrf=False)
        self.assertEqual(status, 403, "a change needs the page's CSRF token")

    def test_destinations_are_listed_for_the_page(self):
        self.svc.add_job({"name": "Holidays", "root": "sources", "source": "Archive/2019/Holidays",
                          "dest": "Holidays", "group": "Backup", "auto_check": "off"})
        status, data, _ = self.c.call("GET", "/api/destinations")
        self.assertEqual(status, 200)
        self.assertEqual([(f["name"], f["kind"], f["folders"]) for f in data["folders"]],
                         [("Backup", "group", ["Holidays"])])

    def test_a_job_with_a_destination_is_created_through_the_api(self):
        status, data, _ = self.c.call("POST", "/api/jobs", {
            "name": "Holidays", "root": "sources", "source": "Archive/2019/Holidays",
            "dest": "Holidays", "group": "Backup", "auto_check": "off"})
        self.assertEqual(status, 200, data)
        self.assertIn("Backup/Holidays", data["message"])
        status, data, _ = self.c.call("GET", "/api/status")
        job = data["jobs"][0]
        self.assertEqual((job["group"], job["dest"]), ("Backup", "Holidays"))


if __name__ == "__main__":
    unittest.main()
