"""GitHub Copilot integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant

    from .runtime import CopilotRuntime

_LOGGER = logging.getLogger(__name__)


def _remove_stopped_runtime(
    hass: HomeAssistant, entry: ConfigEntry, runtime: CopilotRuntime
) -> None:
    """Remove only this runtime, without disturbing a replacement registry."""
    runtimes = hass.data.get(DOMAIN)
    if runtimes is None or runtimes.get(entry.entry_id) is not runtime:
        return
    runtimes.pop(entry.entry_id)
    if not runtimes and hass.data.get(DOMAIN) is runtimes:
        hass.data.pop(DOMAIN, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a GitHub Copilot config entry."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
    from homeassistant.exceptions import ConfigEntryNotReady

    from .provisioning import RuntimeDownloadError
    from .runtime import CopilotRuntime

    previous = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if previous is not None:
        try:
            await previous.async_stop()
        except (OSError, RuntimeDownloadError) as err:
            raise ConfigEntryNotReady(
                "Unable to stop the previous Copilot runtime."
            ) from err
        _remove_stopped_runtime(hass, entry, previous)

    runtime = CopilotRuntime(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    async def async_stop_runtime(event: Event) -> None:
        """Finish runtime cleanup before Home Assistant closes its event loop."""
        await runtime.async_stop()

    try:
        await runtime.async_start()
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform.CONVERSATION, Platform.AI_TASK]
        )
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop_runtime)
        )
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        entry.async_create_background_task(
            hass, runtime.backend.async_check_model(), f"{DOMAIN} model check"
        )
    except BaseException as err:
        try:
            await runtime.async_stop()
        except BaseException:
            _LOGGER.exception("Failed to stop the Copilot runtime after setup failed")
        else:
            _remove_stopped_runtime(hass, entry, runtime)
        if isinstance(err, (OSError, RuntimeDownloadError)):
            raise ConfigEntryNotReady(
                f"Unable to start the Copilot runtime: {err}"
            ) from err
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a GitHub Copilot config entry."""
    from homeassistant.const import Platform

    if not await hass.config_entries.async_unload_platforms(
        entry, [Platform.CONVERSATION, Platform.AI_TASK]
    ):
        return False

    runtime: CopilotRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return True
    await runtime.async_stop()
    _remove_stopped_runtime(hass, entry, runtime)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when its options change."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None and runtime.options != entry.options:
        runtime.options = dict(entry.options)
        await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Seed independent task settings once, preserving conversation choices."""
    from .const import (
        CONF_AI_TASK_MODEL,
        CONF_AI_TASK_MODEL_BILLING,
        CONF_AI_TASK_TIMEOUT,
        CONF_MODEL,
        CONF_MODEL_BILLING,
        DEFAULT_AI_TASK_TIMEOUT,
    )

    if entry.version != 1:
        return False
    if entry.minor_version < 2:
        options = dict(entry.options)
        options.setdefault(CONF_AI_TASK_MODEL, options[CONF_MODEL])
        options.setdefault(CONF_AI_TASK_TIMEOUT, DEFAULT_AI_TASK_TIMEOUT)
        data = dict(entry.data)
        snapshot = data.get(CONF_MODEL_BILLING, {})
        data.setdefault(
            CONF_AI_TASK_MODEL_BILLING,
            dict(snapshot)
            if snapshot.get("model") == options[CONF_AI_TASK_MODEL]
            else {"model": options[CONF_AI_TASK_MODEL], "multiplier": None},
        )
        hass.config_entries.async_update_entry(
            entry, options=options, data=data, minor_version=2
        )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove entry-owned state and Repairs, retaining the shared runtime cache."""
    import asyncio

    from homeassistant.helpers import issue_registry as ir

    from .const import (
        DATA_CREDENTIAL_LOCKS,
        ISSUE_MODEL_FALLBACK,
        ISSUE_MODEL_UNAVAILABLE,
        ISSUE_QUOTA_EXCEEDED,
        ISSUE_TASK_MODEL_FALLBACK,
        ISSUE_TASK_MODEL_UNAVAILABLE,
    )
    from .runtime import remove_entry_directory

    runtime: CopilotRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None:
        await runtime.async_stop()
        _remove_stopped_runtime(hass, entry, runtime)

    await asyncio.to_thread(remove_entry_directory, hass, entry.entry_id)
    hass.data.get(DATA_CREDENTIAL_LOCKS, {}).pop(entry.entry_id, None)

    for issue in (
        ISSUE_MODEL_FALLBACK,
        ISSUE_MODEL_UNAVAILABLE,
        ISSUE_QUOTA_EXCEEDED,
        ISSUE_TASK_MODEL_FALLBACK,
        ISSUE_TASK_MODEL_UNAVAILABLE,
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
