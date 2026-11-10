# Public release checklist

Use this before changing repository visibility and repeat the public checks after
material repository or GitHub settings changes. “Today” means 2026-11-07. The
GitHub API results below were read with `gh api` on that day.

## Before going public

- [ ] **Confirm visibility.** Verify with
  `gh api repos/CaputoDavide93/Keepsake-for-iCloud --jq .visibility`.

  Today: `private`.

- [ ] **Run the repository gate.** Verify with `make check`.

  Today: it passes on `main` as of the last merged change (CI shows the same gate green).

- [ ] **Scrub the current tree and history.** Verify the current tree with
  `make scrub` (or `python3 tools/scrub.py`). The following history check uses
  the scrubber's generic rules plus the private words in the git-ignored
  `.deny-local` file (see `tools/scrub.py`), while excluding the scrubber and its
  intentional test fixtures:

  ```sh
  git log --all --format='%an <%ae> %cn <%ce>%n%B' -p -- . \
    ':(exclude)tools/scrub.py' ':(exclude)tests/test_scrub.py' \
    ':(exclude)tests/test_hygiene.py' ':(exclude)LICENSE' |
  python3 -c 'import re, sys; from functools import reduce; from tools.scrub import ALLOWED, DENY, local_rules; text = reduce(lambda value, allowed: value.replace(allowed, ""), ALLOWED, sys.stdin.read()); hits = [reason for pattern, reason in list(DENY) + local_rules() if re.search(pattern, text, re.IGNORECASE)]; print("history scrub: ok" if not hits else "history scrub: FAIL " + ", ".join(hits)); raise SystemExit(bool(hits))'
  ```

  Today: `make scrub` passes. History is a single squashed commit authored with the
  GitHub no-reply address, so the command above prints `history scrub: ok`.
  The private words are kept out of the repository on purpose: the tracked
  scrubber only holds generic rules, and `.deny-local` is never committed.

- [ ] **Verify the licence and trademark notice.** Run:

  ```sh
  head -n 1 LICENSE
  grep -F -e 'Not affiliated with or endorsed by Apple Inc.' \
    -e 'iCloud is a trademark of Apple Inc.' README.md tests/test_hygiene.py
  ```

  Today: `LICENSE` is MIT; both trademark statements are present in the README
  and enforced by `tests/test_hygiene.py`.

- [ ] **Verify secret handling.** Run
  `grep -A22 '^# Secrets and credentials: never commit$' .gitignore` and
  `git check-ignore -q .env`.

  Today: `.gitignore` has the secrets-and-credentials block, ignores `.env` and
  keeps `.env.example` eligible for tracking.

- [ ] **Review screenshots.** Inspect `docs/assets/screenshots/`, then run
  `grep -F 'Demo data only (generic folders in a temporary directory).' README.md`
  and `git ls-files docs/assets/screenshots`.

  Today: the README states the screenshots are demo data only; the tracked
  files are PNGs from the screenshot tool, but only your own look at them can
  confirm no real folder name or photo is in one.

- [ ] **Confirm Actions policy.** Verify with
  `gh api repos/CaputoDavide93/Keepsake-for-iCloud/actions/permissions --jq '{enabled,allowed_actions,sha_pinning_required}'`.

  Today: Actions is enabled, `allowed_actions` is `all`, and
  `sha_pinning_required` is `false`.

- [ ] **Confirm repository metadata and publishing state.** Run:

  ```sh
  gh api repos/CaputoDavide93/Keepsake-for-iCloud --jq '{description,topics}'
  gh api repos/CaputoDavide93/Keepsake-for-iCloud/tags --jq length
  gh api repos/CaputoDavide93/Keepsake-for-iCloud/releases --jq length
  ```

  Today: the description and topics are set; there are no tags and no releases.

- [ ] **Record unavailable private-repository controls.** Run:

  ```sh
  gh api -i repos/CaputoDavide93/Keepsake-for-iCloud/branches/main/protection
  gh api -i repos/CaputoDavide93/Keepsake-for-iCloud/rulesets
  gh api repos/CaputoDavide93/Keepsake-for-iCloud --jq .security_and_analysis
  gh api -i repos/CaputoDavide93/Keepsake-for-iCloud/private-vulnerability-reporting
  ```

  Today: branch protection and rulesets return HTTP 403, “Upgrade to GitHub Pro
  or make this repository public”, so they cannot be read or set while this
  repository is private on the current plan. `security_and_analysis` is `null`,
  so secret scanning and push protection are not readable while private. The
  private vulnerability reporting endpoint returns 404 and is not enabled.

## While public

- [ ] **Enable private vulnerability reporting.** Use **Settings → Security →
  Code security and analysis → Private vulnerability reporting**, then verify
  with
  `gh api -i repos/CaputoDavide93/Keepsake-for-iCloud/private-vulnerability-reporting`.
  Check the policy with
  `grep -F -e 'security/advisories/new' -e 'caputodav@gmail.com' SECURITY.md`.

  Today: private vulnerability reporting is not enabled. `SECURITY.md` has the
  advisory URL and the public contact address; the advisory URL works only after
  private vulnerability reporting is enabled.

- [ ] **Enable secret scanning and push protection.** Use **Settings → Security
  → Code security and analysis → Secret scanning**, enable secret scanning and
  push protection, then verify with
  `gh api repos/CaputoDavide93/Keepsake-for-iCloud --jq .security_and_analysis`.

  Today: `security_and_analysis` is `null` while the repository is private, so
  neither setting is claimed as enabled.

- [ ] **Enable Dependabot alerts.** Use **Settings → Security → Code security
  and analysis → Dependabot alerts**, then verify with
  `gh api -i repos/CaputoDavide93/Keepsake-for-iCloud/vulnerability-alerts`.

  Today: the endpoint returns HTTP 404, which means Dependabot alerts are not enabled.

- [ ] **Protect `main`.** After all CI jobs have reported, use **Settings →
  Branches → Branch protection rules → Add rule**, target `main`, require status
  checks before merging, and select `check`, `docker`, and `ui`. Verify the job
  IDs with `grep -nE '^  (check|docker|ui):$' .github/workflows/ci.yml` and the
  applied rule with
  `gh api repos/CaputoDavide93/Keepsake-for-iCloud/branches/main/protection`.

  Today: `check`, `docker`, and `ui` are the CI job IDs; branch protection cannot
  be read or set while the repository remains private on the current plan.

- [ ] **Upload the social preview.** Verify the source asset with
  `git ls-files --error-unmatch docs/assets/social-preview.png`, then upload it
  through **Settings → General → Social preview**.

  Today: `docs/assets/social-preview.png` exists in the repository. GitHub has no
  API for uploading a repository social preview, so no uploaded preview is
  claimed.
