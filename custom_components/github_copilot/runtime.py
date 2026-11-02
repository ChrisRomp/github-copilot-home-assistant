"""Local Copilot SDK runtime lifecycle."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .api_errors import is_authentication_error, is_quota_error
from .auth import (
    CredentialStore,
    OAuthError,
    ReauthenticationRequired,
    async_start_reauth_flow,
)
from .const import (
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_MODEL,
    CONF_MODEL_BILLING,
    DOMAIN,
    MODEL_CACHE_SECONDS,
    OAUTH_CLIENT_ID,
    SDK_TOKEN_MIN_LIFETIME_SECONDS,
)
from .models import model_billing
from .provisioning import async_runtime_path

if TYPE_CHECKING:
    from copilot.client import GitHubTokenProviderArgs, GitHubTokenProviderResult
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class CopilotRuntime:
    """Own the SDK client process for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options = dict(entry.options)
        self.credentials = CredentialStore(hass, entry, OAUTH_CLIENT_ID)
        self.client: Any | None = None
        self.backend: Any | None = None
        self.base_directory = Path(hass.config.path(".storage", DOMAIN, entry.entry_id))
        self.is_started = False
        self._models: list[Any] | None = None
        self._models_fetched_at = 0.0
        self._models_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()

    async def async_start(self) -> None:
        """Provision and start the pinned native runtime."""
        from copilot import CopilotClient
        from copilot.client import RuntimeConnection

        try:
            await self.credentials.async_ensure_account_id()
        except ReauthenticationRequired:
            await async_start_reauth_flow(self.hass, self.entry)
            _LOGGER.warning("GitHub account identity requires reauthentication")
        except OAuthError:
            _LOGGER.warning("Could not verify GitHub account identity during startup")
        runtime_path = await async_runtime_path(self.hass)
        await asyncio.to_thread(self.base_directory.mkdir, parents=True, exist_ok=True)
        self.client = await asyncio.to_thread(
            CopilotClient,
            connection=RuntimeConnection.for_stdio(path=runtime_path),
            use_logged_in_user=False,
            base_directory=str(self.base_directory),
            # The backend owns idle expiry and deletes native sessions on eviction.
            session_idle_timeout_seconds=0,
            mode="empty",
            log_level="error",
        )
        await self.client.start()
        from .backend import ConversationBackend

        self.backend = ConversationBackend(
            self.hass,
            self.entry,
            self.client,
            self.async_github_token_provider,
            self.async_list_models,
            access_token_getter=self.credentials.async_get_access_token,
        )
        self.is_started = True

    async def async_stop(self) -> None:
        """Stop the SDK runtime and release its process resources."""
        async with self._stop_lock:
            backend, self.backend = self.backend, None
            client, self.client = self.client, None
            self.is_started = False
            try:
                if backend is not None:
                    await backend.async_stop()
            finally:
                if client is not None:
                    try:
                        await client.stop()
                    except BaseException:
                        self.client = client
                        await client.force_stop()
                        self.client = None
                        raise

    async def async_github_token_provider(
        self, request: GitHubTokenProviderArgs
    ) -> GitHubTokenProviderResult:
        """Supply an expiring OAuth token only for the GitHub.com authority."""
        if request.get("host") not in (
            "github.com",
            "https://github.com",
            "https://github.com/",
        ):
            _LOGGER.error("Refusing credentials for an unsupported GitHub host")
            return {"kind": "cancelled"}
        try:
            token = await self.credentials.async_get_access_token()
        except ReauthenticationRequired:
            await async_start_reauth_flow(self.hass, self.entry)
            raise
        expires_at = self.entry.data.get(CONF_ACCESS_TOKEN_EXPIRES_AT)
        if expires_at is None:
            raise ReauthenticationRequired(
                "GitHub sign-in has no token expiry. Reauthenticate in Home Assistant."
            )
        expires_in = int(expires_at - time.time())
        if expires_in <= SDK_TOKEN_MIN_LIFETIME_SECONDS:
            raise OAuthError(
                "GitHub returned a token that expires too soon for Copilot."
            )
        return {
            "kind": "token",
            "accessToken": token,
            "expiresIn": expires_in,
        }

    async def async_list_models(self, *, force_refresh: bool = False) -> list[Any]:
        """Return models available to the account, refreshing a bounded cache."""
        async with self._models_lock:
            if (
                not force_refresh
                and self._models is not None
                and time.monotonic() - self._models_fetched_at < MODEL_CACHE_SECONDS
            ):
                return list(self._models)

            token = await self.credentials.async_get_access_token()
            try:
                try:
                    models = await self._async_list_models_rpc(token)
                except (ReauthenticationRequired, OAuthError):
                    raise
                except Exception as err:  # noqa: BLE001 - experimental SDK RPC
                    if is_quota_error(err) or is_authentication_error(err):
                        raise
                    _LOGGER.debug(
                        "Model listing through the running client failed (%s); "
                        "using a temporary client",
                        err,
                    )
                    models = await async_probe_models(
                        token, flow_id=self.entry.entry_id, hass=self.hass
                    )
            except Exception as err:
                if is_quota_error(err):
                    if self.backend is not None:
                        self.backend.report_quota_exceeded()
                    raise RuntimeError("GitHub Copilot quota exceeded.") from err
                if is_authentication_error(err):
                    raise ReauthenticationRequired(
                        "GitHub sign-in was rejected. Reauthenticate in Home Assistant."
                    ) from err
                raise

            self._models = list(models)
            billing = model_billing(self.entry.options[CONF_MODEL], self._models)
            if (
                billing["multiplier"] is not None
                and self.entry.data.get(CONF_MODEL_BILLING) != billing
            ):
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, CONF_MODEL_BILLING: billing}
                )
            self._models_fetched_at = time.monotonic()
            return list(models)

    async def _async_list_models_rpc(self, token: str) -> list[Any]:
        """List models with an explicit token on the already running client."""
        from copilot.generated.rpc import ModelsListRequest

        if self.client is None:
            raise RuntimeError("The Copilot runtime is not started.")
        result = await self.client.rpc.models.list(
            ModelsListRequest(git_hub_token=token)
        )
        return list(result.models)


