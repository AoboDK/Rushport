# rushport — Rushfiles cloud migration

`rushport` is a command-line tool for moving files between
[Rushfiles](https://www.rushfiles.com/) and other cloud storage providers.
Today it migrates from Rushfiles to Microsoft OneDrive; the roadmap is a
full cloud-to-cloud migration suite centered on Rushfiles — both directions,
multiple destinations.

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

## Features

- OAuth Authorization Code + PKCE login for Rushfiles (headless Chromium via Playwright)
- Device-code login for OneDrive (works for both personal and work/school accounts)
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
  client_id: "04b07795-8ddb-461a-bbee-02f9e1bf7b46"   # Azure CLI public client
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

## How it works

```
  auth.rushfiles.com  ──OAuth AuthCode+PKCE──▶  JWT (initial login via Playwright)
  clientgateway.<tenant>  ─── metadata / file tree
  filecache01.<tenant>    ─── streaming bytes
                                  │
                             (no temp files)
                                  ▼
  Destination cloud (today: Microsoft Graph / OneDrive upload sessions)
```

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

- **Initial Rushfiles login requires a real Chromium** (via Playwright). Pure-HTTP
  replay of the auth POST returns 500 — likely bot-fingerprinting on their side.
  Token refresh (30-day refresh token) is pure HTTP and does not need the browser.
- **No path-length guard.** OneDrive's ~400-char path limit may bite deep trees.
- **No test suite yet.** Contributions welcome.
- **`list-shares` can return empty** on some Rushfiles accounts (`ManagedShares`
  comes back empty from `fullprofile`). Workaround: pass the share ID explicitly
  to `rushport run <share-id>`.

## License

[MIT](LICENSE) — free and open source. Fork, modify, redistribute, sell,
whatever you want.
