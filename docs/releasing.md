# 🚀 Releasing

Nothing in the release workflow creates a tag. A maintainer creates and pushes
the tag after the release changes have been reviewed and merged.

## Prepare and tag a release

1. Move the release entries from `## [Unreleased]` in `CHANGELOG.md` into a
   section headed `## [X.Y.Z] - YYYY-MM-DD`. Leave an `## [Unreleased]` section
   for future changes.
2. Set the same `X.Y.Z` version in the `[project]` section of `pyproject.toml`
   and in `src/keepsake/__init__.py`. Both files carry the project version.
3. Run `make check`.
4. Open a pull request with the changelog and version changes, have it reviewed,
   and merge it into `main`.
5. On the merged `main` commit, create the tag with `git tag vX.Y.Z`.
6. Push that tag with `git push origin vX.Y.Z`.

## What the tag workflow does

A push of a tag matching `v*.*.*` starts `.github/workflows/release.yml`. It
first checks that the tag is exactly `v` plus the version in both version files
and that `CHANGELOG.md` has a non-empty section for it, so a missing section
fails within seconds. The workflow then runs `make check` with Python 3.13 and
Ruff 0.8.6, and separately runs `make docker-smoke`. After both jobs pass, it
extracts the matching changelog section and creates a GitHub release titled
with the tag. A missing or empty section fails the
workflow instead of creating a release.

Right after that the workflow attaches two files to the release: a source
tarball, `keepsake-for-icloud-X.Y.Z.tar.gz`, built with `git archive` from the
tagged commit (every file under a `keepsake-for-icloud-X.Y.Z/` folder), and
`SHA256SUMS`, written with `sha256sum`. To check a download, put both files in
one folder and run `sha256sum -c SHA256SUMS` (on macOS, `shasum -a 256 -c
SHA256SUMS`). The upload uses `--clobber`, so re-running a failed job replaces
the files instead of failing on them.

The workflow does not publish a container image by default. To enable GitHub
Container Registry publishing, set the repository variable `PUBLISH_GHCR` to
`true`. A successful release then builds the root `Dockerfile` and pushes
`ghcr.io/<lowercased-owner>/<lowercased-repository>:X.Y.Z`. It publishes only
the versioned tag, not `latest`.

## If the workflow fails

For a transient runner, registry, or GitHub CLI failure, use **Re-run failed
jobs** for the tag workflow. For a failed check, version mismatch, or invalid
changelog section, correct the problem in a pull request and merge it. The
existing tag still points to the old commit, so remove that tag locally and from
the remote, recreate `vX.Y.Z` on the corrected `main` commit, and push it again.
Do not create a GitHub release manually while the workflow is failing.
