import json
import tempfile
import unittest
from pathlib import Path

from keepsake.config import (
    Config,
    ConfigError,
    JobStore,
    apply_settings,
    is_loopback,
    parse_roots,
    validate_job,
)
from tests.helpers import make_cfg


class EnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **kw):
        return {"KEEPSAKE_DATA": str(self.tmp), **kw}

    def test_defaults_are_generic(self):
        cfg = Config.from_env(self.env())
        self.assertEqual(cfg.roots, {"sources": Path("/sources")})
        self.assertEqual(cfg.dest_root, Path("/icloud"))
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.tz, "UTC")
        self.assertFalse(cfg.needs_password)

    def test_environment_beats_the_config_file(self):
        (self.tmp / "config.json").write_text(json.dumps(
            {"max_dest_gb": 11, "shrink_pct": 30, "roots": {"docs": "/mnt/docs"}}))
        cfg = Config.from_env(self.env(KEEPSAKE_MAX_DEST_GB="22"))
        self.assertEqual((cfg.max_dest_gb, cfg.shrink_pct), (22, 30))
        self.assertEqual(cfg.roots, {"docs": Path("/mnt/docs")})

    def test_roots_markers_and_fs_roots(self):
        cfg = Config.from_env(self.env(
            KEEPSAKE_ROOTS="a=/x/a,b=/x/b", KEEPSAKE_FS_ROOTS="a",
            KEEPSAKE_ROOT_MARKERS="b=.keepsake-root"))
        self.assertEqual(cfg.fs_roots, frozenset({"a"}))
        self.assertEqual(cfg.root_markers, {"b": ".keepsake-root"})
        with self.assertRaises(ConfigError):
            Config.from_env(self.env(KEEPSAKE_ROOTS="a=/x", KEEPSAKE_FS_ROOTS="zzz"))
        with self.assertRaises(ConfigError):
            Config.from_env(self.env(KEEPSAKE_ROOTS="a=/x",
                                     KEEPSAKE_ROOT_MARKERS="a=../x"))

    def test_bad_values_are_plain_refusals(self):
        for bad in ({"KEEPSAKE_PORT": "abc"}, {"KEEPSAKE_SHRINK_PCT": "100"},
                    {"KEEPSAKE_ROOTS": "nope"}, {"KEEPSAKE_WEBHOOK_URL": "ftp://x"}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                Config.from_env(self.env(**bad))

    def test_password_file_is_read_and_a_missing_one_refuses(self):
        pw = self.tmp / "pw"
        pw.write_text("s3cret-pass-word\n")
        cfg = Config.from_env(self.env(KEEPSAKE_PASSWORD_FILE=str(pw)))
        self.assertEqual(cfg.password, "s3cret-pass-word")
        with self.assertRaises(ConfigError):
            Config.from_env(self.env(KEEPSAKE_PASSWORD_FILE=str(self.tmp / "none")))

    def test_secrets_never_appear_in_repr(self):
        cfg = Config.from_env(self.env(KEEPSAKE_PASSWORD="hunter2hunter2",
                                       KEEPSAKE_WEBHOOK_URL="https://x.example/tok123"))
        text = repr(cfg)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("tok123", text)

    def test_loopback_detection(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            self.assertTrue(is_loopback(host), host)
        for host in ("0.0.0.0", "192.0.2.1", "example.test", "::"):
            self.assertFalse(is_loopback(host), host)
        self.assertTrue(Config.from_env(self.env(KEEPSAKE_HOST="0.0.0.0")).needs_password)

    def test_settings_validation(self):
        cfg = make_cfg(self.tmp)
        new = apply_settings(cfg, {"max_dest_gb": 5, "notify_on": ["failed"],
                                   "webhook_url": "https://hook.example/x"})
        self.assertEqual((new.max_dest_gb, new.notify_on), (5, ("failed",)))
        for bad in ({"shrink_pct": 0}, {"notify_on": ["x"]}, {"webhook_url": "nope"},
                    "text"):
            with self.assertRaises(ConfigError):
                apply_settings(cfg, bad)

    def test_parse_roots(self):
        self.assertEqual(parse_roots("a=/x"), {"a": Path("/x")})
        # Camel Case is allowed and keeps its case; spaces and other characters are not.
        self.assertEqual(parse_roots("HomeFolder=/x"), {"HomeFolder": Path("/x")})
        for bad in ("Home Folder=/x", "home_folder=/x", "-a=/x", "a/b=/x"):
            with self.assertRaises(ConfigError):
                parse_roots(bad)


class JobValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, **kw):
        base = {"name": "photos", "root": "sources", "source": "Photos"}
        base.update(kw)
        return validate_job(base, self.cfg, [])

    def test_good_job_gets_safe_defaults(self):
        job = self.check()
        self.assertEqual((job.mode, job.keep_days, job.dest, job.adopt_existing),
                         ("mirror-keep-deleted", 30, "photos", False))

    def test_refusals_are_plain_sentences(self):
        for bad in ({"source": "../etc"}, {"source": "/etc"}, {"source": "a/../b"},
                    {"root": "etc"}, {"name": "Bad Name"}, {"mode": "wipe"},
                    {"schedule": {"every_hours": 0}}, {"schedule": {"at": "25:00"}},
                    {"source": "Keys/.ssh"}, {"source": "a\\b"}, {"keep_days": 0},
                    {"excludes": "nope"}, {"dest": "../x"},
                    {"adopt_existing": "yes"}, {"enabled": "no"}, {"max_gb": 0}):
            with self.assertRaises(ConfigError, msg=str(bad)) as ctx:
                self.check(**bad)
            self.assertGreater(len(str(ctx.exception)), 15)

    def test_duplicates_and_overlaps_refused(self):
        first = self.check()
        for other in ({"name": "photos", "source": "Other"},
                      {"name": "p2", "source": "Photos/2025"},
                      {"name": "p3", "source": "Photos", "dest": "photos"}):
            with self.assertRaises(ConfigError):
                validate_job({"root": "sources", **other}, self.cfg, [first])

    def test_hand_edited_jobs_file_that_escapes_is_refused_on_read(self):
        self.cfg.jobs_file.write_text(json.dumps({"jobs": [
            {"name": "x", "root": "sources", "source": "../../etc"}]}))
        with self.assertRaises(ConfigError):
            JobStore(self.cfg).load()


if __name__ == "__main__":
    unittest.main()
