"""GitHub Copilot integration for Home Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .runtime import CopilotRuntime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a GitHub Copilot config entry."""
    from homeassistant.const import Platform
    from homeassistant.exceptions import ConfigEntryNotReady

    from .runtime import CopilotRuntime

    runtime = CopilotRuntime(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    try:
        await runtime.async_start()
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform.CONVERSATION]
        )
    except (OSError, RuntimeError) as err:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await runtime.async_stop()
        raise ConfigEntryNotReady(
            f"Unable to start the Copilot runtime: {err}"
        ) from err

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_create_background_task(
        hass, runtime.backend.async_check_model(), f"{DOMAIN} model check"
    )
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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, object]:
    """Return diagnostics without credentials or conversation content."""
    runtime: CopilotRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return {
        "auth_method": entry.data.get("auth_method"),
        "has_access_token": bool(entry.data.get("access_token")),
        "selected_model": entry.options.get("model"),
        "active_model": (
            runtime.backend.active_model
            if runtime is not None and runtime.backend is not None
            else None
        ),
        "device_control_enabled": entry.options.get("enable_device_control", False),
        "runtime_started": runtime is not None and runtime.is_started,
    }
