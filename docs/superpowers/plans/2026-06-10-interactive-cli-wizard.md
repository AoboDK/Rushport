# Interactive CLI Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `gui.py` with `wizard.py`, a terminal-native guided wizard that takes any user from zero config to completed OneDrive transfer with a single `rushport` command.

**Architecture:** `wizard.py` is a thin orchestration layer over the existing `config`, `auth`, `clients`, `sync`, and `destinations` modules. It runs 6 steps in order, each with its own skip condition. No existing module is modified. `gui.py` is deleted. `pyproject.toml` entry points are updated so `rushport` launches the wizard and `rushport-cli` exposes the original Typer subcommands.

**Tech Stack:** Python 3.11+, questionary>=2.0 (new), rich (existing), pytest>=8 + pytest-asyncio>=0.23 (dev/test only)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `wizard.py` | **Create** | Guided wizard — 6 step functions + `main()` entry point |
| `pyproject.toml` | **Modify** | Add questionary dep, update entry points, add dev extras |
| `gui.py` | **Delete** | Tkinter GUI removed |
| `tests/__init__.py` | **Create** | Empty — makes `tests/` a package |
| `tests/test_wizard.py` | **Create** | Unit tests for all 6 wizard step functions |

---

## Task 1: Update pyproject.toml and install

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Replace pyproject.toml content**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "rushport"
version = "0.1.0"
description = "Rushfiles cloud-to-cloud migration — move files between Rushfiles and other cloud providers, preserving timestamps"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "msal>=1.28",
    "pydantic>=2.0",
    "typer>=0.12",
    "rich>=13",
    "aiosqlite>=0.20",
    "pyyaml>=6",
    "playwright>=1.40",
    "questionary>=2.0",
]

[project.scripts]
rushport     = "wizard:main"
rushport-cli = "cli:app"

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[tool.hatch.build.targets.wheel]
packages = ["."]
```

- [ ] **Step 2: Install dependencies**

```bash
pip install -e ".[dev]"
```

Expected: installs `questionary`, `pytest`, `pytest-asyncio` with no errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add questionary dep, update entry points, add pytest dev extras"
```

---

## Task 2: Create wizard.py skeleton and test scaffolding

**Files:**
- Create: `wizard.py`
- Create: `tests/__init__.py`
- Create: `tests/test_wizard.py`

- [ ] **Step 1: Write failing tests**

Create `tests/__init__.py` as an empty file.

Create `tests/test_wizard.py`:

```python
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# Async generator helper used by transfer tests
async def async_items(*items):
    for item in items:
        yield item


def test_main_is_callable():
    from wizard import main
    assert callable(main)


def test_handle_interrupt_exits_cleanly():
    from wizard import _handle_interrupt
    with pytest.raises(SystemExit) as exc:
        _handle_interrupt()
    assert exc.value.code == 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_wizard.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'wizard'`

- [ ] **Step 3: Create wizard.py**

```python
"""rushport interactive wizard."""
from __future__ import annotations

import asyncio
from pathlib import Path

import questionary
import yaml
from rich.console import Console

from auth.microsoft import MicrosoftAuth, MicrosoftAuthError
from auth.rushfiles import RushfilesAuth, RushfilesAuthError
from clients.rushfiles import RushfilesClient
from config import Config, ConfigError, load_config
from destinations.onedrive import create_onedrive_client
from sync.engine import SyncEngine, SyncStats, _human_bytes
from sync.state import StateDB
from sync.transfer import sanitize_name

console = Console()
CONFIG_PATH = Path("config.yaml")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_step(n: int, title: str) -> None:
    console.print(f"\n[bold cyan]── Step {n}: {title} ──[/]")


def _print_skip(message: str) -> None:
    console.print(f"[dim green]✓ {message}[/]")


def _print_error(message: str, hint: str = "") -> None:
    console.print(f"[red]{message}[/]")
    if hint:
        console.print(f"[dim]{hint}[/]")


def _handle_interrupt() -> None:
    console.print("\n[dim]Wizard cancelled.[/]")
    raise SystemExit(0)


# ── Step stubs (filled in by subsequent tasks) ────────────────────────────────

def step_config_setup() -> Config:
    raise NotImplementedError


async def step_rf_auth(cfg: Config) -> RushfilesAuth:
    raise NotImplementedError


def step_ms_auth(cfg: Config) -> MicrosoftAuth:
    raise NotImplementedError


async def step_pick_share(cfg: Config, rf_auth: RushfilesAuth) -> list[tuple[str, str]]:
    raise NotImplementedError


def step_transfer_options(cfg: Config, shares: list[tuple[str, str]]) -> dict:
    raise NotImplementedError


async def step_run_transfer(cfg: Config, rf_auth: RushfilesAuth, options: dict) -> None:
    raise NotImplementedError


# ── Wizard loop ───────────────────────────────────────────────────────────────

async def _wizard() -> None:
    cfg = step_config_setup()
    rf_auth = await step_rf_auth(cfg)
    step_ms_auth(cfg)

    while True:
        shares = await step_pick_share(cfg, rf_auth)
        options = step_transfer_options(cfg, shares)

        confirmed = questionary.confirm("Start transfer?", default=True).ask()
        if confirmed is None or not confirmed:
            console.print("[dim]Transfer cancelled.[/]")
        else:
            await step_run_transfer(cfg, rf_auth, options)

        again = questionary.confirm("Transfer another share?", default=False).ask()
        if not again:
            console.print("\n[dim]All done. Goodbye![/]")
            break


def main() -> None:
    console.print("[bold cyan]rushport[/] — Rushfiles migration wizard\n")
    try:
        asyncio.run(_wizard())
    except KeyboardInterrupt:
        _handle_interrupt()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_wizard.py -v
```

