"""Diagnostics for the GitHub Copilot integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    DOMAIN,
    IMAGE_GENERATION_VERIFIED,
    IMAGE_HOST_BUDGET_VERIFIED,
    IMAGE_INPUT_VERIFIED,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .runtime import CopilotRuntime


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, object]:
    """Return diagnostics without credentials or conversation content."""
    runtime: CopilotRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    images = getattr(runtime, "image_models", None)
    image_model = next((model for model in images or [] if model.is_default), None)
    availability = getattr(image_model, "availability", None)
    return {
        "auth_method": entry.data.get("auth_method"),
        "has_access_token": bool(entry.data.get("access_token")),
        "selected_model": entry.options.get("model"),
        "active_model": (
            runtime.backend.active_model
            if runtime is not None and runtime.backend is not None
            else None
        ),
        "ai_task_model": entry.options.get("ai_task_model", entry.options.get("model")),
        "active_ai_task_model": (
            getattr(runtime.backend, "active_task_model", None)
            if runtime is not None and runtime.backend is not None
            else None
        ),
        "ai_task_timeout": entry.options.get("ai_task_timeout", 180),
        "image_input_verified": IMAGE_INPUT_VERIFIED,
        "image_generation_verified": IMAGE_GENERATION_VERIFIED,
        "image_host_budget_verified": IMAGE_HOST_BUDGET_VERIFIED,
        "native_image_model": getattr(image_model, "id", None),
        "native_image_model_availability": getattr(availability, "value", availability),
        "device_control_enabled": entry.options.get("enable_device_control", False),
        "runtime_started": runtime is not None and runtime.is_started,
    }
