# Interactive CLI Wizard — Design Spec

**Date:** 2026-06-10
**Status:** Approved

## Summary

Replace the Tkinter GUI (`gui.py`) with a terminal-native interactive wizard (`wizard.py`) that guides any user from zero to completed transfer without manual config editing or knowledge of individual CLI subcommands.

---

## Architecture

### Files changed

| Action | File | Notes |
|---|---|---|
| **Add** | `wizard.py` | New guided wizard — orchestrates existing modules, no logic duplication |
| **Delete** | `gui.py` | Tkinter GUI removed entirely |
| **Modify** | `pyproject.toml` | Update entry points, add `questionary>=2.0` dependency |
| **No change** | `cli.py`, `config.py`, `auth/*`, `clients/*`, `sync/*`, `models/*`, `destinations/*` | All existing logic untouched |

### Entry points (pyproject.toml)

```toml
rushport     → wizard:main      # default: launches the guided wizard
rushport-cli → cli:app          # power users / automation / scripting
# rushport-gui removed
```

### New dependency

`questionary>=2.0` — provides arrow-key selection, masked password input, confirm prompts, and text inputs. No other new dependencies.

---

## Wizard Flow

The wizard runs steps 1–6 in order. Each step checks its own skip condition before executing.

### Step 1 — RushFiles Setup
**Skip if:** `config.yaml` exists and `load_config()` succeeds without error.

**If partial config** (file exists but fails validation due to missing fields): re-enters only the missing fields; does not wipe the file.

Prompts:
- RushFiles `clientgateway_base` URL (required)
- RushFiles `filecache_base` URL (optional — auto-discovered at runtime if blank)
- Microsoft app mode: `[Shared rushport app]` (default) or `[My own Entra app]`
  - If own app: prompts `client_id` and `tenant_id`
- Transfer settings shown with defaults pre-filled (concurrency=4, chunk_size_mb=10, retry_attempts=3, retry_delay_s=5) — Enter to accept

Writes `config.yaml` on completion.

### Step 2 — RushFiles Authentication
**Skip if:** valid cached token found in `~/.rushport/`.

Prompts:
- Email (plain text input; pre-fills from `config.yaml` if set)
- Password (masked input via `questionary.password`)

Calls existing `RushfilesAuth.login()`. On failure: prints error in red with recovery hint, re-prompts credentials.

### Step 3 — OneDrive Authentication
**Skip if:** valid cached token found in `~/.rushport/`.

Runs existing `MicrosoftAuth.ensure_authenticated()` device-code flow. Prints the URL and code with a "Press Enter once you've completed sign-in" gate. On failure: prints error in red.

### Step 4 — Share Selection
**Always runs.**

Fetches shares via `RushfilesClient.list_shares()`. If zero shares returned: prints the known workaround tip and prompts for a manual share ID.

Presents an arrow-key `questionary.select` list:
```
? Choose a share to transfer:
  ❯ Acme Corp — Sales Documents  [share-id-1]
    Acme Corp — HR Files          [share-id-2]
    ── Transfer all shares ──
```

If **"Transfer all shares"** is selected: Step 5 skips the destination folder prompt (each share gets a folder named after itself, matching current `rushport run` behavior) and the dry-run / concurrency options still apply to all shares.

### Step 5 — Transfer Options
**Always runs.**

Prompts:
- Destination folder name in OneDrive (default: sanitized share name — Enter to accept)
- Dry run? `[y/N]` (default: no)
- Override concurrency? (default: value from config — Enter to accept)

Displays a confirmation summary before proceeding:
```
  Share:       Sales Documents
  Destination: /Sales Documents (OneDrive)
  Dry run:     No
  Concurrency: 4
```

### Step 6 — Transfer
**Always runs.**

Calls `SyncEngine.run()` with live Rich output (unchanged from current CLI behavior). On completion: prints the existing status table.

**After completion**, prompts: `Transfer another share? [y/N]`
- Yes → jumps back to Step 4
- No → prints a farewell line and exits

---

## Config Management

- `config.yaml` is written/updated in the working directory (where `load_config()` already looks)
- Partial updates preserve existing keys — only missing/prompted fields are overwritten
- Transfer defaults are pre-filled in the wizard prompts; user presses Enter to accept without needing to know the values
- Auth tokens continue to live in `~/.rushport/` — no changes to token storage
- `config.yaml` remains git-ignored; no credentials are committed

---

## UX & Output Style

### Visual conventions

| Element | Treatment |
|---|---|
| Step headers | `[bold cyan]── Step N: Title ──[/]` printed via Rich |
| Skipped steps | `[dim green]✓ Config already set up[/]` — single line |
| Errors | `[red]Error message[/]` + plain-English recovery hint |
| Interactive prompts | `questionary` (arrow-key, password, confirm, text) |
| Transfer progress | Existing Rich output from `SyncEngine` — unchanged |

### Keyboard / interrupts

- `Ctrl+C` at any `questionary` prompt exits cleanly with a short message — no Python traceback shown to the user
- All prompts have sensible defaults so power users can move through quickly

---

## What Is Not Changing

- All `auth/*`, `sync/*`, `clients/*`, `destinations/*`, `models/*` modules — untouched
- `cli.py` — untouched; accessible via `rushport-cli` for automation
- `config.py` — untouched
- SQLite state at `~/.rushport/state.db` — untouched
- Token cache paths — untouched

---

## Out of Scope

- Google Drive, S3, or any destination other than OneDrive
- `list-files` debug command (remains available via `rushport-cli list-files`)
- Status/resume commands in the wizard (accessible via `rushport-cli status` / `rushport-cli resume`)
