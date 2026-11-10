"""The guided setup is a lock: until it is done the page shows only the wizard and
the REST API answers only what the wizard needs. It is read from what is really
there (like the first-run checklist), it is complete for an install that already
works, and once complete it never comes back."""

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from keepsake.server import SETUP_REFUSAL, serve, setup_allows
from keepsake.service import Service
from tests.helpers import make_cfg, write
from tests.test_api import Client


def fresh_tree(tmp: Path) -> None:
    for i in range(3):
        write(tmp / f"sources/Photos/2025/{i}.jpg", f"d{i}", 1_700_000_000)


class Wizard(unittest.TestCase):
    """A service whose setup was never recorded, as on a brand-new install."""

    anchor = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp, setup=True)
        if not self.anchor:
            (self.cfg.dest_root / ".keepsake-anchor").unlink()
        fresh_tree(self.tmp)
        self.svc = Service(self.cfg, [])

    def tearDown(self):
        self._tmp.cleanup()

    def setup(self):
        return self.svc.status()["setup"]

    def add(self, name="photos"):
        return self.svc.add_job({"name": name, "root": "sources", "source": "Photos"})


class SetupSteps(Wizard):
    def test_a_new_install_is_locked_at_the_first_step(self):
        self.assertEqual(self.setup(), {"complete": False, "step": "confirm", "existing": False})

    def test_the_step_follows_what_is_really_there(self):
        self.assertEqual(self.setup()["step"], "confirm")
        self.svc.confirm_icloud()
        self.assertEqual(self.setup()["step"], "folder")
        self.add()
        self.assertEqual(self.setup()["step"], "preview")
        self.svc.preview("photos")
        self.svc.run_next()
        self.assertEqual(self.setup()["step"], "sync-check")
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.assertEqual(self.setup(), {"complete": False, "step": "sync-check", "existing": False},
                         "a copy alone is not a check")
        self.svc.verify_job("photos")
        self.assertEqual(self.setup(), {"complete": True, "step": "complete", "existing": False})

    def test_the_first_copy_waits_for_the_person_while_the_setup_is_open(self):
        self.svc.confirm_icloud()
        self.add()
        self.assertEqual(self.svc.due_jobs(), [], "a scheduled run must not beat the preview")
        self.assertFalse(self.svc.run_next())
        self.assertFalse((self.tmp / "icloud/photos").exists())
        self.svc.sync_now("photos")
        self.assertTrue(self.svc.run_next())
        self.assertTrue((self.tmp / "icloud/photos/2025/0.jpg").exists())

    def test_the_add_message_does_not_promise_a_copy_the_setup_holds_back(self):
        self.svc.confirm_icloud()
        self.assertIn("when you ask for it in the setup", self.add())

    def test_a_folder_that_never_copied_is_not_a_step_forward_for_the_preview_when_paused(self):
        self.svc.confirm_icloud()
        self.svc.add_job({"name": "photos", "root": "sources", "source": "Photos",
                          "enabled": False})
        self.assertEqual(self.setup()["step"], "sync-check",
                         "a paused folder is not waiting for a preview")

    def test_no_setting_can_finish_it(self):
        for key in ("setup", "setup_complete", "complete", "first_run_done"):
            self.svc.update_settings({key: True})
        self.assertEqual(self.setup()["complete"], False)


class ExistingInstall(Wizard):
    """An install made before the wizard: a confirmed folder and a job on disk,
    and no setup.json, when the service starts."""

    anchor = True

    def setUp(self):
        super().setUp()
        self.add()
        self.cfg.setup_file.unlink()
        self.svc = Service(self.cfg, [])            # the restart that brings the wizard

    def test_an_install_that_already_works_is_complete_and_never_locked(self):
        self.assertEqual(self.setup(), {"complete": True, "step": "complete", "existing": True})
        self.assertTrue(self.svc.setup_complete())

    def test_an_existing_install_is_not_held_back_by_the_first_copy_rule(self):
        self.assertEqual(self.svc.due_jobs(), ["photos"])

    def test_an_existing_install_without_a_confirmed_folder_is_still_set_up_by_the_wizard(self):
        self.cfg.setup_file.unlink()
        (self.cfg.dest_root / ".keepsake-anchor").unlink()
        self.assertEqual(Service(self.cfg, []).status()["setup"]["step"], "confirm")


class PartWayThrough(Wizard):
    anchor = True

    def test_the_same_state_judged_later_is_a_person_part_way_through(self):
        self.assertEqual(self.setup()["step"], "folder")            # judged at start-up: started
        self.add()
        self.assertEqual(self.setup(), {"complete": False, "step": "preview", "existing": False})


