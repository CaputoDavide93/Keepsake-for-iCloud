# 🔒 Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/CaputoDavide93/Keepsake-for-iCloud/security/advisories/new)
rather than opening a public issue, or write to caputodav@gmail.com. You should
get a response within a week.

## Data handling

- Keepsake reads the folders you point it at (read-only) and writes copies into
  your own iCloud Drive folder on your own Mac. It sends nothing anywhere else.
- It has no telemetry, no account and no analytics. The web page loads nothing
  from other sites.
- State (jobs, settings, activity log, per-job file lists) is stored as JSON in
  the data folder. Treat that folder as confidential: file names are in it. The
  optional webhook address is stored there too and is never shown again.
- Files are copied as they are. iCloud Drive is not end-to-end encrypted unless
  your Apple Account has Advanced Data Protection on; assume Apple can read what
  you copy. Files named like keys or secrets are skipped, and there is no switch
  to include them.
- Secrets are read from the environment or a `*_FILE` path, never committed or
  logged.

## Deployment checklist

- Keep the default `127.0.0.1` binding, or set `KEEPSAKE_PASSWORD` (10+
  characters). Keepsake refuses to start on any other address without one.
- Serve behind HTTPS if exposed beyond localhost, and set
  `KEEPSAKE_COOKIE_SECURE=1`.
- Mount sources read-only.
- Treat the data folder as confidential.

The threat model and accepted residual risks of the engine are in
[docs/safety-model.md](docs/safety-model.md); the web page in
[docs/security.md](docs/security.md).

## Supported versions

Only the latest commit on `main` is supported.
