# 🔒 Securing the web page and API

* **Binds to `127.0.0.1` by default.** On a loopback address no password is
  needed; the page checks the `Host` header so a DNS-rebinding page cannot reach
  it.
* **Any other address needs a password.** `KEEPSAKE_PASSWORD` (or
  `KEEPSAKE_PASSWORD_FILE`, at least 10 characters). Without one Keepsake exits
  with a plain sentence. The Docker image listens on `0.0.0.0` inside the
  container, so it always needs one; the compose file publishes the port on
  `127.0.0.1` only.
* **Change it in the page.** Settings, Change password (current password, the new
  one twice, at least 10 characters). The new password is stored as a salted
  scrypt hash in `password.json` in the data folder, never in clear text, and the
  API never returns it. From then on the hash is the password and
  `KEEPSAKE_PASSWORD` is ignored, so the old value in a `.env` file stops working.
  Every other browser is signed out; the one you changed it in stays in. A wrong
  current password counts against the same login rate limit.
* **CSRF.** Every state-changing request needs the per-session token in
  `X-CSRF-Token`, a JSON `Content-Type` and, when the browser sends an `Origin`,
  one that matches the `Host`. A form on another site can send none of these.
* **Login** is rate limited (5 wrong passwords per client and 25 overall in five
  minutes, then 429 with `Retry-After`), compared in constant time, and issues a
  fresh session id. Cookies are `HttpOnly; SameSite=Strict`, and `Secure` when
  `KEEPSAKE_COOKIE_SECURE=1` (set it behind HTTPS).
* **Headers:** a strict `Content-Security-Policy` (own scripts and styles only,
  no inline code, no framing), `X-Content-Type-Options`, `Referrer-Policy:
  no-referrer`, `Cross-Origin-*` isolation, `Cache-Control: no-store` on the API.
  The page loads no font, script, image or tracker from another site.
* **No secrets in logs.** Passwords, session ids and the webhook address are
  never logged; a failed alert is logged by channel and host name only. ntfy
  and Pushover keys, topic addresses and the e-mail password come from the
  environment or `*_FILE` files, are never returned by the API (Settings lists
  only whether each channel is configured), and are removed from the error
  sentence of a test alert. Keepsake refuses to send an e-mail password
  without STARTTLS or SSL.
* **Diagnostics leave things out.** `GET /api/diagnostics` (the Settings
  button "Download diagnostics") never includes a password, token, key, webhook,
  ntfy, Pushover, SMTP or MQTT secret or address (alert channels appear by name
  and whether they are set), cuts any URL in a log line to scheme and host,
  shows the home folder as `~`, and names no file from inside your folders. It
  needs the same login as the rest of the API.
* **Read-only sources.** Mount them `:ro` (the compose file does). The page can
  only browse inside the roots named in `KEEPSAKE_ROOTS`, never follows symlinks
  while browsing, and refuses `..` and absolute paths.
* **Limits:** requests over 64 KB (or with a negative length) are refused, a
  client that stalls is dropped after 20 seconds, and at most 64 requests are
  served at once; the process serves one household, not the internet.

## Reaching it from another device

The stdlib server has no TLS. Put a reverse proxy that terminates HTTPS in front
of it, keep the container port bound to `127.0.0.1`, and **always set
`KEEPSAKE_PASSWORD`**: with no password Keepsake means "this Mac only", so it
refuses any request that carries a proxy header (`X-Forwarded-*`, `Forwarded`,
`Via`, `X-Real-IP`) and refuses to start if `KEEPSAKE_ALLOWED_HOSTS` is set
without a password. Also set `KEEPSAKE_COOKIE_SECURE=1`. Do not publish port 8765
on a public interface. A proxy that strips those headers defeats the check, which
is another reason the password is the only real protection.

## Forgot the password

Run this on the Mac that runs Keepsake. It asks for the new password twice
without showing it, stores its hash, and signs everyone out:

```bash
docker exec -it keepsake python3 -m keepsake reset-password
```

Without Docker, run `python3 -m keepsake reset-password` with the same
`KEEPSAKE_DATA` the service uses. For a script, `--stdin` reads one line and
`--password-file FILE` reads the first line of a file; neither prints the
password. The password still needs at least 10 characters, and the running page
needs no restart: it notices the change on the next request.

## Automation
## Automation

The API accepts the password as `Authorization: Bearer <password>`, which skips
the cookie and CSRF steps (there is no ambient credential to forge). Failed
bearer attempts count against the same rate limit.
