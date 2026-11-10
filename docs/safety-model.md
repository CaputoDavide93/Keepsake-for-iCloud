# 🛡️ Safety model

The engine ([`src/keepsake/engine.py`](../src/keepsake/engine.py)) judges a run
from the content it left behind, never from its own bookkeeping. Each guard below
refuses the whole run before anything is copied or removed.

| # | Guard | Result |
|---|---|---|
| 0 | The iCloud folder holds the one-time marker `.keepsake-anchor` (written only by the explicit confirm step, never as a placeholder) | `skipped` |
| 1 | The source location is a live mount: a folder with visible content, the root of a mounted filesystem when listed in `KEEPSAKE_FS_ROOTS`, holding its marker file when one is set. Checked again before anything is retired | `skipped` |
| 2 | The source does not resolve outside its root | `failed` |
| 3 | The source is not empty, fully readable, and has not shrunk in files, bytes or remembered paths (200 sampled) by more than `KEEPSAKE_SHRINK_PCT` since the last good run. Only the JSON boolean `true` can accept a shrink | `failed` |
| 4 | With no history (first run, lost manifest, job re-added) the run is a safe first run: copies and verifies, retires nothing. A non-empty destination folder without Keepsake's owner marker is refused unless the job sets `adopt_existing: true`; one owned by another job is always refused. A folder holding only `.deleted`, `.removed` or `.restored` content counts as populated | `failed` |
| 5 | The folder would not pass its size cap, and the Mac would keep its free-disk reserve | `failed` |

Also:

* The destination is untrusted. Source and destination are walked and written by
  directory file descriptors opened without following links, one step at a time;
  `.deleted` and `.removed` must be real folders; temp files are created
  exclusively under random names; a replaced file is moved aside only after its
  replacement exists, so a failed copy leaves the old version.
* A file that changes while it is read makes the run `failed` and is retried.
* Files named like keys or secrets (`.env`, `*.pem`, `*.key`, `id_rsa*` and
  similar) are skipped and reported, and a source path containing words such as
  `secret`, `password`, `.ssh` or `token` is refused. There is no switch to allow
  it. **Nothing secret belongs in iCloud through this tool.**
* After a run the destination is read back: file count, size and a sha256 sample
  (up to 5 files of 256 MB or less, copied or unchanged).

## Trust features