async def async_probe_models(
    access_token: str, *, flow_id: str, hass: HomeAssistant
) -> list[Any]:
    """Start an isolated client, validate the token, and retrieve models."""
    from copilot import CopilotClient
    from copilot.client import RuntimeConnection

    runtime_path = await async_runtime_path(hass)
    temporary_directory = await asyncio.to_thread(
        tempfile.TemporaryDirectory,
        prefix=f"{DOMAIN}-{flow_id[:8]}-",
    )
    base_directory = Path(temporary_directory.name)
    client = None
    try:
        client = await asyncio.to_thread(
            CopilotClient,
            connection=RuntimeConnection.for_stdio(path=runtime_path),
            github_token=access_token,
            use_logged_in_user=False,
            base_directory=str(base_directory),
            mode="empty",
            log_level="error",
        )
        await client.start()
        return await client.list_models()
    except ReauthenticationRequired:
        raise
    except Exception as err:
        if is_authentication_error(err):
            raise ReauthenticationRequired(
                "GitHub sign-in was rejected. Reauthenticate in Home Assistant."
            ) from err
        raise RuntimeError("Copilot authentication or model discovery failed.") from err
    finally:
        try:
            if client is not None:
                await client.stop()
        finally:
            await asyncio.to_thread(temporary_directory.cleanup)


def remove_entry_directory(hass: HomeAssistant, entry_id: str) -> None:
    """Remove only the named entry directory, never the shared runtime cache."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", entry_id) or entry_id == "runtime":
        raise ValueError("Invalid Copilot entry directory identifier.")
    root = Path(hass.config.path(".storage", DOMAIN))
    if root.is_symlink():
        raise ValueError("Refusing to remove entry state through a symlinked root.")
    directory = root / entry_id
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)
