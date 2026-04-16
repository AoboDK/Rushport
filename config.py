"""
Configuration loader — reads config.yaml and provides typed access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


_DEFAULT_CONFIG_PATH = Path("config.yaml")
_DEFAULT_MS_CLIENT_ID = "6750d115-92e4-4196-b69a-d9cc7c1fb214"


class Config:
    def __init__(self, data: dict):
        self._data = data

    # Rushfiles
    @property
    def rf_email(self) -> str:
        return self._data.get("rushfiles", {}).get("email", "")

    @property
    def rf_password(self) -> str:
        return self._data.get("rushfiles", {}).get("password", "")

    @property
    def rf_clientgateway_base(self) -> str:
        return self._data.get("rushfiles", {}).get(
            "clientgateway_base", ""
        )

    @property
    def rf_filecache_base(self) -> str:
        return self._data.get("rushfiles", {}).get(
            "filecache_base", ""
        )

    # Microsoft
    @property
    def ms_client_id(self) -> str:
        raw = self._data.get("microsoft", {}).get("client_id", "")
        return raw or _DEFAULT_MS_CLIENT_ID

    @property
    def ms_tenant_id(self) -> str:
        return self._data.get("microsoft", {}).get("tenant_id", "common")

    # Transfer
    @property
    def concurrency(self) -> int:
        return int(self._data.get("transfer", {}).get("concurrency", 4))

    @property
    def chunk_size_mb(self) -> int:
        return int(self._data.get("transfer", {}).get("chunk_size_mb", 10))

    @property
    def retry_attempts(self) -> int:
        return int(self._data.get("transfer", {}).get("retry_attempts", 3))

    @property
    def retry_delay_s(self) -> float:
        return float(self._data.get("transfer", {}).get("retry_delay_s", 5))

    # State DB
    @property
    def db_path(self) -> Path:
        raw = self._data.get("state", {}).get("db_path", "~/.rushport/state.db")
        return Path(raw).expanduser()


class ConfigError(Exception):
    pass


def load_config(path: Path = _DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        raise ConfigError(
            f"config.yaml not found. Copy config.yaml.example to config.yaml and fill in your values."
        )
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    cfg = Config(data)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    missing = []
    if not cfg.rf_clientgateway_base:
        missing.append("rushfiles.clientgateway_base")
    # rf_filecache_base is optional — auto-discovered from fullprofile.FilecacheUrls at runtime
    if missing:
        raise ConfigError(
            f"config.yaml is missing required fields: {', '.join(missing)}\n"
            f"See config.yaml.example for reference."
        )
