"""GitHub OAuth device flow and credential lifecycle."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import (
    AUTH_METHOD_OAUTH,
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_ACCOUNT_ID,
    CONF_AUTH_METHOD,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
    DATA_CREDENTIAL_LOCKS,
    OAUTH_DEVICE_CODE_URL,
    OAUTH_REFRESH_MARGIN_SECONDS,
    OAUTH_SCOPE,
    OAUTH_TIMEOUT_SECONDS,
    OAUTH_TOKEN_URL,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_CREDENTIAL_KEYS = (
    CONF_AUTH_METHOD,
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
    CONF_ACCOUNT_ID,
)


class OAuthError(Exception):
    """A safe, user-presentable GitHub OAuth error."""


class ReauthenticationRequired(Exception):
    """The stored credentials can no longer be refreshed."""


async def async_start_reauth_flow(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Open Home Assistant's reauthentication flow for this config entry."""
    entry.async_start_reauth(hass)


async def async_get_account_id(session: Any, token: str) -> str:
    """Read the immutable GitHub user ID without requesting repository access."""
    try:
        async with session.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        ) as response:
            if response.status == 401:
                raise ReauthenticationRequired("GitHub could not verify this account.")
            response.raise_for_status()
            payload = await response.json()
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
        raise OAuthError("Could not verify the GitHub account.") from err
    account_id = payload.get("id") if isinstance(payload, Mapping) else None
    if type(account_id) is not int or account_id <= 0:
        raise OAuthError("GitHub returned an invalid account identity.")
    return str(account_id)


def validate_pat(token: str) -> str:
    """Normalize a PAT and reject the unsupported classic token format."""
    normalized = token.strip()
    if not normalized:
        raise ValueError("Enter a fine-grained personal access token.")
    if normalized.startswith("ghp_"):
        raise ValueError(
            "Classic personal access tokens are not supported. "
            "Use a fine-grained token with Copilot Requests permission."
        )
    return normalized


async def request_device_code(session: Any, client_id: str) -> dict[str, Any]:
    """Request a GitHub device code without exposing response credentials."""
    if not client_id:
        raise OAuthError("The project GitHub OAuth app is not configured.")

    try:
        async with session.post(
            OAUTH_DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": OAUTH_SCOPE},
            headers={"Accept": "application/json"},
            timeout=15,
        ) as response:
            response.raise_for_status()
            payload = await response.json()
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
        raise OAuthError("Could not contact GitHub to start sign-in.") from err

    if not isinstance(payload, Mapping):
        raise OAuthError("GitHub returned an invalid device authorization.")
    required = ("device_code", "user_code", "verification_uri", "expires_in")
    if any(not payload.get(key) for key in required):
        raise OAuthError("GitHub returned an incomplete device authorization.")
    if any(not isinstance(payload[key], str) for key in required[:3]):
        raise OAuthError("GitHub returned an invalid device authorization.")
    try:
        expires_in = int(payload["expires_in"])
        raw_interval = int(payload.get("interval", 5))
    except (TypeError, ValueError) as err:
        raise OAuthError("GitHub returned an invalid device authorization.") from err
    if expires_in <= 0 or raw_interval < 0:
        raise OAuthError("GitHub returned an invalid device authorization.")
    interval = max(1, raw_interval)
    if not payload["verification_uri"].startswith("https://github.com/"):
        raise OAuthError("GitHub returned an unexpected verification URL.")
    return {**payload, "expires_in": expires_in, "interval": interval}


