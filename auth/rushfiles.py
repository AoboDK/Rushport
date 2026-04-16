"""
Rushfiles authentication — Playwright-driven Authorization Code + PKCE flow.

The Rushfiles web client uses client_id="js" with Authorization Code + PKCE,
routing credentials through domainauth.{tenant} for whitelabel login.
The login step is done through a real Chromium (headless by default) because
pure-HTTP replays of the POST to domainauth.{tenant}/Account/Login return 500.

Flow:
  1. Launch Chromium and navigate to auth.rushfiles.com/connect/authorize?...
     with our PKCE challenge and a redirect_uri we control.
  2. Chromium follows the redirect chain to domainauth.{tenant}/Account/Login.
  3. Fill #Username / #Password, click the login button.
  4. Wait for navigation to redirect_uri?code=... and capture the code.
  5. POST auth.rushfiles.com/connect/token with code + PKCE verifier.

Set RF_AUTH_HEADED=1 to run Chromium visibly (useful for debugging).

Token lifetime: access token 1h, refresh token 30d. Auto-refresh is pure HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx

_TOKEN_CACHE_PATH = Path("~/.rushport/rushfiles_token.json").expanduser()
_REFRESH_BUFFER_S = 300
_AUTH_BASE = "https://auth.rushfiles.com"
_CLIENT_ID = "js"
_SCOPE = "openid profile domain_api offline_access"


class RushfilesAuthError(Exception):
    pass


class RushfilesTokens:
    def __init__(self, access_token: str, refresh_token: str, expires_at: float):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at

    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - _REFRESH_BUFFER_S)

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RushfilesTokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=data["expires_at"],
        )


class RushfilesAuth:
    """
    Manages Rushfiles tokens via headless OAuth Authorization Code + PKCE.

    Usage:
        auth = RushfilesAuth(
            clientgateway_base="https://clientgateway.your-domain.com",
            tenant="your-domain.com",
        )
        await auth.login(email, password)
        headers = await auth.auth_headers()
    """

    def __init__(
        self,
        clientgateway_base: str = "https://clientgateway.your-domain.com",
        tenant: str = "",
        cache_path: Path = _TOKEN_CACHE_PATH,
    ):
        self._cg_base = clientgateway_base.rstrip("/")
        # Derive tenant from clientgateway URL if not explicitly provided
        # e.g. "https://clientgateway.your-domain.com" -> "your-domain.com"
        if not tenant and clientgateway_base:
            host = urllib.parse.urlparse(clientgateway_base).netloc
            parts = host.split(".", 1)
            self._tenant = parts[1] if len(parts) > 1 else host
        else:
            self._tenant = tenant
        self._cache_path = cache_path
        self._tokens: Optional[RushfilesTokens] = None
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def login(self, email: str, password: str) -> None:
        """Authenticate headlessly and cache the tokens."""
        self._tokens = await self._headless_login(email, password)
        self._save_cache()

    async def load_cached(self) -> bool:
        """Load tokens from disk. Returns True if valid tokens exist."""
        if not self._cache_path.exists():
            return False
        try:
            data = json.loads(self._cache_path.read_text())
            self._tokens = RushfilesTokens.from_dict(data)
            if self._tokens.is_expired():
                if self._tokens.refresh_token:
                    await self._refresh()
                else:
                    self._tokens = None
                    self._cache_path.unlink(missing_ok=True)
                    raise RushfilesAuthError(
                        "Session expired. Please run `rushport auth rushfiles` again."
                    )
            return True
        except RushfilesAuthError:
            raise
        except Exception:
            return False

    async def auth_headers(self) -> dict[str, str]:
        if self._tokens is None:
            raise RushfilesAuthError(
                "Not authenticated. Run `rushport auth rushfiles` first."
            )
        if self._tokens.is_expired():
            # Serialize refreshes so concurrent workers don't all fire _refresh()
            # simultaneously with the same refresh token (server rejects duplicates).
            async with self._refresh_lock:
                if self._tokens.is_expired():  # re-check inside the lock
                    await self._refresh()
        return {"Authorization": f"Bearer {self._tokens.access_token}"}

    @property
    def access_token(self) -> str:
        if self._tokens is None:
            raise RushfilesAuthError("Not authenticated.")
        return self._tokens.access_token

    # ------------------------------------------------------------------
    # Headless login
    # ------------------------------------------------------------------

    async def _headless_login(self, email: str, password: str) -> RushfilesTokens:
        redirect_uri = f"https://{self._tenant}/client/Signedin.html"
        code_verifier = _pkce_verifier()
        code_challenge = _pkce_challenge(code_verifier)
        state = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")

        authorize_url = (
            f"{_AUTH_BASE}/connect/authorize?"
            + urllib.parse.urlencode({
                "client_id": _CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": _SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "acr_values": (
                    f"tenant:{self._tenant} "
                    "deviceId: deviceName:rushport deviceType:WebClient "
                    "deviceOs:Windows latitude: longitude:"
                ),
            })
        )

        code = await _browser_login(
            authorize_url=authorize_url,
            redirect_uri=redirect_uri,
            email=email,
            password=password,
            headed=os.environ.get("RF_AUTH_HEADED") == "1",
        )

        # Exchange code for tokens (pure HTTP)
        async with httpx.AsyncClient(timeout=30) as session:
            token_resp = await session.post(
                f"{_AUTH_BASE}/connect/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": _CLIENT_ID,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )

        if token_resp.status_code >= 400:
            try:
                error = token_resp.json().get("error_description") or token_resp.text
            except Exception:
                error = token_resp.text
            raise RushfilesAuthError(f"Token exchange failed: {error}")

        data = token_resp.json()
        if "access_token" not in data:
            raise RushfilesAuthError(f"No access token in response: {data}")

        return RushfilesTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=time.time() + data.get("expires_in", 3600),
        )

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def _refresh(self) -> None:
        if not self._tokens or not self._tokens.refresh_token:
            self._tokens = None
            self._cache_path.unlink(missing_ok=True)
            raise RushfilesAuthError(
                "Session expired. Please run `rushport auth rushfiles` again."
            )

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_AUTH_BASE}/connect/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": _CLIENT_ID,
                    "refresh_token": self._tokens.refresh_token,
                    "scope": _SCOPE,
                },
                timeout=30,
            )

        if resp.status_code in (400, 401):
            self._tokens = None
            self._cache_path.unlink(missing_ok=True)
            raise RushfilesAuthError(
                "Session expired. Please run `rushport auth rushfiles` again."
            )

        resp.raise_for_status()
        data = resp.json()
        self._tokens = RushfilesTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", self._tokens.refresh_token),
            expires_at=time.time() + data.get("expires_in", 3600),
        )
        self._save_cache()

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _save_cache(self) -> None:
        if self._tokens is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._tokens.to_dict(), indent=2))
        self._cache_path.chmod(0o600)


# ------------------------------------------------------------------
# PKCE helpers
# ------------------------------------------------------------------

def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _extract_form_post(form_html: str) -> tuple[str, dict[str, str]]:
    """Extract action URL and all input values from an auto-submit form."""
    action_m = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', form_html, re.IGNORECASE)
    if not action_m:
        return "", {}
    action = html.unescape(action_m.group(1))
    fields: dict[str, str] = {}
    for m in re.finditer(r'<input[^>]+>', form_html, re.IGNORECASE):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            fields[name_m.group(1)] = html.unescape(val_m.group(1)) if val_m else ""
    return action, fields


def _extract_hidden_fields(form_html: str) -> dict[str, str]:
    """Extract all hidden input fields from a form."""
    fields: dict[str, str] = {}
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', form_html, re.IGNORECASE):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            fields[name_m.group(1)] = html.unescape(val_m.group(1)) if val_m else ""
    return fields


def _extract_csrf(html: str) -> str:
    """Extract __RequestVerificationToken from a login form."""
    match = re.search(
        r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"', html
    )
    if match:
        return match.group(1)
    # alternate attribute order
    match = re.search(
        r'<input[^>]+value="([^"]+)"[^>]+name="__RequestVerificationToken"', html
    )
    return match.group(1) if match else ""


def _extract_code(url: str) -> str:
    """Extract the authorization code from a redirect URL."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    return qs.get("code", [""])[0]


