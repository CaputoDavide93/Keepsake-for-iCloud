# 🤝 Contributing

Thanks for helping. Keepsake touches people's files, so the bar is "refuse
loudly rather than risk data".

1. `make setup` once (Python 3.11 or newer).
2. Make your change on a branch.
3. `make check` must pass: lint, tests, the scrub, the diagram check. For web
   page changes also run `make ui-test` (see the README) and `make docker-smoke`
   if you touched packaging.
4. A new guard or refusal needs a test that fails without it. A new setting
   needs an entry in `.env.example`; `tests/test_hygiene.py` checks the parity.
5. Documentation must be true of the code: every command, flag and feature.
   No hand-typed counts.
6. Commits are conventional (`feat:`, `fix:`, `docs:`, `refactor:`, `ci:`,
   `chore:`). `make hooks` arms the local pre-commit scrub.

Please do not put real folder names, addresses, host names or personal paths in
code, tests, docs or screenshots: `tools/scrub.py` will refuse them (put your own private words in a git-ignored `.deny-local`, one regex per line, so they are caught before they are committed and never named in the repository). Use generic
names (Photos, Documents, Projects).

Security problems: see [SECURITY.md](SECURITY.md), not a public issue.
