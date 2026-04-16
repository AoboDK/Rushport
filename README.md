# rushport — migration utility for authorized cloud exports

> Unofficial interoperability tool for migrating data you are authorized to access. Not affiliated with, endorsed by, or supported by RushFiles A/S or Microsoft.

`rushport` is a command-line migration utility for moving files between
services when you are authorized to access the source data.
Today it supports transfers from [RushFiles](https://www.rushfiles.com/) to
Microsoft OneDrive.

File bytes stream directly from Rushfiles' FileCache into the destination's
upload API — nothing is buffered to local disk. Transfer state lives in SQLite
so interrupted runs resume cleanly. Original `createdDateTime` and
`lastModifiedDateTime` are preserved on both files and folders.

## Supported providers

| Direction | Status |
|---|---|
| Rushfiles → OneDrive (personal / business) | ✅ Shipping |
| Rushfiles → Google Drive | 🗺️ Planned |
| Rushfiles → S3 / local filesystem | 🗺️ Planned |
| OneDrive / Google Drive → Rushfiles | 🗺️ Planned |

New destinations plug in through `destinations/<name>.py`; see
["Adding a new destination"](#adding-a-new-destination) below.

Use of third-party services remains subject to their own terms, policies, and trademarks.

## Features

- RushFiles sign-in support and OneDrive device-code sign-in
- Concurrent uploads with configurable fan-out
- Chunked upload sessions for files ≥ 4 MB (320 KiB-aligned chunks)
- Preserves timestamps on files **and** folders
- 429 / `Retry-After` handling for Microsoft Graph
- Post-upload size verification to catch truncated transfers
- Dry-run, resume, reset, multi-share batch, per-share status view
- Filename sanitization for OneDrive-forbidden characters and reserved names

## Requirements

- Python **3.11+**
- Chromium (installed once via `playwright install chromium`)
- A Rushfiles account with access to the share(s) you want to migrate
- A destination cloud account (currently: Microsoft personal or work/school)

Primarily tested on Windows. Linux / macOS should work but Playwright launch
args may need tuning if Chromium fails to start.

## Install

```bash
git clone https://github.com/<your-username>/rushport.git
cd rushport
pip install -e .
python -m playwright install chromium
```

## Configure

Copy the template and fill in your Rushfiles tenant URLs:

```bash
cp config.yaml.example config.yaml
```

```yaml
rushfiles:
  email: ""              # optional; prompted at runtime if blank
  password: ""           # optional; prompted at runtime if blank
  clientgateway_base: "https://clientgateway.<your-tenant>"
  filecache_base: "https://filecache01.<your-tenant>"

microsoft:
  client_id: "<your-app-registration-client-id>"
  tenant_id: "common"

transfer:
  concurrency: 4
  chunk_size_mb: 10
  retry_attempts: 3
  retry_delay_s: 5

state:
  db_path: "~/.rushport/state.db"
```

`config.yaml` is git-ignored — your credentials never get committed.

Recommended for production use: register your own Microsoft Entra application
and request only the Microsoft Graph permissions required for your transfer
scenario (least privilege).

> **Finding your Rushfiles tenant URLs:** the client gateway and file cache are
> tenant-specific. Your Rushfiles admin or portal will have them; they're
> typically `clientgateway.<your-org>.<tld>` and `filecache01.<your-org>.<tld>`.

## Use

```bash
# Sign in (tokens are cached under ~/.rushport/)
rushport auth rushfiles
rushport auth onedrive

# See what Rushfiles shares you can access
rushport list-shares

# Transfer a share to OneDrive
rushport run <share-id>

# Scan only — no uploads
rushport run <share-id> --dry-run

# Resume an interrupted transfer
rushport resume <share-id>

# Progress overview
rushport status
rushport status --failed     # detail of failed files
```

Run `rushport --help` for the full command list.

Security note: authentication tokens are stored locally in the user profile
directory. On Unix-like systems, cache files are written with user-only
permissions where supported. Use endpoint security and disk encryption on
shared systems.

## How it works

At a high level, `rushport`:

1. Authenticates to configured source and destination services
2. Scans the source tree and records transfer state in local SQLite
3. Streams source bytes directly to destination upload APIs (no local temp files)
4. Applies post-transfer metadata updates (including timestamp preservation)

`SyncEngine` runs three phases:

1. **Scan** — walks the Rushfiles share tree, records every file/folder in SQLite as `pending`
2. **Transfer** — workers pull `pending` rows, stream FileCache → destination, flip to `done`/`failed`
3. **Folder dates** — after uploads finish, patches `fileSystemInfo` on every directory
   so the destination shows original timestamps (OneDrive bumps folder mtime whenever a
   file lands in it, so this step has to come last)

State lives at `~/.rushport/state.db`. Delete it to start a share fresh.

## Adding a new destination

Drop a factory into `destinations/<name>.py` returning `(Auth, Client)` where the
client exposes `ensure_folder(path)` and `upload_file(path, iterator, total_size)`
matching the `GraphClient` signatures, then wire it into `cli.py` as a
`--destination` option. See `destinations/onedrive.py` for the reference
implementation.

Reverse-direction destinations (any-cloud → Rushfiles) will follow the same
extension point with a `source` counterpart.

## Limitations

- **Initial RushFiles login requires Chromium** (via Playwright in current implementation).
- **No path-length guard.** OneDrive's ~400-char path limit may bite deep trees.
- **No test suite yet.** Contributions welcome.
- **`list-shares` can return empty** on some Rushfiles accounts (`ManagedShares`
  comes back empty from `fullprofile`). Workaround: pass the share ID explicitly
  to `rushport run <share-id>`.

## License

[MIT](LICENSE)-licensed. You may use, modify, and redistribute this code under
the MIT License. Third-party services remain subject to their own terms and
trademarks.