# ------------------------------------------------------------------
# Playwright-driven login
# ------------------------------------------------------------------

async def _save_diag(page, tag: str) -> None:
    """Save a screenshot and page HTML for debugging. Never raises."""
    import tempfile
    ss = Path(tempfile.gettempdir()) / f"rf_pw_{tag}.png"
    html_path = Path(tempfile.gettempdir()) / f"rf_pw_{tag}.html"
    try:
        await page.screenshot(path=str(ss), full_page=True)
        html_path.write_text(await page.content(), encoding="utf-8")
        print(f"  [playwright] saved {ss} and {html_path}")
    except Exception as e:
        print(f"  [playwright] diag save failed: {e}")

async def _browser_login(
    authorize_url: str,
    redirect_uri: str,
    email: str,
    password: str,
    headed: bool = False,
    timeout_ms: int = 60000,
) -> str:
    """
    Drive a real Chromium through the Rushfiles authorize flow and return the
    authorization code from the final redirect to redirect_uri.

    Raises RushfilesAuthError on any failure.
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError as e:
        raise RushfilesAuthError(
            "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
        ) from e

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            # Capture any request that targets our redirect_uri — it carries ?code=...
            captured_code: dict[str, str] = {}

            def _check_url(url: str) -> None:
                if redirect_uri.split("?")[0] in url:
                    code = _extract_code(url)
                    if code and "code" not in captured_code:
                        captured_code["code"] = code

            page.on("request", lambda req: _check_url(req.url))
            page.on("framenavigated", lambda frame: _check_url(frame.url) if frame == page.main_frame else None)

            try:
                await page.goto(authorize_url, timeout=timeout_ms, wait_until="domcontentloaded")
            except PWTimeout:
                pass  # May already be on Signedin.html with the code

            if "code" not in captured_code:
                # Step 1: userprompt page (email-only, IdP detection). The form has #Username
                # and a button[name=button][value=login] labeled "Next" / "Næste".
                try:
                    await page.wait_for_selector("#Username", timeout=timeout_ms)
                except PWTimeout:
                    raise RushfilesAuthError(
                        f"Login form did not appear. Current URL: {page.url[:300]}"
                    )

                print(f"  [playwright] step1 page: {page.url[:200]}")
                # Accept cookie banner if present (some servers treat missing consent as a bot signal)
                try:
                    await page.click("button.accept-policy", timeout=1500)
                except Exception:
                    pass
                await page.fill("#Username", email)
                await page.click('button[name="button"][value="login"]')

                # Step 2: domainauth login page — has #Username + #Password. Wait for
                # Password to appear (indicates federation redirect has completed).
                try:
                    await page.wait_for_selector("#Password", timeout=timeout_ms, state="visible")
                except PWTimeout:
                    await _save_diag(page,"step2_no_password")
                    raise RushfilesAuthError(
                        f"#Password field did not appear after userprompt step. URL: {page.url[:300]}"
                    )
                print(f"  [playwright] step2 page: {page.url[:200]}")

                # Accept cookie banner on domainauth host too if present
                try:
                    await page.click("button.accept-policy", timeout=1500)
                except Exception:
                    pass
                # Username is pre-filled (readonly) by the userprompt step — only fill Password
                await page.fill("#Password", password)

                # Click login and wait for the redirect chain to hit redirect_uri
                try:
                    async with page.expect_request(
                        lambda req: redirect_uri.split("?")[0] in req.url,
                        timeout=timeout_ms,
                    ) as req_info:
                        await page.click('button[name="button"][value="login"]')
                    req = await req_info.value
                    _check_url(req.url)
                except PWTimeout:
                    await _save_diag(page,"step2_no_redirect")
                    raise RushfilesAuthError(
                        f"Never reached redirect_uri after login. Current URL: {page.url[:300]}"
                    )

            if "code" not in captured_code:
                raise RushfilesAuthError(
                    f"Login completed but no authorization code was captured. Current URL: {page.url[:300]}"
                )

            return captured_code["code"]
        finally:
            await browser.close()
