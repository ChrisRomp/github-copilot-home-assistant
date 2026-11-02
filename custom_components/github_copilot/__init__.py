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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a GitHub Copilot config entry."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
    from homeassistant.exceptions import ConfigEntryNotReady

    from .provisioning import RuntimeDownloadError
    from .runtime import CopilotRuntime

    runtime = CopilotRuntime(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    async def async_stop_runtime(event: Event) -> None:
        """Finish runtime cleanup before Home Assistant closes its event loop."""
        await runtime.async_stop()

    try:
        await runtime.async_start()
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform.CONVERSATION]
        )
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop_runtime)
        )
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        entry.async_create_background_task(
            hass, runtime.backend.async_check_model(), f"{DOMAIN} model check"
        )
    except BaseException as err:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
        try:
            await runtime.async_stop()
        except BaseException:
            _LOGGER.exception("Failed to stop the Copilot runtime after setup failed")
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
        entry, [Platform.CONVERSATION]
    ):
        return False

    runtime: CopilotRuntime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime.async_stop()
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when its options change."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None and runtime.options != entry.options:
        runtime.options = dict(entry.options)
        await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove entry-owned state and Repairs, retaining the shared runtime cache."""
    import asyncio

    from homeassistant.helpers import issue_registry as ir

    from .const import (
        DATA_CREDENTIAL_LOCKS,
        ISSUE_MODEL_FALLBACK,
        ISSUE_MODEL_UNAVAILABLE,
        ISSUE_QUOTA_EXCEEDED,
    )
    from .runtime import remove_entry_directory

    await asyncio.to_thread(remove_entry_directory, hass, entry.entry_id)
    hass.data.get(DATA_CREDENTIAL_LOCKS, {}).pop(entry.entry_id, None)

    for issue in (ISSUE_MODEL_FALLBACK, ISSUE_MODEL_UNAVAILABLE, ISSUE_QUOTA_EXCEEDED):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
