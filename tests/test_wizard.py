import pytest
import yaml
from config import ConfigError
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