Expected: PASS (2 tests: `test_main_is_callable`, `test_handle_interrupt_exits_cleanly`)

- [ ] **Step 5: Commit**

```bash
git add wizard.py tests/__init__.py tests/test_wizard.py
git commit -m "feat: add wizard.py skeleton with entry point and step stubs"
```

---

## Task 3: Implement step_config_setup()

**Files:**
- Modify: `wizard.py` (replace `step_config_setup` stub)
- Modify: `tests/test_wizard.py` (add 3 tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_wizard.py`:

```python
# ── step_config_setup ─────────────────────────────────────────────────────────

def test_config_setup_skips_when_config_valid(tmp_path, monkeypatch):
    """Returns immediately without prompting if load_config() succeeds."""
    monkeypatch.chdir(tmp_path)
    mock_cfg = MagicMock()
    with patch("wizard.load_config", return_value=mock_cfg):
        from wizard import step_config_setup
        result = step_config_setup()
    assert result is mock_cfg


def test_config_setup_writes_config_yaml(tmp_path, monkeypatch):
    """Writes config.yaml from prompts when it is missing."""
    monkeypatch.chdir(tmp_path)

    calls = [0]
    def fake_load():
        calls[0] += 1
        if calls[0] == 1:
            raise ConfigError("missing")
        return MagicMock()

    with patch("wizard.load_config", side_effect=fake_load), \
         patch("questionary.text") as mock_text, \
         patch("questionary.select") as mock_select:

        mock_text.return_value.ask.side_effect = [
            "https://cg.example.com",   # clientgateway_base
            "",                          # filecache_base
            "4",                         # concurrency
            "10",                        # chunk_size_mb
        ]
        mock_select.return_value.ask.return_value = "Shared rushport app (recommended)"

        from wizard import step_config_setup
        step_config_setup()

    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["rushfiles"]["clientgateway_base"] == "https://cg.example.com"
    assert data["transfer"]["concurrency"] == 4
    assert data["microsoft"]["client_id"] == ""


def test_config_setup_own_app_prompts_client_id(tmp_path, monkeypatch):
    """Prompts for client_id and tenant_id when 'My own Entra app' is chosen."""
    monkeypatch.chdir(tmp_path)

    calls = [0]
    def fake_load():
        calls[0] += 1
        if calls[0] == 1:
            raise ConfigError("missing")
        return MagicMock()

    with patch("wizard.load_config", side_effect=fake_load), \
         patch("questionary.text") as mock_text, \
         patch("questionary.select") as mock_select:

        mock_text.return_value.ask.side_effect = [
            "https://cg.example.com",   # clientgateway_base
            "",                          # filecache_base
            "my-client-id",              # client_id (own app)
            "my-tenant",                 # tenant_id (own app)
            "4",                         # concurrency
            "10",                        # chunk_size_mb
        ]
        mock_select.return_value.ask.return_value = "My own Entra app"

        from wizard import step_config_setup
        step_config_setup()

    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["microsoft"]["client_id"] == "my-client-id"
    assert data["microsoft"]["tenant_id"] == "my-tenant"
```

Also add `import yaml` at the top of `tests/test_wizard.py` (after the existing imports) and import `ConfigError`:

```python
import yaml
from config import ConfigError
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_wizard.py::test_config_setup_skips_when_config_valid tests/test_wizard.py::test_config_setup_writes_config_yaml tests/test_wizard.py::test_config_setup_own_app_prompts_client_id -v
```

Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Replace the step_config_setup stub in wizard.py**

```python
def step_config_setup() -> Config:
    try:
        cfg = load_config()
        _print_skip("Config already set up")
        return cfg
    except ConfigError:
        pass

    _print_step(1, "RushFiles Setup")

    existing: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            existing = yaml.safe_load(f) or {}

    rf = existing.get("rushfiles", {})
    ms = existing.get("microsoft", {})
    tr = existing.get("transfer", {})
    st = existing.get("state", {})

    clientgateway = questionary.text(
        "RushFiles clientgateway URL:", default=rf.get("clientgateway_base", "")
    ).ask()
    if clientgateway is None:
        _handle_interrupt()

    filecache = questionary.text(
        "RushFiles filecache URL (leave blank to auto-discover):",
        default=rf.get("filecache_base", ""),
    ).ask()
    if filecache is None:
        _handle_interrupt()

    app_mode = questionary.select(
        "Microsoft app mode:",
        choices=["Shared rushport app (recommended)", "My own Entra app"],
        default="Shared rushport app (recommended)",
    ).ask()
    if app_mode is None:
        _handle_interrupt()

    client_id = ""
    tenant_id = "common"
    if app_mode == "My own Entra app":
        client_id = questionary.text(
            "Microsoft Entra client ID:", default=ms.get("client_id", "")
        ).ask()
        if client_id is None:
            _handle_interrupt()
        tenant_id = questionary.text(
            "Tenant ID:", default=ms.get("tenant_id", "common")
        ).ask()
        if tenant_id is None:
            _handle_interrupt()

    concurrency = questionary.text(
        "Concurrent uploads:", default=str(tr.get("concurrency", 4))
    ).ask()
    if concurrency is None:
        _handle_interrupt()

    chunk_size_mb = questionary.text(
        "Chunk size (MB):", default=str(tr.get("chunk_size_mb", 10))
    ).ask()
    if chunk_size_mb is None:
        _handle_interrupt()

    data = {
        "rushfiles": {
            "email": rf.get("email", ""),
            "password": rf.get("password", ""),
            "clientgateway_base": clientgateway,
            "filecache_base": filecache,
        },
        "microsoft": {
            "client_id": client_id,
            "tenant_id": tenant_id,
        },
        "transfer": {
            "concurrency": int(concurrency),
            "chunk_size_mb": int(chunk_size_mb),
            "retry_attempts": int(tr.get("retry_attempts", 3)),
            "retry_delay_s": float(tr.get("retry_delay_s", 5)),
        },
        "state": {
            "db_path": st.get("db_path", "~/.rushport/state.db"),
        },
    }

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    console.print("[green]Config saved to config.yaml[/]")
    return load_config()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_wizard.py::test_config_setup_skips_when_config_valid tests/test_wizard.py::test_config_setup_writes_config_yaml tests/test_wizard.py::test_config_setup_own_app_prompts_client_id -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add wizard.py tests/test_wizard.py
git commit -m "feat: implement step_config_setup with interactive config prompts"
```

---

## Task 4: Implement step_rf_auth()

**Files:**
- Modify: `wizard.py` (replace `step_rf_auth` stub)
- Modify: `tests/test_wizard.py` (add 3 tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_wizard.py`:

```python
# ── step_rf_auth ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rf_auth_skips_when_cached():
    """Returns without prompting when a cached token loads successfully."""
    mock_auth = MagicMock()
    mock_auth.load_cached = AsyncMock(return_value=True)
    mock_cfg = MagicMock(rf_clientgateway_base="https://cg.example.com")

    with patch("wizard.RushfilesAuth", return_value=mock_auth):
        from wizard import step_rf_auth
        result = await step_rf_auth(mock_cfg)

    assert result is mock_auth
    mock_auth.load_cached.assert_called_once()


@pytest.mark.asyncio
async def test_rf_auth_prompts_and_logs_in():
    """Prompts for email and password then calls auth.login() when no cache."""
    mock_auth = MagicMock()
    mock_auth.load_cached = AsyncMock(return_value=False)
    mock_auth.login = AsyncMock()
    mock_cfg = MagicMock(rf_clientgateway_base="https://cg.example.com", rf_email="")

    with patch("wizard.RushfilesAuth", return_value=mock_auth), \
         patch("questionary.text") as mock_text, \
         patch("questionary.password") as mock_pw:

        mock_text.return_value.ask.return_value = "user@example.com"
        mock_pw.return_value.ask.return_value = "secret"

        from wizard import step_rf_auth
        result = await step_rf_auth(mock_cfg)

    mock_auth.login.assert_called_once_with("user@example.com", "secret")
    assert result is mock_auth


@pytest.mark.asyncio
async def test_rf_auth_retries_on_bad_password():
    """Re-prompts after RushfilesAuthError, succeeds on second attempt."""
    from auth.rushfiles import RushfilesAuthError

    mock_auth = MagicMock()
    mock_auth.load_cached = AsyncMock(return_value=False)
    mock_auth.login = AsyncMock(
        side_effect=[RushfilesAuthError("bad password"), None]
    )
    mock_cfg = MagicMock(rf_clientgateway_base="https://cg.example.com", rf_email="")

    with patch("wizard.RushfilesAuth", return_value=mock_auth), \
         patch("questionary.text") as mock_text, \
         patch("questionary.password") as mock_pw:

        mock_text.return_value.ask.return_value = "user@example.com"
        mock_pw.return_value.ask.return_value = "secret"

        from wizard import step_rf_auth
        await step_rf_auth(mock_cfg)

    assert mock_auth.login.call_count == 2
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_wizard.py::test_rf_auth_skips_when_cached tests/test_wizard.py::test_rf_auth_prompts_and_logs_in tests/test_wizard.py::test_rf_auth_retries_on_bad_password -v
```

Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Replace the step_rf_auth stub in wizard.py**

```python
async def step_rf_auth(cfg: Config) -> RushfilesAuth:
    auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)

    if await auth.load_cached():
        _print_skip("Already authenticated with RushFiles")
        return auth

    _print_step(2, "RushFiles Authentication")

    while True:
        email = questionary.text(
            "RushFiles email:", default=cfg.rf_email or ""
        ).ask()
        if email is None:
            _handle_interrupt()

        password = questionary.password("RushFiles password:").ask()
        if password is None:
            _handle_interrupt()

        try:
            await auth.login(email, password)
            console.print("[green]RushFiles authentication successful.[/]")
            return auth
        except RushfilesAuthError as e:
            _print_error(f"Authentication failed: {e}", "Please try again.")
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_wizard.py::test_rf_auth_skips_when_cached tests/test_wizard.py::test_rf_auth_prompts_and_logs_in tests/test_wizard.py::test_rf_auth_retries_on_bad_password -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add wizard.py tests/test_wizard.py
git commit -m "feat: implement step_rf_auth with credential prompts and retry loop"
```

---

## Task 5: Implement step_ms_auth()

**Files:**
- Modify: `wizard.py` (replace `step_ms_auth` stub)
- Modify: `tests/test_wizard.py` (add 2 tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_wizard.py`:

```python
# ── step_ms_auth ──────────────────────────────────────────────────────────────

def test_ms_auth_skips_when_authenticated():
    """Returns without running device-code flow when already authenticated."""
    mock_ms_auth = MagicMock()
    mock_ms_auth.is_authenticated.return_value = True
    mock_cfg = MagicMock(ms_client_id="", ms_tenant_id="common")

    with patch("wizard.MicrosoftAuth", return_value=mock_ms_auth):
        from wizard import step_ms_auth
        result = step_ms_auth(mock_cfg)

    assert result is mock_ms_auth
    mock_ms_auth.ensure_authenticated.assert_not_called()


def test_ms_auth_runs_device_code_flow():
    """Calls ensure_authenticated() when no valid token is cached."""
    mock_ms_auth = MagicMock()
    mock_ms_auth.is_authenticated.return_value = False
    mock_cfg = MagicMock(ms_client_id="", ms_tenant_id="common")

    with patch("wizard.MicrosoftAuth", return_value=mock_ms_auth):
        from wizard import step_ms_auth
        step_ms_auth(mock_cfg)

    mock_ms_auth.ensure_authenticated.assert_called_once()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_wizard.py::test_ms_auth_skips_when_authenticated tests/test_wizard.py::test_ms_auth_runs_device_code_flow -v
```

Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Replace the step_ms_auth stub in wizard.py**

```python
def step_ms_auth(cfg: Config) -> MicrosoftAuth:
    ms_auth = MicrosoftAuth(client_id=cfg.ms_client_id, tenant_id=cfg.ms_tenant_id)

    if ms_auth.is_authenticated():
        _print_skip("Already authenticated with OneDrive")
        return ms_auth

    _print_step(3, "OneDrive Authentication")
    console.print("Works with [cyan]personal OneDrive[/] and [cyan]OneDrive for Business[/].\n")

    try:
        ms_auth.ensure_authenticated(console_print=console.print)
        console.print("[green]OneDrive authentication successful.[/]")
        return ms_auth
    except MicrosoftAuthError as e:
        _print_error(
            f"OneDrive authentication failed: {e}",
            "Run `rushport-cli auth onedrive` to retry manually.",
        )
        raise SystemExit(1)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_wizard.py::test_ms_auth_skips_when_authenticated tests/test_wizard.py::test_ms_auth_runs_device_code_flow -v
```

Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add wizard.py tests/test_wizard.py
git commit -m "feat: implement step_ms_auth with device-code flow"
```

---

## Task 6: Implement step_pick_share()

**Files:**
- Modify: `wizard.py` (replace `step_pick_share` stub)
- Modify: `tests/test_wizard.py` (add 3 tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_wizard.py`:

```python
# ── step_pick_share ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_share_returns_selected_share():
    """Returns a single-element list with the (share_id, folder_name) chosen."""
    share = MagicMock()
    share.id = "abc-123"
    share.name = "Sales Docs"
    share.is_deleted = False

    mock_rf = AsyncMock()
    mock_rf.list_shares = AsyncMock(return_value=[share])
    mock_rf.__aenter__ = AsyncMock(return_value=mock_rf)
    mock_rf.__aexit__ = AsyncMock(return_value=None)

    mock_cfg = MagicMock(rf_clientgateway_base="https://cg.example.com", rf_filecache_base="")
    mock_rf_auth = MagicMock()

    with patch("wizard.RushfilesClient", return_value=mock_rf), \
         patch("questionary.select") as mock_select:

        mock_select.return_value.ask.return_value = ("abc-123", "Sales Docs")

        from wizard import step_pick_share
        result = await step_pick_share(mock_cfg, mock_rf_auth)

    assert result == [("abc-123", "Sales Docs")]


@pytest.mark.asyncio
async def test_pick_share_all_returns_every_non_deleted_share():
    """Selecting ALL returns all non-deleted shares."""
    shares = []
    for i, name in enumerate(["Share A", "Share B", "Share C"]):
        s = MagicMock()
        s.id = f"id-{i}"
        s.name = name
        s.is_deleted = False
        shares.append(s)

    mock_rf = AsyncMock()
    mock_rf.list_shares = AsyncMock(return_value=shares)
    mock_rf.__aenter__ = AsyncMock(return_value=mock_rf)
    mock_rf.__aexit__ = AsyncMock(return_value=None)

    mock_cfg = MagicMock(rf_clientgateway_base="https://cg.example.com", rf_filecache_base="")
    mock_rf_auth = MagicMock()

    with patch("wizard.RushfilesClient", return_value=mock_rf), \
         patch("questionary.select") as mock_select:

        mock_select.return_value.ask.return_value = "ALL"

        from wizard import step_pick_share
        result = await step_pick_share(mock_cfg, mock_rf_auth)

    assert len(result) == 3
    assert result[0][0] == "id-0"
    assert result[2][0] == "id-2"


@pytest.mark.asyncio
async def test_pick_share_manual_entry_when_no_shares():
    """Prompts for a manual share ID when list_shares() returns empty."""
    mock_rf = AsyncMock()
    mock_rf.list_shares = AsyncMock(return_value=[])
    mock_rf.__aenter__ = AsyncMock(return_value=mock_rf)
    mock_rf.__aexit__ = AsyncMock(return_value=None)

    mock_cfg = MagicMock(rf_clientgateway_base="https://cg.example.com", rf_filecache_base="")
    mock_rf_auth = MagicMock()

    with patch("wizard.RushfilesClient", return_value=mock_rf), \
         patch("questionary.text") as mock_text:

        mock_text.return_value.ask.return_value = "manual-share-id"

        from wizard import step_pick_share
        result = await step_pick_share(mock_cfg, mock_rf_auth)

    assert result == [("manual-share-id", "manual-share-id")]
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_wizard.py::test_pick_share_returns_selected_share tests/test_wizard.py::test_pick_share_all_returns_every_non_deleted_share tests/test_wizard.py::test_pick_share_manual_entry_when_no_shares -v
```

Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Replace the step_pick_share stub in wizard.py**

```python
async def step_pick_share(cfg: Config, rf_auth: RushfilesAuth) -> list[tuple[str, str]]:
    _print_step(4, "Choose Share")

    async with RushfilesClient(
        rf_auth,
        clientgateway_base=cfg.rf_clientgateway_base,
        filecache_base=cfg.rf_filecache_base,
    ) as rf:
        shares = await rf.list_shares()

    if not shares:
        console.print(
            "[yellow]No shares found automatically.[/]\n"
            "[dim]Tip: some accounts return empty shares from the profile endpoint.[/]"
        )
        share_id = questionary.text("Enter share ID manually:").ask()
        if share_id is None:
            _handle_interrupt()
        return [(share_id, share_id)]

    choices = [
        questionary.Choice(
            title=f"{s.name}  ({s.id})",
            value=(s.id, sanitize_name(s.name)),
        )
        for s in shares
        if not s.is_deleted
    ]
    choices.append(questionary.Choice(title="Transfer all shares", value="ALL"))

    selection = questionary.select(
        "Choose a share to transfer:", choices=choices
    ).ask()
    if selection is None:
        _handle_interrupt()

    if selection == "ALL":
        return [(s.id, sanitize_name(s.name)) for s in shares if not s.is_deleted]

    return [selection]
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_wizard.py::test_pick_share_returns_selected_share tests/test_wizard.py::test_pick_share_all_returns_every_non_deleted_share tests/test_wizard.py::test_pick_share_manual_entry_when_no_shares -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add wizard.py tests/test_wizard.py
git commit -m "feat: implement step_pick_share with arrow-key selection and manual fallback"
```

---

## Task 7: Implement step_transfer_options()

**Files:**
- Modify: `wizard.py` (replace `step_transfer_options` stub)
- Modify: `tests/test_wizard.py` (add 3 tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_wizard.py`:

```python
# ── step_transfer_options ─────────────────────────────────────────────────────

def test_transfer_options_single_share_prompts_dest():
    """Prompts for OneDrive destination folder when one share is selected."""
    mock_cfg = MagicMock(concurrency=4)

    with patch("questionary.text") as mock_text, \
         patch("questionary.confirm") as mock_confirm:

        mock_text.return_value.ask.side_effect = ["My Folder", "4"]
        mock_confirm.return_value.ask.return_value = False

        from wizard import step_transfer_options
        result = step_transfer_options(mock_cfg, [("share-1", "Default Name")])

    assert result["shares"] == [("share-1", "My Folder")]
    assert result["dry_run"] is False
    assert result["concurrency"] == 4


def test_transfer_options_all_shares_skips_dest_prompt():
    """Skips destination prompt and preserves folder names for all-shares mode."""
    mock_cfg = MagicMock(concurrency=4)
    shares = [("id-1", "Share A"), ("id-2", "Share B")]

    with patch("questionary.text") as mock_text, \
         patch("questionary.confirm") as mock_confirm:

        mock_text.return_value.ask.return_value = "4"
        mock_confirm.return_value.ask.return_value = False

        from wizard import step_transfer_options
        result = step_transfer_options(mock_cfg, shares)

    assert result["shares"] == shares
    assert mock_text.call_count == 1  # only concurrency, not destination


def test_transfer_options_dry_run_sets_flag():
    """dry_run is True in result when user confirms dry run."""
    mock_cfg = MagicMock(concurrency=4)

    with patch("questionary.text") as mock_text, \
         patch("questionary.confirm") as mock_confirm:

        mock_text.return_value.ask.side_effect = ["My Folder", "4"]
        mock_confirm.return_value.ask.return_value = True

        from wizard import step_transfer_options
        result = step_transfer_options(mock_cfg, [("share-1", "Name")])

    assert result["dry_run"] is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_wizard.py::test_transfer_options_single_share_prompts_dest tests/test_wizard.py::test_transfer_options_all_shares_skips_dest_prompt tests/test_wizard.py::test_transfer_options_dry_run_sets_flag -v
```

Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Replace the step_transfer_options stub in wizard.py**

```python
def step_transfer_options(cfg: Config, shares: list[tuple[str, str]]) -> dict:
    _print_step(5, "Transfer Options")

    transfer_all = len(shares) > 1

    if not transfer_all:
        share_id, default_folder = shares[0]
        dest = questionary.text(
            "Destination folder in OneDrive:", default=default_folder
        ).ask()
        if dest is None:
            _handle_interrupt()
        shares = [(share_id, dest)]

    dry_run = questionary.confirm(
        "Dry run (scan only, no uploads)?", default=False
    ).ask()
    if dry_run is None:
        _handle_interrupt()

    concurrency_str = questionary.text(
        "Concurrency:", default=str(cfg.concurrency)
    ).ask()
    if concurrency_str is None:
        _handle_interrupt()

    console.print("\n[bold]Summary:[/]")
    if transfer_all:
        console.print(f"  Shares:       {len(shares)} (all)")
    else:
        console.print(f"  Share:        {shares[0][1]}")
        console.print(f"  Destination:  /[cyan]{shares[0][1]}[/] (OneDrive)")
    console.print(f"  Dry run:      {'Yes' if dry_run else 'No'}")
    console.print(f"  Concurrency:  {concurrency_str}")

    return {
        "shares": shares,
        "dry_run": dry_run,
        "concurrency": int(concurrency_str),
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_wizard.py::test_transfer_options_single_share_prompts_dest tests/test_wizard.py::test_transfer_options_all_shares_skips_dest_prompt tests/test_wizard.py::test_transfer_options_dry_run_sets_flag -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add wizard.py tests/test_wizard.py
git commit -m "feat: implement step_transfer_options with summary preview"
```

---

## Task 8: Implement step_run_transfer()

**Files:**
- Modify: `wizard.py` (replace `step_run_transfer` stub)
- Modify: `tests/test_wizard.py` (add 2 tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_wizard.py`:

```python
# ── step_run_transfer ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_transfer_dry_run_walks_without_uploading():
    """In dry-run mode, walks files and prints counts; SyncEngine is never created."""
    fake_vf = MagicMock(is_file=True, size_bytes=2048)

    mock_rf = AsyncMock()
    mock_rf.walk = lambda share_id: async_items(("path/file.txt", fake_vf))
    mock_rf.__aenter__ = AsyncMock(return_value=mock_rf)
    mock_rf.__aexit__ = AsyncMock(return_value=None)

    mock_state_db = AsyncMock()
    mock_state_db.__aenter__ = AsyncMock(return_value=mock_state_db)
    mock_state_db.__aexit__ = AsyncMock(return_value=None)

    mock_cfg = MagicMock(
        ms_client_id="", ms_tenant_id="common", chunk_size_mb=10,
        rf_clientgateway_base="https://cg.example.com", rf_filecache_base="",
        db_path=MagicMock(), retry_attempts=3, retry_delay_s=5,
    )
    mock_rf_auth = MagicMock()
    options = {"shares": [("share-1", "Sales Docs")], "dry_run": True, "concurrency": 4}

    with patch("wizard.RushfilesClient", return_value=mock_rf), \
         patch("wizard.StateDB", return_value=mock_state_db), \
         patch("wizard.create_onedrive_client", return_value=(MagicMock(), MagicMock())), \
         patch("wizard.SyncEngine") as mock_engine_cls:

        from wizard import step_run_transfer
        await step_run_transfer(mock_cfg, mock_rf_auth, options)

    mock_engine_cls.assert_not_called()


@pytest.mark.asyncio
async def test_run_transfer_calls_sync_engine_for_each_share():
    """In live mode, creates SyncEngine and calls run() once per share."""
    mock_rf = AsyncMock()
    mock_rf.__aenter__ = AsyncMock(return_value=mock_rf)
    mock_rf.__aexit__ = AsyncMock(return_value=None)

    mock_state_db = AsyncMock()
    mock_state_db.__aenter__ = AsyncMock(return_value=mock_state_db)
    mock_state_db.__aexit__ = AsyncMock(return_value=None)

    mock_graph = AsyncMock()
    mock_graph.__aenter__ = AsyncMock(return_value=mock_graph)
    mock_graph.__aexit__ = AsyncMock(return_value=None)

    mock_engine = AsyncMock()
    mock_engine.run = AsyncMock()

    mock_cfg = MagicMock(
        ms_client_id="", ms_tenant_id="common", chunk_size_mb=10,
        rf_clientgateway_base="https://cg.example.com", rf_filecache_base="",
        db_path=MagicMock(), retry_attempts=3, retry_delay_s=5,
    )
    mock_rf_auth = MagicMock()
    options = {
        "shares": [("id-1", "Share A"), ("id-2", "Share B")],
        "dry_run": False,
        "concurrency": 4,
    }

    with patch("wizard.RushfilesClient", return_value=mock_rf), \
         patch("wizard.StateDB", return_value=mock_state_db), \
         patch("wizard.create_onedrive_client", return_value=(MagicMock(), mock_graph)), \
         patch("wizard.SyncEngine", return_value=mock_engine):

        from wizard import step_run_transfer
        await step_run_transfer(mock_cfg, mock_rf_auth, options)

    assert mock_engine.run.call_count == 2
    mock_engine.run.assert_any_call("id-1", "Share A", resume=False)
    mock_engine.run.assert_any_call("id-2", "Share B", resume=False)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_wizard.py::test_run_transfer_dry_run_walks_without_uploading tests/test_wizard.py::test_run_transfer_calls_sync_engine_for_each_share -v
```

Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Replace the step_run_transfer stub in wizard.py**

```python
async def step_run_transfer(cfg: Config, rf_auth: RushfilesAuth, options: dict) -> None:
    _print_step(6, "Transfer")

    _, graph_client = create_onedrive_client(
        client_id=cfg.ms_client_id,
        tenant_id=cfg.ms_tenant_id,
        chunk_size_mb=cfg.chunk_size_mb,
    )

    async with RushfilesClient(
        rf_auth,
        clientgateway_base=cfg.rf_clientgateway_base,
        filecache_base=cfg.rf_filecache_base,
    ) as rf_client:
        async with StateDB(cfg.db_path) as state_db:
            if options["dry_run"]:
                console.print("[yellow]Dry run — scanning only, no files will be transferred.[/]")
                for share_id, folder in options["shares"]:
                    stats = SyncStats()
                    async for _path, vf in rf_client.walk(share_id):
                        if vf.is_file:
                            stats.scanned += 1
                            stats.total_bytes += vf.size_bytes
                    console.print(
                        f"  [cyan]{folder}[/]: [bold]{stats.scanned}[/] files "
                        f"([bold]{_human_bytes(stats.total_bytes)}[/])"
                    )
                return

            async with graph_client:
                engine = SyncEngine(
                    rf_client=rf_client,
                    graph_client=graph_client,
                    state_db=state_db,
                    concurrency=options["concurrency"],
                    retry_attempts=cfg.retry_attempts,
                    retry_delay_s=cfg.retry_delay_s,
                )
                for share_id, folder in options["shares"]:
                    console.print(f"\n[bold]Share:[/] {folder}")
                    await engine.run(share_id, folder, resume=False)
```

Also update the `_wizard()` function to pass `rf_auth` to `step_run_transfer`:

```python
async def _wizard() -> None:
    cfg = step_config_setup()
    rf_auth = await step_rf_auth(cfg)
    step_ms_auth(cfg)

    while True:
        shares = await step_pick_share(cfg, rf_auth)
        options = step_transfer_options(cfg, shares)

        confirmed = questionary.confirm("Start transfer?", default=True).ask()
        if confirmed is None or not confirmed:
            console.print("[dim]Transfer cancelled.[/]")
        else:
            await step_run_transfer(cfg, rf_auth, options)

        again = questionary.confirm("Transfer another share?", default=False).ask()
        if not again:
            console.print("\n[dim]All done. Goodbye![/]")
            break
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_wizard.py::test_run_transfer_dry_run_walks_without_uploading tests/test_wizard.py::test_run_transfer_calls_sync_engine_for_each_share -v
```

Expected: PASS (2 tests)

- [ ] **Step 5: Run the full test suite to confirm nothing is broken**

```bash
pytest tests/ -v
```

Expected: PASS (all 18 tests)

- [ ] **Step 6: Commit**

```bash
git add wizard.py tests/test_wizard.py
git commit -m "feat: implement step_run_transfer; complete wizard step implementations"
```

---

## Task 9: Delete gui.py and verify entry points

**Files:**
- Delete: `gui.py`

- [ ] **Step 1: Delete gui.py**

```bash
git rm gui.py
```

- [ ] **Step 2: Confirm the wizard entry point works**

```bash
python -c "from wizard import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Confirm the cli entry point still works**

```bash
python -c "from cli import app; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Run the full test suite one final time**

```bash
pytest tests/ -v
```

Expected: PASS (all 18 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: remove gui.py — wizard.py is the new default entry point"
```

---

## Done

At this point:
- `rushport` launches the interactive wizard
- `rushport-cli` exposes the original subcommands for automation
- `gui.py` is gone
- All 16 tests pass
