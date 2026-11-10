"""The first-run checklist is read from what is really there (the marker file in
the iCloud folder, a stored folder, a finished run with a matching copy check),
never from a flag a person can set."""

import unittest

from tests.helpers import write
from tests.test_service import Base


class FirstRun(Base):
    def steps(self):
        fr = self.svc.status()["first_run"]
        return [s["done"] for s in fr["steps"]], fr["done"]

    def test_the_steps_are_named_and_in_order(self):
        self.assertEqual([s["id"] for s in self.svc.status()["first_run"]["steps"]],
                         ["confirm", "folder", "sync-check"])

    def test_every_step_ticks_itself_from_real_state(self):
        (self.cfg.dest_root / ".keepsake-anchor").unlink()
        self.assertEqual(self.steps(), ([False, False, False], False))
        self.svc.confirm_icloud()
        self.assertEqual(self.steps(), ([True, False, False], False))
        self.add()
        self.assertEqual(self.steps(), ([True, True, False], False))
        self.svc.run_next()
        self.assertEqual(self.steps(), ([True, True, False], False), "a sync alone is not a check")
        self.svc.verify_job("photos")
        self.assertEqual(self.steps(), ([True, True, True], True))

    def test_a_check_that_found_a_difference_does_not_tick(self):
        self.add()
        self.svc.run_next()
        write(self.cfg.dest_root / "photos/2025/0.jpg", "tampered!")
        result = self.svc.verify_job("photos")
        self.assertEqual(result["state"], "mismatch")
        self.assertEqual(self.steps()[0][2], False)

    def test_removing_the_folder_unticks_it(self):
        self.add()
        self.svc.run_next()
        self.svc.verify_job("photos")
        self.assertTrue(self.steps()[1])
        self.svc.remove_job("photos")
        self.assertEqual(self.steps(), ([True, False, False], False))

    def test_an_unavailable_icloud_folder_is_not_confirmed(self):
        import shutil
        shutil.rmtree(self.cfg.dest_root)
        self.assertEqual(self.steps()[0][0], False)

    def test_no_setting_can_tick_it(self):
        (self.cfg.dest_root / ".keepsake-anchor").unlink()
        for key in ("first_run", "first_run_done", "steps", "done"):
            self.svc.update_settings({key: True})
        self.assertEqual(self.steps(), ([False, False, False], False))


if __name__ == "__main__":
    unittest.main()