* **Check a copy** reads up to 40 files (at most 256 MB each, 1 GB in all) chosen from the manifest the last good run left, opens each on both sides by directory file descriptor without following links (a link, folder or pipe in place of a file is a mismatch, never read through), and compares size and sha256. It opens everything read-only and writes nothing on either side. A file edited at the source since the run, one macOS evicted, or one too big is skipped and counted, not called a match. A check that compared nothing is refused. A check is refused while its job is copying or queued, and its result is thrown away if a run finishes while it reads. A mismatch makes the folder "Needs attention" until a later check passes.
* **Scheduled check** runs the same check on the worker, one at a time and only when nothing is copying or queued, so it never overlaps a copy of any folder. Each check carries on after the last file the previous one looked at (an order fixed by a per-folder seed and a cursor in the state file), so every file is examined after about files / 40 checks. Skipped files (edited since the run, evicted, over 256 MB, or past the 1 GB per-check budget, which is picked up first by the next check) are examined but not compared: coverage means "looked at", not "proved identical". A check that could compare nothing is retried in six hours, not reported as a match or a mismatch.
* **What iCloud state is visible.** It reads the iCloud folder on this Mac: evicted placeholders (`.<name>.icloud`) are counted per folder, read by directory fd without following links and without writing. A file whose size or time disagrees with the manifest is already caught by the next run and by Check a copy. It does not detect an iCloud folder that stopped changing, and it has no macOS-only check (`brctl`, `xattr`).
* **Alerts** go only through channels configured in the environment. A secret is never logged, returned by the API or included in a test alert's error text, and an e-mail password is never sent without STARTTLS or SSL. A failed channel never stops a copy. The stale alert is stored in the state file, so it is sent once per outage, not on every poll or restart.
* **Restore all** (one date, or everything kept) copies into `.restored/<date>/` with the same guards as restoring one file: never the source, never over an existing file (a taken name gets a number), links and folders reached through links are skipped and named, each file is written under a temp name and linked into place. The whole batch is judged before anything is written: more than 5000 files, the folder's size cap and the free-disk reserve each refuse it as a whole. The dated folder must be a real date, and every kept name must be plain (no `..`, empty part, NUL or `/`). If the count has changed since the person confirmed, it is refused. A file that cannot be copied is skipped with its reason and the rest carry on; a batch that restores nothing is refused, never reported as a success.
* **Deleted files** lists `.deleted/<date>/` by directory file descriptor. Links are never followed or listed, and only folders named like a date are read.
* **Restore** copies one kept file to `<dest>/.restored/<date>/<same path>` inside the job's iCloud folder. It never writes to the source. The source is mounted read-only by design and Keepsake writes only inside its own iCloud folder, so a restore into the source would break that rule, and the next run would mirror the file straight back. The kept original stays in `.deleted`. A name that is taken gets a number (`name (2).ext`); nothing is replaced. The path is refused if it is absolute or holds `..`, an empty part or a NUL; the file is opened through no-follow steps and must be a plain file; the restore is refused when it would pass the folder's size cap or the free-disk reserve, or when the folder is not marked as this job's. `.restored` is reserved like `.deleted`: it must be a real folder, the mirror ignores it, it counts towards the size cap, and it is never pruned automatically, because it holds files you asked for.
* A destination folder that holds several jobs' folders carries `.keepsake-group`, written only when Keepsake creates that folder or is told with an explicit tick to take over one it did not create. A folder without it, and without that tick, is refused (at Add and again by the engine), and one that holds a single job's files directly (`.keepsake-owner`) is never used as a destination for others.
* A source whose top level holds a folder or file named `.deleted`, `.removed`, `.restored` or `.keepsake-owner` is refused before anything is copied, so a source file can never land on Keepsake's own folders (a restored file included).
* **The guided setup lock** keeps a new install on the safe path: until the iCloud folder is confirmed, a folder is chosen, its first copy was previewed, made and checked, the page is only the wizard and the REST API refuses everything outside it (an allow list, so a new route is locked by default). A folder's first copy waits for the person, so it never runs before the preview. It is read from what is really there and never from a flag, an existing install is never locked, and once complete it stays complete.
* **Preview next sync** runs the engine's plan mode: the same code that decides a real run (`_plan_sync`), so the guards fire in the same order with the same sentences, but called read-only. It opens the source and the iCloud folder by directory file descriptor without following links, never creates the iCloud folder (a folder that does not exist yet reads as empty), never writes the owner marker, never removes a leftover temp file, and never touches a manifest, the state file, the history, the jobs file or the activity log. Its result is held in memory. It runs on the worker, one job at a time, and is refused while that folder is copying or waiting. It is a prediction: it does not copy or read anything back, so it cannot know whether a copy will fail, and files can change before a real sync. Previewing with `accept_shrink` shows what a smaller source would move to `.deleted`; only the JSON boolean `true` counts, as for a sync. A test makes every write call (create, delete, rename, write-mode open) raise during a preview and compares the iCloud folder and the data folder byte for byte and time for time before and after.
* **Diagnostics** (`GET /api/diagnostics`) are built from counts, times, states and channel names, and every log line and error sentence is redacted last: configured secret values, URLs (cut to scheme and host), anything after `token=`, `password=` or `Authorization:`, and the home folder (`~`). Per-file log warnings and tracebacks keep no file name or message, and nothing from inside your folders is listed beyond what the status API shows. `tests/test_diagnostics.py` sets every secret to a canary and fails if one appears.
* **Progress and stop** (`progress_detail`, `POST /api/jobs/<name>/cancel`) are numbers and one short folder name: nothing in them reaches a file. A stop is honoured between files and between blocks of a file, never in the middle of a write: a copy is a temp file renamed into place and a stopped one is removed, the source is only ever read, and a stopped restore leaves the files it had finished in `.restored`. A read that the operating system is stuck in cannot be interrupted and ends when the share answers; the page says so. A stopped copy is recorded as skipped, not as a failure, and sends no alert.
* **Pause everything and quiet hours** stop only what the schedule starts. Scheduled syncs and scheduled copy checks do not start, scheduled work that was merely waiting for its turn is dropped, and nothing is called late or silent while it lasts. A sync that is running is never interrupted, and what a person asks for (Sync now, Preview) still runs: a pause is not a lock. The time a pause or the daily quiet window covers is not counted towards the late-folder list or the "no successful run" alert, so a week-long pause does not raise an alert on the first morning back. A folder that fails, a copy check that finds a difference and a run that was refused are still reported while paused. A pause is at most a year long and is stored in `settings.json`, so a restart does not end it.
* **What changed** records counts and up to 50 example paths per run in the job's state. The activity log holds the counts only, no file names. **Per-night detail** keeps, for each of the last 14 days, that day's counts and up to 50 example paths (a path is cut at 300 characters, an error sentence at 300) in the state file, next to the outcome; two copying runs on one day add up. It is bounded on write and again on read, a damaged entry loses only its detail, and a night recorded before this existed has none and says so.

## Threat model

The mount points and `KEEPSAKE_*` settings are set by whoever runs Keepsake and
are trusted. Inside them nothing is followed through a symlink. Someone who can
already write to the iCloud folder or the source at the same moment as a run is
a risk to those files that Keepsake cannot remove.

## Accepted residual risks

* The shrink guard allows a loss up to the percentage in one run, so a stale view
  holding 80% or more of the files and bytes passes (lower the percentage if that
  worries you; deleted files stay for the retention days).
* A wrong or empty iCloud folder is caught by the one-time confirmation, not by
  anything Keepsake can see about iCloud itself.
* A swap that lands between two directory steps of one run is refused by the
  kernel only at the step it hits, so one run can stop half way and report
  `failed`; a job removal checks the owner marker and the folder identity but
  cannot make its final rename atomic with that check; size accounting reads by
  path.
* Unchanged files are trusted by size and modification time; a file corrupted
  without changing either is caught only if the sample picks it.
* Not exercised with real services: a real network-share unmount while running,
  and a real iCloud upload.
