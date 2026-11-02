"""Diagnostics for the GitHub Copilot integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .runtime import CopilotRuntime


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
