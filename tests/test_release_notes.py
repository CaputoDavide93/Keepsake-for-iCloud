"""Tests for the release-note extractor and the release workflow's contract."""

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release-notes.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class ReleaseNotesTests(unittest.TestCase):
    def run_script(self, changelog, tag="v1.2.3"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            path.write_text(textwrap.dedent(changelog), encoding="utf-8")
            env = os.environ.copy()
            env["KEEPSAKE_CHANGELOG"] = str(path)
            return subprocess.run(
                [SCRIPT, tag],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_extracts_only_the_matching_dated_section(self):
        result = self.run_script(
            """
            # Changelog

            ## [Unreleased]

            - Work in progress.

            ## [1.2.3] - 2026-11-07

            ### Added

            - A versioned feature.

            ## [1.2.2] - 2026-10-01

            - An older feature.
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "### Added\n\n- A versioned feature.\n")

    def test_accepts_a_heading_without_a_date(self):
        result = self.run_script(
            """
            # Changelog

            ## [1.2.3]

            - Release notes.
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "- Release notes.\n")

    def test_fails_when_the_section_is_missing(self):
        result = self.run_script(
            """
            # Changelog

            ## [Unreleased]

            - Work in progress.
            """
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Release notes for v1.2.3 are missing", result.stderr)

    def test_fails_when_the_section_is_empty(self):
        result = self.run_script(
            """
            # Changelog

            ## [1.2.3] - 2026-11-07


            ## [1.2.2] - 2026-10-01

            - An older feature.
            """
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Release notes for v1.2.3 are empty", result.stderr)

    def test_rejects_a_tag_that_is_not_a_three_part_version(self):
        result = self.run_script("# Changelog\n", tag="release-1.2.3")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "Release tag must have the form vX.Y.Z.\n")



class ReleaseWorkflowTests(unittest.TestCase):
    """The workflow cannot run here, so its text is pinned to what the docs say."""

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")
        start = self.text.index("\n  release:\n")
        end = self.text.index("\n  ghcr:\n")
        self.release_job = self.text[start:end]

    def test_the_release_uploads_a_source_tarball_and_sha256sums(self):
        job = self.release_job
        self.assertIn("git archive --format=tar.gz", job)
        self.assertIn('--prefix="keepsake-for-icloud-${TAG#v}/"', job)
        self.assertIn('"$TAG"', job.split("git archive", 1)[1].splitlines()[0])
        self.assertIn('sha256sum "$tarball" > SHA256SUMS', job)
        self.assertRegex(job, r'gh release upload "\$TAG" "\$tarball" SHA256SUMS --clobber')

    def test_the_upload_comes_after_the_release_is_created(self):
        job = self.release_job
        self.assertLess(job.index("gh release create"), job.index("gh release upload"))

    def test_every_action_is_pinned_to_a_commit_and_permissions_stay_least_privilege(self):
        for line in re.findall(r"^\s*- uses: (.+)$", self.text, re.M):
            self.assertRegex(line, r"@[0-9a-f]{40}\b", line)
        self.assertRegex(self.text, r"\npermissions:\n  contents: read\n")
        self.assertEqual(self.text.count("contents: write"), 1)
        self.assertIn("contents: write", self.release_job)

    def test_the_docs_describe_the_checksum_files(self):
        docs = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        self.assertIn("SHA256SUMS", docs)
        self.assertIn("keepsake-for-icloud-X.Y.Z.tar.gz", docs)


if __name__ == "__main__":
    unittest.main()
