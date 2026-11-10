# 🍎 Setting up on a Mac

iCloud Drive appears as a normal folder only on macOS, at
`~/Library/Mobile Documents/com~apple~CloudDocs`. Keepsake copies into a folder
named `Keepsake` inside it and macOS uploads whatever lands there. Everything
below was written against fixture folders; **it has not been run against a real
iCloud Drive account**, so check each step in Finder.

## 1. Make the Keepsake folder

In Finder open iCloud Drive and create a folder named `Keepsake`. Its path is:

```text
~/Library/Mobile Documents/com~apple~CloudDocs/Keepsake
```

Keepsake writes only inside this folder. Do not put your own files in it.

## 2. Docker Desktop: share the folders

1. Docker Desktop, Settings, Resources, File sharing. Make sure the iCloud Drive
   folder and the folders you want to keep are inside a shared location (your
   home folder is shared by default on current versions; add others if not).
2. The first time a container touches iCloud Drive or another protected folder,
   macOS may ask whether Docker may access it. Allow it. If you refused earlier:
   System Settings, Privacy & Security, Files and Folders (or Full Disk Access),
   and switch Docker on.
3. Copy `.env.example` to `.env` and set:

```bash
KEEPSAKE_PASSWORD=pick-something-of-10-or-more-characters
KEEPSAKE_ICLOUD_HOST_DIR=/Users/<your-name>/Library/Mobile Documents/com~apple~CloudDocs/Keepsake
KEEPSAKE_SOURCES_HOST_DIR=/Users/<your-name>/Documents
```

   Replace <your-name> with your macOS user name. Paths with spaces are fine in `.env`.

4. `docker compose up -d --build`, then open <http://localhost:8765>.

The compose file mounts your sources **read-only** at `/sources` and the
Keepsake folder read-write at `/icloud`. To keep several folders, either mount a
parent and choose subfolders in the page, or name several roots:

```bash
KEEPSAKE_ROOTS=documents=/sources/documents,photos=/sources/photos
```

and add one read-only mount for each in `docker-compose.yml`.

## 3. Confirm the iCloud folder (once)

If iCloud Drive was not available when Docker started the container, Docker may
create an empty `/icloud` that is not iCloud at all. So Keepsake copies nothing
until a person confirms the folder: Settings, Locations tab, **Confirm iCloud folder**, after
checking in Finder that `Keepsake` is there. Keepsake then writes one small file,
`.keepsake-anchor`, and refuses to run any job where that file is missing.

## Adding more locations from the page (optional)

A container cannot mount a new folder by itself, so this works in two parts. You mount
a broad parent folder **read-only** under `/hostfs` once, in a
`docker-compose.override.yml` that stays out of git:

```yaml
services:
  keepsake:
    volumes:
      - type: bind
        source: /Users/<your-name>
        target: /hostfs/home
        read_only: true
      - type: bind
        source: /Volumes
        target: /hostfs/volumes
        read_only: true
```

Then, in Keepsake, open **Locations** (or use "Add location" beside "Where is it?" on the
Add folder page), choose folders under `/hostfs`, and give each a name such as
`HomeFolder`. The names you add are stored in the data volume (`locations.json`);
`KEEPSAKE_ROOTS` entries stay built in. Keepsake refuses a folder that is outside
`KEEPSAKE_LOCATION_PREFIXES` (default `/hostfs`), is reached through a symlink, is not on
a read-only mount, overlaps the iCloud or data folder or another location, or reuses a
name. Mount parents with care: a whole home folder includes keys and tokens, and it is
you who then chooses which folders under it Keepsake may copy.

## 4. Running natively instead of Docker

```bash
KEEPSAKE_DATA="$HOME/.keepsake" \
KEEPSAKE_ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Keepsake" \
KEEPSAKE_ROOTS="documents=$HOME/Documents" \
PYTHONPATH=src python3 -m keepsake
```

Needs Python 3.11 or newer. If you later run it as a background job (launchd),
macOS may block background programs from protected folders; grant the program
Full Disk Access, or keep using Docker Desktop, which holds its own permission.

## Network shares as sources

A mounted share can disappear while the container runs. Keepsake treats a source
that is empty, or that lost too much, as a refusal, not as a deletion. For extra
proof, set `KEEPSAKE_FS_ROOTS=name` (the source folder must be the root of a
mounted filesystem) and/or `KEEPSAKE_ROOT_MARKERS=name=.keepsake-root` with that
file placed at the share's root. If you mount a whole parent folder (such as
`/Volumes`) instead of one share, the container still starts when the share is
away and reports it as unavailable.

## What iCloud does afterwards

A finished run means the files are in the Keepsake folder on the Mac and match
the source. macOS uploads them later, and "Optimise Mac Storage" may replace a
file with a cloud-only placeholder; Keepsake leaves those alone while the source
is unchanged. iCloud Drive is not end-to-end encrypted unless your Apple Account
has Advanced Data Protection on.
