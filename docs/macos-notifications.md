# Optional macOS notifications

This helper is optional. It runs in the logged-in Mac account, outside the
Keepsake container, and polls the existing status API. Keepsake does not need it
to copy, check, or recover files.

## Install

The helper sends the same password used by the Keepsake web page. Create a
one-line password file without putting the password in a command argument:

```bash
umask 077
mkdir -p "$HOME/Library/Application Support/keepsake-notify"
printf 'Keepsake password: '
IFS= read -r -s pw && printf '%s\n' "$pw" \
  > "$HOME/Library/Application Support/keepsake-notify/password"
unset pw
```

The file is created readable by you only. The helper authenticates with
`Authorization: Bearer <password>`, which the API accepts only when Keepsake
has a password configured (`KEEPSAKE_PASSWORD`, `KEEPSAKE_PASSWORD_FILE` or the
`password` setting in `config.json`), so it needs a Keepsake that has one.

Then install and load the per-user launch agent:

```bash
./tools/macos/install-notify.sh \
  --password-file "$HOME/Library/Application Support/keepsake-notify/password" \
  --url "http://localhost:8765"
```

The installer refuses a missing password file or one with any group or other
permissions. It copies the helper into
`~/Library/Application Support/keepsake-notify`, writes the launch agent under
`~/Library/LaunchAgents`, and loads it with `launchctl`. Running the installer
again updates and reloads the same agent.

The launch agent runs one poll when loaded and every 60 seconds after that. To
make a single manual poll, run:

```bash
"$HOME/Library/Application Support/keepsake-notify/keepsake-notify.sh" \
  --once \
  --password-file "$HOME/Library/Application Support/keepsake-notify/password" \
  --url "http://localhost:8765"
```

## Notifications

The helper reads `attention` and `attention_count` from `GET /api/status`. It
notifies once when a job enters the attention list and once when that job leaves
the list (a job also leaves it when it is disabled or removed, so the message
says "no longer needs attention", not "fixed"). It also notifies once when the API cannot be reached, once when it can
be reached again, and once when the API rejects the password. Repeated polls in
the same state do not repeat a notification. A notification that macOS refuses
to show is not retried.

State is stored in
`~/Library/Application Support/keepsake-notify`. Removing that state makes the
next poll treat every current attention item as new. Notification delivery
depends on the Mac's notification settings for the process running
`osascript`. The helper only reports the API view; it does not run jobs, inspect
copies, or change Keepsake settings. If the API is not on this Mac, use a URL
whose transport and access controls you trust.

## Uninstall

```bash
./tools/macos/uninstall-notify.sh
```

Uninstalling is safe to repeat. It unloads and removes the launch agent and the
installed helper. It leaves the password and state files in place so that
removing secrets and history remains an explicit choice.
