#!/usr/bin/env python3
"""Deny-list scrub: fail when a tracked file names a private identifier.

This project is public. Nothing in it may mention a private network, a private
hostname, a personal path, a person's first name or the systems it grew out of.
The generic rules are in this file; the words that would identify someone live in
a git-ignored `.deny-local` (see LOCAL_DENY_FILE), never in the repository.
`make check` and the pre-commit hook both run this; `tests/test_scrub.py` runs
it too, so a regression fails the test suite as well.

    python3 tools/scrub.py            # scan every tracked (or present) file
    python3 tools/scrub.py --staged   # scan only what is staged (pre-commit)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent

# Your own private words, one per line as `regex<TAB>reason` (the reason is optional),
# in a git-ignored file at the repository root or at $KEEPSAKE_SCRUB_DENY. Absent
# on a fresh clone and in CI, where only the generic rules above run. The pre-commit
# hook and `make scrub` on your machine read it, so a private word is stopped before
# it is ever committed.
LOCAL_DENY_FILE = ".deny-local"

# Everywhere. Only rules that name no one in particular live in this file: a deny list
# that spells out the real names it forbids would publish them. The private words
# (people, hosts, systems, employers) go in `.deny-local` (git-ignored), see LOCAL_DENY.
DENY = [
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "a private LAN address"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "a private LAN address"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "a private LAN address"),
    (r"/users/[a-z]", "a personal path"),
    (r"[\w.+-]+@[\w-]+\.(com|co\.uk|org|net)\b", "an e-mail address"),
]
# Code, tests, config and the web page only (the README footer may name the author).
DENY_CODE = [
    (r"\bdavide\b|\bcaputo\b", "a first name"),
]
CODE_DIRS = ("src/", "tests/", "config/", "scripts/")
CODE_FILES = ("Dockerfile", "docker-compose.yml", ".env.example", "Makefile")
# The contact address is the one public e-mail allowed.
ALLOWED = ("caputodav@gmail.com", "noreply@github.com")
EXEMPT = ("tools/scrub.py", "tests/test_scrub.py", "tests/test_hygiene.py", "LICENSE")
SKIP_SUFFIX = (".png", ".jpg", ".ico", ".woff", ".woff2", ".gz", ".zip", ".pyc")
SKIP_DIRS = (".git/", ".venv/", "node_modules/", "__pycache__/", "build/", "dist/")


def local_rules() -> List[Tuple[str, str]]:
    """The private rules from the git-ignored deny file, or none."""
    path = Path(os.environ.get("KEEPSAKE_SCRUB_DENY") or ROOT / LOCAL_DENY_FILE)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rules = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pattern, _, reason = line.partition("\t")
        rules.append((pattern.strip(), reason.strip() or "a private identifier"))
    return rules


def tracked_files(staged: bool = False) -> List[str]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if staged \
        else ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
        return [line for line in out.stdout.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                if p.is_file() and not any(s in str(p) for s in SKIP_DIRS)]


def scan_text(rel: str, text: str) -> List[Tuple[str, int, str, str]]:
    """Return (file, line number, reason, matched text) for each violation."""
    rules = list(DENY) + local_rules()
    if rel.startswith(CODE_DIRS) or rel in CODE_FILES:
        rules += DENY_CODE
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        scrubbed = line
        for ok in ALLOWED:
            scrubbed = scrubbed.replace(ok, "")
        for pattern, reason in rules:
            match = re.search(pattern, scrubbed, re.IGNORECASE)
            if match:
                hits.append((rel, number, reason, match.group(0)))
    return hits


def scan(files: Iterable[str]) -> List[Tuple[str, int, str, str]]:
    hits: List[Tuple[str, int, str, str]] = []
    for rel in files:
        if rel in EXEMPT or rel.endswith(SKIP_SUFFIX) or rel.startswith(SKIP_DIRS):
            continue
        path = ROOT / rel
        if path.is_symlink():
            hits.append((rel, 0, "a symlink (its target can hold a private path)", str(path.readlink())))
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += scan_text(rel, text)
    return hits


def main(argv: List[str]) -> int:
    hits = scan(tracked_files("--staged" in argv))
    for rel, number, reason, matched in hits:
        print(f"{rel}:{number}: {reason}: {matched!r}")
    if hits:
        print(f"scrub: FAIL, {len(hits)} private identifier(s) found")
        return 1
    print("scrub: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
