"""The scrub: no private identifier may appear in this public repository.

Three halves: the generic rules catch what they should, the private-words file
works (tested with invented words, never real ones), and the repository as it
stands is clean.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import scrub  # noqa: E402


class WithoutPrivateFile(unittest.TestCase):
    """Generic rules only: what a fresh clone and CI see."""

    def setUp(self):
        self._old = os.environ.get("KEEPSAKE_SCRUB_DENY")
        os.environ["KEEPSAKE_SCRUB_DENY"] = "/nonexistent/deny-file"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KEEPSAKE_SCRUB_DENY", None)
        else:
            os.environ["KEEPSAKE_SCRUB_DENY"] = self._old

    def hits(self, text, rel="src/keepsake/x.py"):
        return scrub.scan_text(rel, text)

    def test_private_addresses_paths_and_emails_are_denied(self):
        for bad in (
            "host = '192.168.20.250'", "10.0.0.5", "172.20.1.9",
            "/Users/someone/Documents", "someone@example.com",
        ):
            self.assertTrue(self.hits(bad), bad)

    def test_first_names_are_denied_in_code_but_the_author_footer_is_fine(self):
        self.assertTrue(self.hits("# written by Davide"))
        self.assertTrue(self.hits("Davide", "tests/test_x.py"))
        self.assertFalse(self.hits("Made with love by Davide Caputo", "README.md"))

    def test_the_public_contact_address_is_allowed(self):
        self.assertFalse(self.hits("write to caputodav@gmail.com", "SECURITY.md"))

    def test_generic_text_is_clean(self):
        self.assertFalse(self.hits("roots = /sources, icloud = /icloud, 127.0.0.1"))

    def test_the_scrubber_names_no_one(self):
        """The deny rules in the tracked file must stay generic."""
        source = (scrub.ROOT / "tools" / "scrub.py").read_text(encoding="utf-8")
        self.assertEqual(scrub.scan_text("src/keepsake/y.py", source.replace("davide|caputo", "")), [])


class PrivateWordsFile(unittest.TestCase):
    """The git-ignored file adds rules without any of them being in the repository."""

    def setUp(self):
        self._old = os.environ.get("KEEPSAKE_SCRUB_DENY")
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "deny"
        os.environ["KEEPSAKE_SCRUB_DENY"] = str(self.path)

    def tearDown(self):
        self._dir.cleanup()
        if self._old is None:
            os.environ.pop("KEEPSAKE_SCRUB_DENY", None)
        else:
            os.environ["KEEPSAKE_SCRUB_DENY"] = self._old

    def test_a_word_in_the_file_is_denied_with_its_reason(self):
        self.path.write_text("# comment\n\nexamplename\ta first name\nhostx\n", encoding="utf-8")
        hits = scrub.scan_text("docs/a.md", "say hi to ExampleName and HostX")
        self.assertEqual(sorted(h[2] for h in hits), ["a first name", "a private identifier"])

    def test_without_the_file_nothing_extra_is_denied(self):
        self.assertEqual(scrub.scan_text("docs/a.md", "say hi to examplename"), [])

    def test_comments_and_blank_lines_are_ignored(self):
        self.path.write_text("# only a comment\n\n   \n", encoding="utf-8")
        self.assertEqual(scrub.local_rules(), [])


class SymlinksAreRefused(unittest.TestCase):
    def test_a_tracked_symlink_is_a_violation(self):
        with tempfile.TemporaryDirectory(dir=scrub.ROOT) as d:
            link = Path(d) / "link"
            os.symlink("/somewhere/else", link)
            hits = scrub.scan([str(link.relative_to(scrub.ROOT))])
            self.assertEqual(len(hits), 1)
            self.assertIn("symlink", hits[0][2])


class RepositoryIsClean(unittest.TestCase):
    def test_no_tracked_file_names_a_private_identifier(self):
        hits = scrub.scan(scrub.tracked_files())
        self.assertEqual(hits, [], "\n".join(f"{h[0]}:{h[1]} {h[2]}: {h[3]}" for h in hits))


if __name__ == "__main__":
    unittest.main()
