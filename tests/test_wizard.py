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
