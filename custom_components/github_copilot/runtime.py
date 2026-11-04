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

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api_errors import is_authentication_error, is_quota_error
from .auth import (
    CredentialStore,
    OAuthError,
    ReauthenticationRequired,
    async_start_reauth_flow,
)
from .cleanup import async_wait_cleanup
from .const import (
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_AI_TASK_MODEL,
    CONF_AI_TASK_MODEL_BILLING,
    CONF_MODEL,
    CONF_MODEL_BILLING,
    DATA_SESSION_CLEANUP_LOCK,
    DOMAIN,
    MODEL_CACHE_SECONDS,
    MODELS_UPDATED_SIGNAL,
    OAUTH_CLIENT_ID,
    SDK_CLEANUP_TIMEOUT_SECONDS,
    SDK_FORCE_STOP_TIMEOUT_SECONDS,
    SDK_TOKEN_MIN_LIFETIME_SECONDS,
    SESSION_CLEANUP_INTERVAL_SECONDS,
    VERIFIED_IMAGE_RUNTIME,
)
from .models import model_billing
from .provisioning import async_runtime_path
from .session_cleanup import SessionCleanup

if TYPE_CHECKING:
    from copilot.client import GitHubTokenProviderArgs, GitHubTokenProviderResult
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_DATA_PROBE_CLEANUP = f"{DOMAIN}_probe_cleanup"


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
        self.image_models: list[Any] | None = None
        self._models_fetched_at = 0.0
        self._models_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._force_stop_task: asyncio.Task[None] | None = None
        self._force_stop_client: Any | None = None
        self._stopping = False
        self._session_cleanup: SessionCleanup | None = None
        self._maintenance_task: asyncio.Task[None] | None = None

    async def async_start(self) -> None:
        """Provision and start the pinned native runtime."""
        from copilot import CopilotClient
        from copilot.client import RuntimeConnection

        self._stopping = False
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
        self._session_cleanup = SessionCleanup(
            self.client,
            self.base_directory,
            self.hass.data.setdefault(DATA_SESSION_CLEANUP_LOCK, asyncio.Lock()),
        )
        await self._session_cleanup.async_run("startup", lambda session_id: False)
        from .backend import ConversationBackend

        client = self.client

        def on_runtime_failure(stopped: bool) -> None:
            """Never await reload from a turn whose teardown reload must drain."""
            if (
                self.client is not client
                or self.backend is not backend
                or self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id) is not self
            ):
                return
            self.is_started = False
            if self._session_cleanup is not None:
                self._session_cleanup.stop()
            self.image_models = None
            async_dispatcher_send(
                self.hass, f"{MODELS_UPDATED_SIGNAL}_{self.entry.entry_id}"
            )
            if stopped and not self._stopping and not self.hass.is_stopping:
                self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

        backend = ConversationBackend(
            self.hass,
            self.entry,
            self.client,
            self.async_github_token_provider,
            self.async_list_models,
            access_token_getter=self.credentials.async_get_access_token,
            image_model_getter=self.image_model,
            force_stop=lambda: self._async_force_stop_client(client),
            on_runtime_failure=on_runtime_failure,
        )
        self.backend = backend
        self.is_started = True
        self._maintenance_task = self.entry.async_create_background_task(
            self.hass,
            self._async_maintain_sessions(client, backend, self._session_cleanup),
            f"{DOMAIN} session cleanup",
        )

    async def _async_maintain_sessions(
        self, client: Any, backend: Any, cleanup: SessionCleanup
    ) -> None:
        while True:
            await asyncio.sleep(SESSION_CLEANUP_INTERVAL_SECONDS)
            if (
                self._stopping
                or not self.is_started
                or self.hass.is_stopping
                or self.client is not client
                or self.backend is not backend
            ):
                return
            await cleanup.async_run("periodic", backend.is_session_protected)

    async def _async_quiesce_maintenance(self) -> None:
        if self._session_cleanup is not None:
            self._session_cleanup.stop()
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None

    async def _async_close_session_cleanup(self) -> None:
        if self._session_cleanup is not None:
            await self._session_cleanup.async_close()
            self._session_cleanup = None

    async def _async_force_stop_client(self, client: Any) -> None:
        """Share native termination between turn recovery and runtime shutdown."""
        if self._session_cleanup is not None and self.client is client:
            self._session_cleanup.stop()
        if (
            self._force_stop_client is not client
            or self._force_stop_task is None
            or (
                self._force_stop_task.done()
                and (
                    self._force_stop_task.cancelled()
                    or self._force_stop_task.exception() is not None
                )
            )
        ):
            self._force_stop_client = client

            async def stop() -> None:
                try:
                    async with asyncio.timeout(SDK_FORCE_STOP_TIMEOUT_SECONDS):
                        await client.force_stop()
                except BaseException as err:
                    _LOGGER.error(
                        "Unable to confirm native Copilot termination (%s); "
                        "retaining the client for retry",
                        type(err).__name__,
                    )
                    raise

            self._force_stop_task = asyncio.create_task(stop())
        await async_wait_cleanup(self._force_stop_task)

    async def async_stop(self) -> None:
        """Stop the SDK runtime and release its process resources."""
        async with self._stop_lock:
            self._stopping = True
            interrupted: asyncio.CancelledError | None = None
            try:
                await async_wait_cleanup(
                    asyncio.create_task(self._async_quiesce_maintenance())
                )
            except asyncio.CancelledError as err:
                interrupted = err
            backend, self.backend = self.backend, None
            client, self.client = self.client, None
            self.is_started = False
            try:
                if backend is not None:
                    await backend.async_stop()
            finally:
                if client is not None:
                    try:
                        async with asyncio.timeout(SDK_CLEANUP_TIMEOUT_SECONDS):
                            await client.stop()
                    except BaseException as err:
                        # Keep ownership until native termination is confirmed.
                        self.client = client
                        self.backend = backend

                        # Caller cancellation must not skip confirmed-stop bookkeeping.
                        async def force_stop_and_finalize() -> None:
                            await self._async_force_stop_client(client)
                            if backend is not None:
                                backend.mark_runtime_stopped()
                            self.client = None
                            self.backend = None
                            await self._async_close_session_cleanup()

                        await async_wait_cleanup(
                            asyncio.create_task(force_stop_and_finalize())
                        )
                        if isinstance(err, TimeoutError):
                            _LOGGER.warning(
                                "Copilot graceful shutdown timed out; "
                                "the native process was stopped forcibly"
                            )
                        else:
                            raise
                    else:
                        if backend is not None:
                            backend.mark_runtime_stopped()
                        await self._async_close_session_cleanup()
                else:
                    await self._async_close_session_cleanup()
            if interrupted is not None:
                raise interrupted

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
                    self.image_models = None
                    models, self.image_models = await async_probe_model_catalog(
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
            data = dict(self.entry.data)
            for model_key, billing_key in (
                (CONF_MODEL, CONF_MODEL_BILLING),
                (CONF_AI_TASK_MODEL, CONF_AI_TASK_MODEL_BILLING),
            ):
                model = self.entry.options.get(model_key)
                if model is None:
                    continue
                billing = model_billing(model, self._models)
                if billing["multiplier"] is not None:
                    data[billing_key] = billing
            if data != self.entry.data:
                self.hass.config_entries.async_update_entry(self.entry, data=data)
            self._models_fetched_at = time.monotonic()
            async_dispatcher_send(
                self.hass, f"{MODELS_UPDATED_SIGNAL}_{self.entry.entry_id}"
            )
            return list(models)

    def image_model(self) -> Any | None:
        """Return the runtime-selected, account-authorized generation model."""
        from copilot._cli_version import CLI_VERSION

        if CLI_VERSION != VERIFIED_IMAGE_RUNTIME:
            return None
        for model in self.image_models or []:
            availability = getattr(model.availability, "value", model.availability)
            operations = {
                getattr(operation, "value", operation)
                for operation in model.supported_operations
            }
            if (
                model.is_default
                and availability == "available"
                and "generate" in operations
            ):
                return model
        return None

    async def _async_list_models_rpc(self, token: str) -> list[Any]:
        """List models with an explicit token on the already running client."""
        if self.client is None:
            raise RuntimeError("The Copilot runtime is not started.")
        models, self.image_models = await _async_list_model_catalog(self.client, token)
        return models


async def _async_list_model_catalog(
    client: Any, access_token: str
) -> tuple[list[Any], list[Any] | None]:
    """Keep chat and image capabilities from the same authenticated discovery."""
    from copilot.generated.rpc import ModelsListRequest

    result = await client.rpc.models.list(ModelsListRequest(git_hub_token=access_token))
    images = getattr(result, "image_models", None)
    return list(result.models), list(images) if images is not None else None


class _ModelProbeCleanup:
    """Retain failed probe cleanup until it can be retried safely."""

    def __init__(self, hass: HomeAssistant, directory: Path) -> None:
        self.hass = hass
        self.directory: Path | None = directory
        self.client: Any | None = None
        self._stop_lock = asyncio.Lock()
        self._unsubscribe: Any | None = None
        self._directory_cleanup: asyncio.Task[None] | None = None

    async def async_stop(self) -> None:
        """Reap the process before removing its directory."""
        async with self._stop_lock:
            stop_error: BaseException | None = None
            try:
                if self.client is not None:
                    try:
                        async with asyncio.timeout(SDK_CLEANUP_TIMEOUT_SECONDS):
                            await self.client.stop()
                    except BaseException as err:
                        stop_error = err
                        try:
                            async with asyncio.timeout(SDK_FORCE_STOP_TIMEOUT_SECONDS):
                                await self.client.force_stop()
                        except BaseException:
                            _LOGGER.exception(
                                "Failed to force-stop a Copilot model probe; "
                                "retaining its client and directory %s",
                                self.directory,
                            )
                            if isinstance(stop_error, asyncio.CancelledError):
                                raise stop_error from None
                            raise
                    self.client = None
                if self.directory is not None:
                    if self._directory_cleanup is None:
                        self._directory_cleanup = asyncio.create_task(
                            asyncio.to_thread(shutil.rmtree, self.directory)
                        )
                    try:
                        await asyncio.shield(self._directory_cleanup)
                    except BaseException:
                        if self._directory_cleanup.done() and (
                            self._directory_cleanup.cancelled()
                            or self._directory_cleanup.exception() is not None
                        ):
                            self._directory_cleanup = None
                        raise
                    self._directory_cleanup = None
                    self.directory = None
            except BaseException:
                self.hass.data.setdefault(_DATA_PROBE_CLEANUP, {})[id(self)] = self
                if self._unsubscribe is None:
                    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

                    self._unsubscribe = self.hass.bus.async_listen_once(
                        EVENT_HOMEASSISTANT_STOP, self._async_shutdown
                    )
                raise
            pending = self.hass.data.get(_DATA_PROBE_CLEANUP)
            if pending is not None:
                pending.pop(id(self), None)
                if not pending:
                    self.hass.data.pop(_DATA_PROBE_CLEANUP, None)
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            if stop_error is not None:
                raise stop_error

    async def _async_shutdown(self, event: Any) -> None:
        """Retry retained cleanup before Home Assistant exits."""
        try:
            await self.async_stop()
        except BaseException:
            _LOGGER.exception("Failed to clean up a retained Copilot model probe")


async def async_probe_models(
    access_token: str, *, flow_id: str, hass: HomeAssistant
) -> list[Any]:
    """Return chat models for configuration flows."""
    models, _ = await async_probe_model_catalog(
        access_token, flow_id=flow_id, hass=hass
    )
    return models


async def async_probe_model_catalog(
    access_token: str, *, flow_id: str, hass: HomeAssistant
) -> tuple[list[Any], list[Any] | None]:
    """Start an isolated client and discover authorized chat and image models."""
    from copilot import CopilotClient
    from copilot.client import RuntimeConnection

    for pending in tuple(hass.data.get(_DATA_PROBE_CLEANUP, {}).values()):
        try:
            await pending.async_stop()
        except Exception:
            if pending.directory is not None:
                raise
            _LOGGER.warning(
                "Retained Copilot model probe required forced cleanup", exc_info=True
            )

    runtime_path = await async_runtime_path(hass)
    base_directory = Path(
        await asyncio.to_thread(
            tempfile.mkdtemp,
            prefix=f"{DOMAIN}-{flow_id[:8]}-",
        )
    )
    cleanup = _ModelProbeCleanup(hass, base_directory)
    probe_failed = True
    try:
        client = cleanup.client = await asyncio.to_thread(
            CopilotClient,
            connection=RuntimeConnection.for_stdio(path=runtime_path),
            github_token=access_token,
            use_logged_in_user=False,
            base_directory=str(base_directory),
            mode="empty",
            log_level="error",
        )
        await client.start()
        catalog = await _async_list_model_catalog(client, access_token)
        probe_failed = False
        return catalog
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
            await cleanup.async_stop()
        except BaseException:
            _LOGGER.exception("Copilot model probe cleanup reported an error")
            if not probe_failed:
                raise


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
