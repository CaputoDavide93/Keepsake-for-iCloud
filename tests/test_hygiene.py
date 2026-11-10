"""Documentation never lies: the repo's own claims are checked against the code."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tracked():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.splitlines() if p]


class EnvParity(unittest.TestCase):
    def test_every_variable_the_code_reads_is_in_env_example(self):
        used = set()
        for path in (ROOT / "src").rglob("*.py"):
            used |= set(re.findall(r"\b(KEEPSAKE_[A-Z_]+)\b", path.read_text()))
        used |= {v + "_FILE" for v in (
            "KEEPSAKE_PASSWORD", "KEEPSAKE_WEBHOOK_URL", "KEEPSAKE_MQTT_PASSWORD",
            "KEEPSAKE_NTFY_URL", "KEEPSAKE_NTFY_TOKEN", "KEEPSAKE_PUSHOVER_TOKEN",
            "KEEPSAKE_PUSHOVER_USER", "KEEPSAKE_SMTP_PASSWORD")}
        used.add("TZ")
        used.discard("KEEPSAKE_PASSWORD_FILE_")
        listed = set(re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
        # Compose-only host variables are listed too; they are not read by the code.
        compose_only = {"KEEPSAKE_SOURCES_HOST_DIR", "KEEPSAKE_ICLOUD_HOST_DIR",
                        "KEEPSAKE_HOST_PORT"}
        self.assertEqual(sorted(used - listed), [], "read by the code, missing from .env.example")
        self.assertEqual(sorted(listed - used - compose_only), [],
                         "listed in .env.example but never read")

    def test_compose_variables_are_listed(self):
        text = (ROOT / "docker-compose.yml").read_text()
        listed = set(re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
        for var in set(re.findall(r"\$\{([A-Z_]+)", text)):
            self.assertIn(var, listed)


class ReadmeTruth(unittest.TestCase):
    def test_readme_repo_tree_matches_git_ls_files_top_level(self):
        readme = (ROOT / "README.md").read_text()
        block = re.search(r"## 📁 Repo structure\s+```text\n(.*?)```", readme, re.S)
        self.assertTrue(block, "README needs a repo structure tree")
        shown = set(re.findall(r"^[├└]── ([\w.\-]+/?)", block.group(1), re.M))
        real = set()
        for p in tracked():
            head = p.split("/")[0]
            real.add(head + "/" if "/" in p else head)
        self.assertEqual(sorted(real - shown), [], "tracked at top level but missing from the tree")
        self.assertEqual(sorted(shown - real), [], "in the tree but not tracked")

    def test_every_documented_make_target_exists(self):
        makefile = (ROOT / "Makefile").read_text()
        targets = set(re.findall(r"^([a-z][\w-]*):", makefile, re.M))
        for text in ((ROOT / "README.md").read_text(), (ROOT / "AGENTS.md").read_text(),
                     (ROOT / "CONTRIBUTING.md").read_text()):
            for name in re.findall(r"`make ([a-z][\w-]*)", text):
                self.assertIn(name, targets, f"make {name} is documented but missing")

    def test_every_doc_is_linked_from_the_readme_or_docs_index(self):
        index = (ROOT / "README.md").read_text() + (ROOT / "docs/README.md").read_text()
        for p in tracked():
            if p.startswith("docs/") and p.endswith(".md") and p != "docs/README.md":
                self.assertTrue(p in index or p.split("/", 1)[1] in index, f"{p} is not linked")

    def test_ci_badge_points_at_a_real_workflow(self):
        readme = (ROOT / "README.md").read_text()
        for wf in re.findall(r"actions/workflows/([\w.-]+)/badge\.svg", readme):
            self.assertTrue((ROOT / ".github/workflows" / wf).exists(), wf)

    def test_license_badge_matches_the_license_file(self):
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text())
        self.assertIn("License-MIT", (ROOT / "README.md").read_text())

    def test_trademark_notice_and_no_apple_marks(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("Not affiliated with or endorsed by Apple Inc.", readme)
        self.assertIn("iCloud is a trademark of Apple Inc.", readme)
        self.assertEqual([p for p in tracked() if re.search(r"apple|icloud-logo", p, re.I)], [])

    def test_the_web_page_loads_nothing_from_other_sites(self):
        for path in (ROOT / "src/keepsake/web").iterdir():
            if path.suffix == ".png":      # the icons are pictures: nothing to read a URL from
                continue
            text = path.read_text()
            for url in re.findall(r"https?://[^\s\"')]+", text):
                # The SVG namespace, and one placeholder shown inside an empty field.
                self.assertTrue(url.startswith(("http://www.w3.org/", "https://example.com/")),
                                f"{path.name} references {url}")

    def test_footer_is_the_house_credit_line_and_ends_the_readme(self):
        readme = (ROOT / "README.md").read_text().rstrip()
        self.assertTrue(readme.endswith(
            '<p align="center"><sub>Made with ❤️ by '
            '<a href="https://github.com/CaputoDavide93">Davide Caputo</a></sub></p>'))


if __name__ == "__main__":
    unittest.main()