class NeverComesBack(Wizard):
    anchor = True

    def finish(self):
        self.add()
        self.svc.sync_now("photos")
        self.svc.run_next()
        self.svc.verify_job("photos")
        self.assertTrue(self.setup()["complete"])

    def test_removing_every_folder_does_not_lock_the_page_again(self):
        self.finish()
        self.svc.remove_job("photos")
        self.assertEqual(self.setup(), {"complete": True, "step": "complete", "existing": False})

    def test_it_survives_a_restart_and_a_missing_icloud_folder(self):
        self.finish()
        again = Service(self.cfg, [])
        (self.cfg.dest_root / ".keepsake-anchor").unlink()
        self.assertTrue(again.status()["setup"]["complete"])
        self.assertTrue(again.setup_complete())

    def test_an_unfinished_setup_survives_a_restart_as_unfinished(self):
        self.setup()
        again = Service(self.cfg, [])
        self.assertEqual(again.status()["setup"]["step"], "folder")


class ApiLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        cfg = replace(make_cfg(self.tmp, setup=True), port=0, password="", host="127.0.0.1")
        (cfg.dest_root / ".keepsake-anchor").unlink()
        fresh_tree(self.tmp)
        self.svc = Service(cfg, [])
        self.httpd = serve(self.svc)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.c = Client(self.httpd.server_address[1])
        self.c.open()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def call(self, method, path, body=None):
        return self.c.call(method, path, body)

    def test_status_says_the_setup_is_open(self):
        status, data, _ = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["setup"]["step"], "confirm")

    def test_everything_outside_the_wizard_is_refused_with_a_plain_sentence(self):
        for method, path in (("GET", "/api/settings"), ("PUT", "/api/settings"),
                             ("POST", "/api/settings/test-alert"),
                             ("GET", "/api/activity"), ("GET", "/api/diagnostics"),
                             ("PATCH", "/api/jobs/photos"),
                             ("GET", "/api/jobs/photos/deleted"),
                             ("POST", "/api/jobs/photos/restore"),
                             ("POST", "/api/jobs/photos/restore-batch"),
                             ("GET", "/api/jobs/photos/restore-plan"),
                             ("POST", "/api/password"), ("GET", "/api/anything-new")):
            status, data, _ = self.call(method, path, {})
            self.assertEqual(status, 403, (method, path))
            self.assertEqual(data["error"] if "error" in data else data["message"],
                             SETUP_REFUSAL, (method, path))

    def test_the_wizard_can_do_what_it_needs(self):
        self.assertEqual(self.call("GET", "/api/browse?root=sources&path=")[0], 200)
        self.assertEqual(self.call("POST", "/api/icloud/confirm", {})[0], 200)
        self.assertEqual(self.call("POST", "/api/jobs", {
            "name": "photos", "root": "sources", "source": "Photos"})[0], 200)
        self.assertEqual(self.call("POST", "/api/jobs/photos/preview", {})[0], 200)
        self.svc.run_next()                                  # the worker makes the preview
        self.assertEqual(self.call("GET", "/api/jobs/photos/preview")[0], 200)
        self.assertEqual(self.call("POST", "/api/jobs/photos/sync", {})[0], 200)
        self.assertEqual(self.call("POST", "/api/logout", {})[0], 200)

    def test_a_folder_that_has_not_copied_yet_can_be_removed_but_not_one_that_has(self):
        self.call("POST", "/api/icloud/confirm", {})
        self.call("POST", "/api/jobs", {"name": "photos", "root": "sources", "source": "Photos"})
        self.assertEqual(self.call("POST", "/api/jobs/photos/remove", {})[0], 200)
        self.call("POST", "/api/jobs", {"name": "photos", "root": "sources", "source": "Photos"})
        self.call("POST", "/api/jobs/photos/sync", {})
        self.svc.run_next()
        status, _, _ = self.call("POST", "/api/jobs/photos/remove", {})
        self.assertEqual(status, 403, "a copy exists: removal waits for the end of the setup")

    def test_the_lock_opens_when_the_setup_is_complete(self):
        self.call("POST", "/api/icloud/confirm", {})
        self.call("POST", "/api/jobs", {"name": "photos", "root": "sources", "source": "Photos"})
        self.call("POST", "/api/jobs/photos/sync", {})
        self.svc.run_next()
        self.assertEqual(self.call("GET", "/api/settings")[0], 403)
        self.call("POST", "/api/jobs/photos/verify", {})
        self.assertEqual(self.call("GET", "/api/settings")[0], 200)
        self.assertEqual(self.call("GET", "/api/activity")[0], 200)


class AllowList(unittest.TestCase):
    class Svc:
        def never_synced(self, name):
            return True

    def test_an_unknown_route_is_locked_by_default(self):
        self.assertFalse(setup_allows("GET", ["something-new"], self.Svc()))
        self.assertFalse(setup_allows("DELETE", ["jobs", "photos"], self.Svc()))

    def test_the_locations_routes_are_open_for_the_wizard(self):
        for method in ("GET", "POST", "DELETE", "PUT"):
            self.assertTrue(setup_allows(method, ["locations"], self.Svc()))
            self.assertTrue(setup_allows(method, ["locations", "nas"], self.Svc()))


if __name__ == "__main__":
    unittest.main()
