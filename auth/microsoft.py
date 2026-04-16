"""
Microsoft OneDrive authentication via MSAL — Device Code flow.

Supports BOTH personal OneDrive and OneDrive for Business (Microsoft 365):
  - Personal: @outlook.com, @hotmail.com, @live.com accounts
  - Business: any Azure AD / Microsoft 365 work or school account

Both account types use the same Microsoft Graph API endpoint (me/drive),
so no code changes are needed to switch between them — MSAL handles it
transparently when tenant_id = "common".

Flow:
  1. App requests a device code from Microsoft
  2. User visits aka.ms/devicelogin and enters the code (one-time browser step)
  3. App polls until user completes auth
  4. MSAL caches tokens locally; silent refresh from then on

The Microsoft Azure public client ID used here is the well-known
"Microsoft Azure CLI" app which has Files.ReadWrite.All delegated scope
available without custom app registration. For production, register your
own Azure AD app and replace client_id in config.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import msal

_TOKEN_CACHE_PATH = Path("~/.rushport/msal_token_cache.json").expanduser()
_SCOPES = ["Files.ReadWrite.All"]


class MicrosoftAuthError(Exception):
    pass


class MicrosoftAuth:
    """
    Manages Microsoft/OneDrive tokens via MSAL with persistent token cache.

    Usage:
        auth = MicrosoftAuth(client_id="...", tenant_id="common")
        await auth.ensure_authenticated()          # triggers device code if needed
        token = await auth.get_access_token()      # always valid
    """

    def __init__(
        self,
        client_id: str,
        tenant_id: str = "common",
        cache_path: Path = _TOKEN_CACHE_PATH,
    ):
        self._client_id = client_id
        self._tenant_id = tenant_id
        self._cache_path = cache_path
        self._token_cache = msal.SerializableTokenCache()
        self._app: Optional[msal.PublicClientApplication] = None
        self._load_cache()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ensure_authenticated(self, console_print=None) -> None:
        """
        Attempt silent auth first; fall back to device code flow.
        console_print: callable for printing device code instructions (defaults to print).
        """
        printer = console_print or print
        app = self._get_app()

        # Try silent first
        token = self._acquire_silent(app)
        if token:
            return

        # Device code flow
        flow = app.initiate_device_flow(scopes=_SCOPES)
        if "user_code" not in flow:
            raise MicrosoftAuthError(
                f"Failed to start device code flow: {flow.get('error_description', flow)}"
            )

        printer(flow["message"])  # "To sign in, visit https://aka.ms/devicelogin and enter code XXXXXXXX"

        result = app.acquire_token_by_device_flow(flow)
        self._handle_token_result(result)
        self._save_cache()

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing silently if needed."""
        app = self._get_app()
        token = self._acquire_silent(app)
        if token:
            return token["access_token"]
        raise MicrosoftAuthError(
            "No valid token. Run `rushport auth onedrive` to authenticate."
        )

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_access_token()}"}

    def is_authenticated(self) -> bool:
        """Check whether we have any cached account."""
        app = self._get_app()
        return len(app.get_accounts()) > 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_app(self) -> msal.PublicClientApplication:
        if self._app is None:
            authority = f"https://login.microsoftonline.com/{self._tenant_id}"
            self._app = msal.PublicClientApplication(
                client_id=self._client_id,
                authority=authority,
                token_cache=self._token_cache,
            )
        return self._app

    def _acquire_silent(
        self, app: msal.PublicClientApplication
    ) -> Optional[dict]:
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(scopes=_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            self._save_cache()  # persist any refreshed tokens
            return result
        return None

    @staticmethod
    def _handle_token_result(result: dict) -> None:
        if "error" in result:
            raise MicrosoftAuthError(
                f"Authentication failed: {result.get('error_description', result['error'])}"
            )
        if "access_token" not in result:
            raise MicrosoftAuthError(f"Unexpected token response: {result}")

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._token_cache.deserialize(self._cache_path.read_text())
            except Exception:
                # Corrupt cache — start fresh
                self._cache_path.unlink(missing_ok=True)

    def _save_cache(self) -> None:
        if self._token_cache.has_state_changed:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(self._token_cache.serialize())
            self._cache_path.chmod(0o600)
