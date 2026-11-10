"""Portable tests for the optional macOS notification helper."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTIFY = ROOT / "tools" / "macos" / "keepsake-notify.sh"
INSTALL = ROOT / "tools" / "macos" / "install-notify.sh"
UNINSTALL = ROOT / "tools" / "macos" / "uninstall-notify.sh"


class MacOSNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state = self.base / "state"
        self.password = self.base / "password"
        self.password.write_text("correct horse battery staple\n")
        self.password.chmod(0o600)
        self.scenario = self.base / "scenario.json"
        self.counter = self.base / "curl-count"
        self.osa_log = self.base / "osascript.jsonl"
        self.curl_log = self.base / "curl-argv.jsonl"
        self.curl_stdin = self.base / "curl-stdin.txt"
        self._stub("curl", r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
pathlib.Path(os.environ["STUB_CURL_ARGV"]).open("a").write(json.dumps(args) + "\n")
pathlib.Path(os.environ["STUB_CURL_STDIN"]).open("a").write(sys.stdin.read())
counter = pathlib.Path(os.environ["STUB_CURL_COUNTER"])
index = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(index + 1))
items = json.loads(pathlib.Path(os.environ["STUB_CURL_SCENARIO"]).read_text())
item = items[min(index, len(items) - 1)]
if item.get("exit", 0):
    raise SystemExit(item["exit"])
output = pathlib.Path(args[args.index("--output") + 1])
output.write_text(json.dumps(item.get("body", {})))
sys.stdout.write(str(item.get("status", 200)))
''')
        self._stub("osascript", r'''#!/usr/bin/env python3
import json, os, pathlib, sys
sys.stdin.read()
notice = pathlib.Path(sys.argv[-1]).read_text().splitlines()
entry = {"argv": sys.argv[1:], "title": notice[0], "message": notice[1]}
pathlib.Path(os.environ["STUB_OSASCRIPT_LOG"]).open("a").write(json.dumps(entry) + "\n")
''')
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
            "KEEPSAKE_NOTIFY_STATE_DIR": str(self.state),
            "STUB_CURL_ARGV": str(self.curl_log),
            "STUB_CURL_STDIN": str(self.curl_stdin),
            "STUB_CURL_COUNTER": str(self.counter),
            "STUB_CURL_SCENARIO": str(self.scenario),
            "STUB_OSASCRIPT_LOG": str(self.osa_log),
        })

    def _stub(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _set_scenario(self, *items):
        self.scenario.write_text(json.dumps(items))

    def _poll(self, check=True):
        return subprocess.run(
            [str(NOTIFY), "--once", "--password-file", str(self.password),
             "--url", "http://localhost:8765"],
            env=self.env, capture_output=True, text=True, check=check,
        )

    def _notices(self):
        if not self.osa_log.exists():
            return []
        return [json.loads(line) for line in self.osa_log.read_text().splitlines()]

    def test_stubs_are_first_on_path(self):
        found = subprocess.run(
            ["/bin/sh", "-c", "command -v curl; command -v osascript"],
            env=self.env, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(found, [str(self.bin / "curl"), str(self.bin / "osascript")])

    def test_attention_notifies_only_on_transitions_and_leaving_the_list(self):
        attention = {"attention": [{"job": "photos", "reason": "copy \\\"mismatch\\\""}],
                     "attention_count": 1}
        clear = {"attention": [], "attention_count": 0}
        self._set_scenario({"body": attention}, {"body": attention}, {"body": clear})
        self._poll()
        self._poll()
        self._poll()
        messages = [item["message"] for item in self._notices()]
        self.assertEqual(messages, [
            'photos needs attention: copy \\"mismatch\\"',
            "photos no longer needs attention.",
        ])

    def test_unreachable_and_reachable_notices_do_not_repeat(self):
        clear = {"attention": [], "attention_count": 0}
        self._set_scenario({"exit": 7}, {"exit": 7}, {"body": clear})
        self._poll(check=False)
        self._poll(check=False)
        self._poll()
        self.assertEqual([item["message"] for item in self._notices()], [
            "The Keepsake API is unreachable.",
            "The Keepsake API is reachable again.",
        ])

    def test_rejected_password_notifies_once_and_never_reaches_osascript_argv(self):
        self._set_scenario({"status": 401}, {"status": 401})
        self._poll(check=False)
        self._poll(check=False)
        notices = self._notices()
        self.assertEqual([item["message"] for item in notices], [
            "The Keepsake notification password was rejected."
        ])
        argv_log = "\n".join(" ".join(item["argv"]) for item in notices)
        self.assertNotIn(self.password.read_text().strip(), argv_log)

    def test_password_goes_to_curl_on_stdin_never_on_argv(self):
        self._set_scenario({"body": {"attention": [], "attention_count": 0}})
        self._poll()
        secret = self.password.read_text().strip()
        self.assertNotIn(secret, self.curl_log.read_text())
        self.assertIn("Authorization: Bearer " + secret, self.curl_stdin.read_text())

    def test_missing_password_file_is_refused_before_any_request(self):
        self.password.unlink()
        result = self._poll(check=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.curl_log.exists())

    def test_plist_carries_awkward_values_exactly(self):
        import plistlib
        home = self.base / "home"
        home.mkdir()
        odd = self.base / "pw__HOME__&<x>"
        odd.write_text("x\n")
        odd.chmod(0o600)
        env = dict(self.env, HOME=str(home), KEEPSAKE_NOTIFY_LAUNCHCTL="/usr/bin/true")
        subprocess.run([str(INSTALL), "--password-file", str(odd),
                        "--url", "http://localhost:8765/__URL__?a=1&b=2"],
                       env=env, check=True, capture_output=True, text=True)
        plist = home / "Library/LaunchAgents/com.example.keepsake-notify.plist"
        args = plistlib.loads(plist.read_bytes())["ProgramArguments"]
        self.assertEqual(args[3], "http://localhost:8765/__URL__?a=1&b=2")
        self.assertEqual(args[5], str(odd))

    def test_install_refuses_a_group_readable_password_file(self):
        home = self.base / "home"
        home.mkdir()
        self.password.chmod(0o640)
        env = dict(self.env, HOME=str(home), KEEPSAKE_NOTIFY_LAUNCHCTL="/bin/false")
        result = subprocess.run([str(INSTALL), "--password-file", str(self.password)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((home / "Library/LaunchAgents").exists())

    def test_install_and_uninstall_are_idempotent(self):
        home = self.base / "home"
        home.mkdir()
        launchctl_log = self.base / "launchctl.jsonl"
        launchctl = self._stub("launchctl-stub", r'''#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path(os.environ["STUB_LAUNCHCTL_LOG"]).open("a").write(json.dumps(sys.argv[1:]) + "\n")
''')
        env = self.env.copy()
        env.update({
            "HOME": str(home),
            "KEEPSAKE_NOTIFY_LAUNCHCTL": str(launchctl),
            "STUB_LAUNCHCTL_LOG": str(launchctl_log),
        })
        command = [str(INSTALL), "--password-file", str(self.password),
                   "--url", "http://localhost:8765"]
        subprocess.run(command, env=env, check=True, capture_output=True, text=True)
        subprocess.run(command, env=env, check=True, capture_output=True, text=True)
        installed = home / "Library/Application Support/keepsake-notify/keepsake-notify.sh"
        plist = home / "Library/LaunchAgents/com.example.keepsake-notify.plist"
        self.assertTrue(installed.exists())
        text = plist.read_text()
        self.assertIn("<integer>60</integer>", text)
        self.assertNotIn("__SCRIPT_PATH__", text)

        subprocess.run([str(UNINSTALL)], env=env, check=True, capture_output=True, text=True)
        subprocess.run([str(UNINSTALL)], env=env, check=True, capture_output=True, text=True)
        self.assertFalse(installed.exists())
        self.assertFalse(plist.exists())
        calls = [json.loads(line) for line in launchctl_log.read_text().splitlines()]
        self.assertEqual(sum(call[0] == "bootstrap" for call in calls), 2)
        self.assertEqual(sum(call[0] == "bootout" for call in calls), 2)


if __name__ == "__main__":
    unittest.main()