async def poll_for_device_token(
    session: Any,
    *,
    client_id: str,
    device_code: str,
    expires_in: int,
    interval: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll GitHub at its required interval until authorization completes."""
    deadline = monotonic() + min(expires_in, OAUTH_TIMEOUT_SECONDS)
    poll_interval = max(interval, 1)

    while monotonic() < deadline:
        await sleep(min(poll_interval, max(deadline - monotonic(), 0)))
        if monotonic() >= deadline:
            break
        try:
            async with session.post(
                OAUTH_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
                timeout=15,
            ) as response:
                payload = await response.json()
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
            raise OAuthError("Could not contact GitHub to complete sign-in.") from err

        if not isinstance(payload, Mapping):
            raise OAuthError("GitHub returned an invalid sign-in response.")
        if payload.get("access_token"):
            return _credential_data(AUTH_METHOD_OAUTH, payload)

        match payload.get("error"):
            case "authorization_pending":
                continue
            case "slow_down":
                try:
                    server_interval = int(payload.get("interval", 0))
                except (TypeError, ValueError) as err:
                    raise OAuthError(
                        "GitHub returned an invalid sign-in response."
                    ) from err
                poll_interval = max(poll_interval + 5, server_interval)
            case "access_denied":
                raise OAuthError("GitHub sign-in was denied.")
            case "expired_token" | "token_expired":
                raise OAuthError("The GitHub device code expired. Start again.")
            case "incorrect_client_credentials" | "device_flow_disabled":
                raise OAuthError(
                    "The project GitHub OAuth app has invalid device-flow settings."
                )
            case None:
                raise OAuthError("GitHub did not return an access token.")
            case _:
                raise OAuthError("GitHub could not authorize this sign-in.")

    raise OAuthError("The GitHub device code expired. Start again.")


def _credential_data(auth_method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the secret-bearing config-entry data for a successful login."""
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("GitHub did not return an access token.")

    credentials: dict[str, Any] = {
        CONF_AUTH_METHOD: auth_method,
        CONF_ACCESS_TOKEN: access_token,
    }
    if auth_method == AUTH_METHOD_OAUTH and "expires_in" not in payload:
        raise OAuthError("GitHub returned an incomplete token response.")
    if refresh_token := payload.get("refresh_token"):
        if not isinstance(refresh_token, str):
            raise OAuthError("GitHub returned an invalid refresh token.")
        credentials[CONF_REFRESH_TOKEN] = refresh_token
    if "expires_in" in payload:
        expires_in = payload["expires_in"]
        try:
            access_lifetime = int(expires_in)
        except (TypeError, ValueError) as err:
            raise OAuthError("GitHub returned an invalid token lifetime.") from err
        if access_lifetime <= 0:
            raise OAuthError("GitHub returned an invalid token lifetime.")
        credentials[CONF_ACCESS_TOKEN_EXPIRES_AT] = int(time.time()) + access_lifetime
    if "refresh_token_expires_in" in payload:
        refresh_expires_in = payload["refresh_token_expires_in"]
        try:
            refresh_lifetime = int(refresh_expires_in)
        except (TypeError, ValueError) as err:
            raise OAuthError(
                "GitHub returned an invalid refresh-token lifetime."
            ) from err
        if refresh_lifetime <= 0:
            raise OAuthError("GitHub returned an invalid refresh-token lifetime.")
        credentials[CONF_REFRESH_TOKEN_EXPIRES_AT] = int(time.time()) + refresh_lifetime
    return credentials


async def refresh_oauth_token(
    session: Any, *, client_id: str, refresh_token: str
) -> dict[str, Any]:
    """Rotate a GitHub OAuth access/refresh token pair."""
    try:
        async with session.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        ) as response:
            payload = await response.json()
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
        raise OAuthError("Could not contact GitHub to renew sign-in.") from err

    if not isinstance(payload, Mapping):
        raise OAuthError("GitHub returned an invalid token refresh response.")
    if payload.get("error"):
        raise ReauthenticationRequired(
            "GitHub sign-in expired. Reauthenticate in Home Assistant."
        )
    return _credential_data(AUTH_METHOD_OAUTH, payload)


class CredentialStore:
    """Refresh and persist credentials for one Home Assistant config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client_id: str) -> None:
        self.hass = hass
        self.entry = entry
        self.client_id = client_id
        self._refresh_lock = hass.data.setdefault(DATA_CREDENTIAL_LOCKS, {}).setdefault(
            entry.entry_id, asyncio.Lock()
        )

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing expiring OAuth tokens."""
        async with self._refresh_lock:
            while True:
                data = dict(self.entry.data)
                expiry = data.get(CONF_ACCESS_TOKEN_EXPIRES_AT)
                if data.get(CONF_AUTH_METHOD) != AUTH_METHOD_OAUTH or (
                    expiry is not None
                    and expiry > time.time() + OAUTH_REFRESH_MARGIN_SECONDS
                ):
                    token = data.get(CONF_ACCESS_TOKEN)
                    if not token:
                        raise ReauthenticationRequired("Sign in to GitHub again.")
                    return token

                refresh_token = data.get(CONF_REFRESH_TOKEN)
                refresh_expiry = data.get(CONF_REFRESH_TOKEN_EXPIRES_AT)
                if (
                    not refresh_token
                    or not self.client_id
                    or (refresh_expiry is not None and refresh_expiry <= time.time())
                ):
                    raise ReauthenticationRequired(
                        "GitHub sign-in expired. Reauthenticate in Home Assistant."
                    )

                from homeassistant.helpers.aiohttp_client import (
                    async_get_clientsession,
                )

                try:
                    rotated = await refresh_oauth_token(
                        async_get_clientsession(self.hass),
                        client_id=self.client_id,
                        refresh_token=refresh_token,
                    )
                except (OAuthError, ReauthenticationRequired):
                    if self._credentials_changed(data):
                        continue
                    raise
                # Reauth can replace credentials while GitHub is rotating the old pair.
                if self._credentials_changed(data):
                    continue
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, **rotated}
                )
                return rotated[CONF_ACCESS_TOKEN]

    def _credentials_changed(self, snapshot: Mapping[str, Any]) -> bool:
        """Reject responses belonging to a superseded sign-in."""
        return any(
            snapshot.get(key) != self.entry.data.get(key) for key in _CREDENTIAL_KEYS
        )

    async def async_ensure_account_id(self) -> str:
        """Bind legacy entries to their existing account before allowing reauth."""
        if account_id := self.entry.data.get(CONF_ACCOUNT_ID):
            return account_id
        token = await self.async_get_access_token()
        async with self._refresh_lock:
            if account_id := self.entry.data.get(CONF_ACCOUNT_ID):
                return account_id
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            account_id = await async_get_account_id(
                async_get_clientsession(self.hass), token
            )
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_ACCOUNT_ID: account_id}
            )
            return account_id
