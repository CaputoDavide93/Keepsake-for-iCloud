# 🔌 REST API

JSON in, JSON out, under `/api`. Errors are `{"ok": false, "message": "..."}`
with a plain sentence; successes carry `{"ok": true, "message": "..."}`.

**Authentication.** A browser first calls `GET /api/session` (sets the session
cookie, returns `csrf`), sends `POST /api/login {"password": "..."}` when a
password is set, and then sends `X-CSRF-Token` on every non-GET request. Scripts
can use `Authorization: Bearer <password>` instead and need no CSRF token.

| Method and path | Body or query | What it does |
|---|---|---|
| `GET /api/session` | | `csrf`, `auth_required`, `authenticated`, `version` |
| `POST /api/login` | `password` | Log in |
| `POST /api/logout` | | Log out |
| `POST /api/password` | `current`, `new`, `confirm` | Change the password (needs a password to be set). `403` for a wrong `current` (counts against the login rate limit), `400` for fewer than 10 characters, two different new passwords, or the same password again. Signs out every other login and keeps this one; the stored value is a hash and is never returned |
| `GET /api/status` | | Jobs with status, attention list, roots, caps, iCloud and anchor state, `pause` (see below), `first_run` (the three first-run steps) and `setup` (the guided setup, see below) |
| `GET /api/browse` | `root`, `path` | Subfolders inside an allowed root. Read fresh on every call, which is what the page's Refresh button uses |
| `GET /api/destinations` | | The folders already inside iCloud Drive / Keepsake: `folders` (`name`, `kind`, `job`, `folders`) and `usable`. `kind` is `group` (made by Keepsake to hold several folders, safe to add to), `job` (holds one job's files directly, so it cannot be shared) or `other` (Keepsake did not create it; usable only with `adopt_existing`). A folder a job will make on its first copy is listed before it exists |
| `GET /api/locations` | | `locations` (`name`, `path`, `builtin`, `available`, `why`, `jobs`), `prefixes` (the read-only mounts the page may add under: `path`, `mounted`, `readonly`), `can_add` and `why` |
| `GET /api/locations/browse` | `path` (empty for the mounted places) | Subfolders of a mounted place, walked without following symlinks; `readonly` says whether the mount is read-only |
| `POST /api/locations` | `name`, `path` | Add a location. Refused with a sentence when the folder is outside every mounted prefix, is reached through a symlink, is not on a read-only mount, is or holds or sits inside the iCloud or data folder, overlaps another location, or the name is taken (names that differ only by case count as the same) |
| `POST /api/locations/<name>/remove` | | Remove a location. Refused for a built-in one (from `KEEPSAKE_ROOTS`) and for one a job uses, naming the job |
| `POST /api/jobs` | a job | Add a job. `name` and `dest` are letters, digits and hyphens (no spaces) and keep their case; `group` (optional) is the destination folder the job's `dest` folder goes inside, `Keepsake/<group>/<dest>` |
| `GET /api/jobs/<name>` | | One job with its status |
| `PATCH /api/jobs/<name>` | fields to change | Update schedule, excludes, retention, caps, mode, `enabled` |
| `POST /api/jobs/<name>/sync` | `accept_shrink` (boolean `true` only) | Sync now |
| `POST /api/jobs/<name>/remove` | `delete_copy` (boolean `true` only) | Remove the job; optionally move its iCloud copy to `.removed` for 30 days |
| `POST /api/jobs/<name>/verify` | | Check a copy: read up to 40 files back from the source and iCloud and compare size and sha256. `200` with `verify` when all match; `409` with `ok: false` and `verify` when something differs; `400` when it cannot check |
| `POST /api/jobs/<name>/cancel` | | Stop what is happening to this folder (a copy, a preview, a check or a restore), or take it off the waiting list. It stops between files and between blocks of a file, never in the middle of a write, so nothing is left half-written and your originals are never touched. Allowed while the guided setup is open. A read the operating system is stuck in ends only when the share answers |
| `POST /api/jobs/<name>/preview` | `accept_shrink` (boolean `true` only) | Preview next sync: queue a strictly read-only plan of the next sync on the worker. `200` when queued; `400` with a sentence when the folder is unknown, copying or already waiting. Writes nothing (see below) |
| `GET /api/jobs/<name>/preview` | | `state` (`idle`, `queued` or `running`), `progress` (a sentence) and `progress_detail` (see Progress) while it reads, and `preview`: the last result for this folder, or `null` |
| `GET /api/jobs/<name>/deleted` | | Files kept under `.deleted/<date>/`: `entries` (`date`, `path`, `size`, `days_left`), `truncated`, `skipped_links`, `keep_days`, `mode` |
| `POST /api/jobs/<name>/restore` | `date`, `path` | Copy one kept file into `.restored/<date>/<path>` inside the folder's iCloud copy; `400` with a sentence when refused |
| `GET /api/jobs/<name>/restore-plan` | `date` (optional) | What "restore all" would copy, read-only: `files`, `bytes`, `days` (`date`, `files`, `bytes`) and `skipped` (`path`, `reason`) for links and non-plain entries. Without `date`, every kept date; `400` when nothing is kept or there are more than 5000 files |
| `POST /api/jobs/<name>/restore-batch` | `date` or `all` (boolean `true` only; exactly one), `expect_files` (optional) | Copy every kept file of that date, or of every date, into `.restored/<date>/<same path>`. `200` with `restored`, `bytes`, `dates`, `skipped` and a sentence that names what was skipped; `400` with a sentence when refused or when nothing could be restored. `expect_files` is the count the person confirmed: a different count refuses the batch |
| `POST /api/icloud/confirm` | | The one-time confirmation of the iCloud folder |
| `GET /api/diagnostics` | | A plain-text report to attach to a bug report, served as a download (`Content-Disposition: attachment; filename="keepsake-diagnostics.txt"`) and behind the same login as the rest of `/api`. It holds versions, platform, uptime, which alert channels are configured (names only), the schedule and pause state, each folder's last run, counts, iCloud state and last check, and the last 200 log lines. It never holds a password, token or full address (a URL is cut to scheme and host), shows the home folder as `~`, and names no file from inside your folders |
| `GET /api/activity` | `limit` | The activity log, newest first |
| `GET /api/settings`, `PUT /api/settings` | caps, `webhook_url`, `notify_on` (`failed`, `recovered`, `stale`), `pause`, `quiet_hours` | Read or change the limits, the webhook, the global pause and quiet hours (below). `GET` also returns `pause` (the same object as in `GET /api/status`) and `timezone`, and returns `channels`: `name`, `configured` and the host name for `webhook`, `ntfy`, `pushover`, `email`, `mqtt`; never an address, token, key or password |
| `POST /api/settings/test-alert` | | Send one test message through every configured channel. `200` when all delivered, `502` when any failed, `400` when none is configured; `results` lists `channel`, `ok` and a plain `error` that holds no secret |
| `GET /healthz` | | `{"ok": true}` while the worker is alive (no login) |

A job:

```json
{"name": "documents", "root": "documents", "source": "Taxes",
 "schedule": {"every_hours": 24}, "mode": "mirror-keep-deleted",
 "keep_days": 30, "max_gb": null, "excludes": ["*.tmp"],
 "enabled": true, "adopt_existing": false}
```

Two more job fields: `auto_check` (`off`, `daily`, `weekly`, `monthly`; a new folder gets `weekly` when the field is not sent, a folder stored before it existed is `off`) and `stale_days` (0 to 365, default 3; 0 switches the "no successful run" alert off, and it never fires sooner than twice the folder's schedule).

`schedule` is `{"every_hours": 1..168}` or `{"at": "03:30"}`; `name` is lower-case
letters, digits and hyphens (up to 40) and is also the folder name in iCloud.
The source and the iCloud folder name cannot change after creation.

A job in `GET /api/status` and `GET /api/jobs/<name>` also carries `verify` (the last check: `state` is `match` or `mismatch`, `checked`, `skipped`, `message`, `at`; `null` if never checked) and `history` (the last 14 days, oldest first: a list of `{"day": "YYYY-MM-DD", "outcome": "copied" | "nothing" | "paused"}` with an optional `detail`, one entry per local day in `TZ`, only days with a run; `[]` if none; both described below) and `changes` (the last run: `summary`, `added`, `updated`, `removed`, `samples` of up to 50 `{kind, path}`). `status` is `failed` ("Needs attention") while the last check found a difference.

`history` outcomes come from the run report: `copied` is a run that finished and added, updated or moved something; `nothing` is a run that finished with no change; `paused` is a run that was refused, failed or tripped a guard. A day with several runs shows the latest one, except that a later run with no change does not hide an earlier copy that day, so a day is never `copied` or `nothing` unless its last run finished. Days without a run are absent.

A night's `detail`, present only for nights recorded since it was introduced (an older night has no `detail` key, and the page says no detail was recorded): `at` (the last run that day), `runs` (copying runs added up, normally 1), `added`, `updated`, `removed`, `removed_how` (`moved to .deleted` or `removed`), `samples` (up to 50 `{kind, path}`; a path is cut at 300 characters) and, for a night that was refused or failed, `error` (up to 300 characters). Two copying runs on one day add their counts and keep the first 50 examples; a later refusal replaces the day, as it does the outcome. The history stays at 14 entries and a damaged `detail` is dropped on read. `GET /api/status` also returns `today`, the current local day, which is the last day of the strip. State files written before this field existed simply have an empty `history`.

A job in the same responses also carries `icloud` (`evicted`: the number of evicted placeholders `.<name>.icloud` the last run found in the iCloud folder, `null` before a run has read it; `note`: a plain sentence about what that means for a restore, empty when there are none), `next_check` (when the scheduled check is next due; `null` when it is off, the folder is paused or nothing has been copied yet), `check_rounds` (how many times the rotating sample has gone through every file) and `check_cycle_checks` (about how many checks look at every file once: files / 40, rounded up; skipped files are not compared). These describe the iCloud folder on this Mac.

Each check carries on after the last file the previous one looked at, in an order fixed by a per-folder seed, so a file added or removed between checks does not reshuffle the rest. `verify` is replaced by each new check.

### Progress

A job in `GET /api/status` and `GET /api/jobs/<name>` carries `operation` (`sync`, `preview`, `check`, `restore` or `null`) and `progress_detail` (`null` when nothing is happening to it). Both come from the server's own state, so a page that was reloaded, or opened in another browser, shows the same bar. `progress_detail` has the same fields for every operation:

| Field | Meaning |
|---|---|
| `op`, `phase`, `label` | the operation, the phase it is in (`reading-source`, `reading-icloud`, `comparing`, `measuring`, `copying`, `tidying`, `verifying`, `checking`) and the phase in words |
| `files`, `bytes` | counted so far in this phase; they only go up, and start again from 0 in the next phase |
| `total_files`, `total_bytes` | known for a copy, a restore and a check; `null` while a folder is still being read |
| `estimated` | true when `total_files` is the number a finished preview counted, used as a guess while the source is read again (the bar never reaches 100 on a guess) |
| `percent` | `null` when no total is known, otherwise 0 to 100 |
| `path` | the end of the folder or file being worked on, short enough for a line |
| `elapsed_s`, `idle_s` | seconds since it started and since anything last happened |
| `stalled` | `idle_s` is 30 or more: the page says so in plain words and that it is not an error yet |
| `can_cancel` | `idle_s` is 300 or more: the page offers to stop it |
| `cancelling` | a stop was asked for and has not taken effect yet |

A stopped copy is recorded as `skipped` with `cancelled: true` and is not an alarm (no alert is sent). A stopped preview says so and changes nothing. A stopped check records no result. A stopped restore leaves the files it had finished in `.restored`, and no temp file.

### Preview next sync

`POST /api/jobs/<name>/preview` runs the engine in plan mode on the same worker as a sync, one job at a time. It walks the source and the iCloud folder exactly as a run does and applies the same guards in the same order (confirmation marker, live mount, shrink, size cap, free-disk reserve, the first-run rule), then reports what a sync started now would do. It never writes: it does not create the iCloud folder or the owner marker, does not remove leftover temp files, and touches no manifest, state file, history or activity log. Its answer is kept in memory only (lost on restart, and dropped when a real run finishes or the folder is changed or removed). `accept_shrink: true` (the JSON boolean only) previews what a run would do if a smaller source were accepted.

`preview` on a job (and `preview` in `GET .../preview`) is `null` or:

| Field | Meaning |
|---|---|
| `label`, `changed_nothing` | Always `"Preview"` and `true` |
| `at` | When the preview was made |
| `would_refuse`, `guard`, `result` | A guard would stop a sync: which one (`shrink`, `size cap`, `disk reserve`, `first-run`, `source`, `destination`, `anchor`, `mount`) and the result the run would report (`failed` or `skipped`) |
| `message` | One plain sentence. When a guard would refuse it is the same sentence a real run gives |
| `added`, `updated`, `removed`, `removed_how` | Counts of what would happen; `removed_how` is `moved to .deleted` or `removed` (plain mirror mode). All `0` when a guard would refuse |
| `bytes`, `files_total` | Bytes that would be copied; files in the source |
| `samples` | Up to 50 `{kind, path}` examples. The counts are complete |
| `shrink_note` | With `accept_shrink`, the sentence the shrink guard would have given |
| `first_run`, `left_alone`, `notes` | A safe first run (nothing already in iCloud is retired) and how many files it would leave alone |

A preview is a prediction from what Keepsake can see now: files can change before a real sync, and it copies and reads nothing back, so it cannot say whether a copy would fail. A job also carries `previewing` (a preview is waiting or running); a folder that is only previewing keeps its own `status`.

### Pause everything and quiet hours

`PUT /api/settings` accepts, alongside the other settings:

* `pause`: `null` to resume now; `{"preset": "1h"}`, `{"preset": "tomorrow"}` (tomorrow at 08:00) or `{"preset": "1w"}`; or `{"until": "2026-11-10T08:00"}`. A time without an offset is read in `TZ`, like the schedules; the time must be in the future and at most a year ahead. Anything else is a `400` with a sentence and changes nothing.
* `quiet_hours`: `null` to switch off, or `{"start": "22:00", "end": "07:00"}`, a daily window in `TZ` that may cross midnight. Start and end must differ.

While paused or inside quiet hours, scheduled syncs and scheduled copy checks do not start, scheduled work that was only waiting is dropped, and nothing is reported as late or silent. A sync that is already running is never interrupted. Sync now and Preview still work when asked, and a run that fails is still reported as failed. `next_run` and `next_check` are never earlier than the resume time.

The `pause` object in `GET /api/status` and `GET /api/settings`: `paused` (a pause or quiet window is in effect), `kind` (`pause`, `quiet` or empty), `until` (when scheduled work may start again; a pause that ends inside the quiet window waits for the window to end), `pause_until` (the pause itself, or `null`), `quiet` (`start` and `end`, or `null`), `in_quiet`, `message` (a plain sentence), `timezone`. A pause that ran out is cleared by the worker and its end is remembered, so the paused time is not counted towards the "no successful run" alert or the late-folder list; quiet hours are likewise not counted.

### Guided setup

`setup` in `GET /api/status` is `{"complete": bool, "step": "confirm" | "folder" | "preview" | "sync-check" | "complete", "existing": bool}`. While `complete` is false the page shows only the setup wizard and the REST API answers only these calls, for a session and for the bearer token alike: `GET /api/status`, `GET /api/browse`, `POST /api/icloud/confirm`, `POST /api/jobs`, `POST` and `GET /api/jobs/<name>/preview`, `POST /api/jobs/<name>/sync`, `POST /api/jobs/<name>/verify`, `POST /api/jobs/<name>/remove` for a folder that has not made a copy yet, and `POST /api/logout`. Everything else, including any route added later, is refused with `403` and `{"ok": false, "message": "finish the guided setup first: until it is done Keepsake only allows the setup steps"}`. The setup is complete when the three first-run steps are true, and then it stays complete: `step` is `complete` and nothing is refused. `existing` is true for an install that already had the confirmed iCloud folder and a folder when the service first started: it was never locked. While the setup is open, a new folder's first copy waits for `POST /api/jobs/<name>/sync` instead of starting by itself, so the preview comes first. The record is `setup.json` in the data folder.

### First run

`first_run` in `GET /api/status` is `{"steps": [{"id": "confirm" | "folder" | "sync-check", "done": bool}, ...], "done": bool}`. Each step is derived from what is really there: the confirmation marker in the iCloud folder, at least one stored folder, and a folder whose last run finished and whose last copy check matched. Nothing a client sends can set it.

Optional alert events (webhook POST body or MQTT message; ntfy, Pushover and e-mail send the same event as text):
`{"event": "failed" | "recovered" | "stale" | "test", "job": "...", "message": "...", "at": "..."}`. A check that finds a difference is a `failed` event with `result: "mismatch"`; a scheduled one starts its message with "Scheduled check: ".

### MQTT status

With `KEEPSAKE_MQTT_HOST` and `KEEPSAKE_MQTT_DISCOVERY_PREFIX` set (`<prefix>` below), Keepsake keeps one connection to the broker and publishes, all retained (`KEEPSAKE_MQTT_PREFIX`, default `keepsake`, is `<topic>` below):

| Topic | Holds |
|---|---|
| `<topic>/availability` | `online` every 30 seconds while it runs; the last-will message is `offline`, and a clean stop publishes `offline` itself |
| `<topic>/status/<folder>` | JSON for one folder (the name lower-cased, anything but a to z and 0 to 9 becomes `_`): `status` (`ok`, `failed`, `running`, `queued`, `paused`, `never`, `needs attention`), `problem`, `last_success`, `last_run`, `next_run`, `files_copied`, `bytes_copied`, `duration_s` (seconds the last run took), `history` (the last 14 days: `day`, `outcome`, and when the day has a detail `runs`, `files`, `removed`, `bytes`, `duration_s`, `at`), `icloud_bytes`, `check` (`match`, `mismatch` or `never`), `evicted`, `reason` |
| `<topic>/status/summary` | `{"problem": bool, "confirmed": bool, "text": "...", "folders": [{"folder", "reason"}]}`: `problem` is on while any folder needs a person, with the sentence; `confirmed` is whether the iCloud folder has been confirmed |
| `<prefix>/<component>/keepsake_<folder>_<field>/config`, `.../keepsake_problem/config`, `.../keepsake_attention/config` | MQTT discovery for the device "Keepsake for iCloud" (the folder's status entity also carries every field of its status topic as attributes): per folder `status`, `last_copy`, `next_run`, `files_copied`, `bytes_copied`, `icloud_size`, `check`, `evicted` (sensors) and `problem` (binary sensor), and for the install `keepsake_problem`, `keepsake_attention` and `keepsake_icloud_confirmed` |

Only changes are published; a reconnect publishes everything again. A folder that is removed has its discovery and status topics emptied, including after a restart (the announced folders are kept in `mqtt-known.json` in the data folder). The alert events above are separate and are published to `<topic>/event` and `<topic>/job/<folder>` whether or not discovery is on.
