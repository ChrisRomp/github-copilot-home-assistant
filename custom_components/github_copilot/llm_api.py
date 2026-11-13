"""Select Home Assistant LLM APIs and enforce their caller requirements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .const import CONF_ENABLE_DEVICE_CONTROL, CONF_ENABLE_HOME_ASSISTANT_API

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant

ASSIST_API = "assist"
HOME_ASSISTANT_API = "homeassistant"
ADMIN_REQUIRED = (
    "Home Assistant administration tools require an authenticated Home Assistant "
    "administrator. Use an administrator account or disable these tools in the "
    "GitHub Copilot integration settings."
)
API_UNAVAILABLE = (
    "Home Assistant administration tools require Home Assistant 2026.11 or newer. "
    "Upgrade Home Assistant or disable these tools in the GitHub Copilot "
    "integration settings."
)


def home_assistant_api_supported() -> bool:
    """Detect the API in the running HA version, including compatible backports."""
    return getattr(llm, "LLM_API_HOME_ASSISTANT", None) == HOME_ASSISTANT_API


def selected_llm_apis(options: Mapping[str, Any]) -> list[str] | None:
    """Return the configured APIs that exist in the running HA version."""
    apis = [ASSIST_API] if options.get(CONF_ENABLE_DEVICE_CONTROL) else []
    if options.get(CONF_ENABLE_HOME_ASSISTANT_API) and home_assistant_api_supported():
        apis.append(HOME_ASSISTANT_API)
    return apis or None


def api_includes(api_instance: Any, api_id: str) -> bool:
    """Check whether an API instance includes one of its constituent APIs."""
    api = getattr(api_instance, "api", None)
    if api is None:
        return False
    merged_apis = getattr(api, "llm_apis", None)
    if merged_apis is not None:
        return any(
            getattr(merged_api, "id", None) == api_id for merged_api in merged_apis
        )
    return getattr(api, "id", None) == api_id


async def async_is_admin(hass: HomeAssistant, context: Context | None) -> bool:
    """Check the current request's user, not the integration owner's account."""
    user_id = context.user_id if context is not None else None
    user = await hass.auth.async_get_user(user_id) if user_id else None
    return user is not None and user.is_admin


async def async_available_llm_apis(
    hass: HomeAssistant, options: Mapping[str, Any], context: Context | None
) -> tuple[list[str] | None, str | None]:
    """Keep normal tools available while explaining unavailable administrator tools."""
    apis = selected_llm_apis(options) or []
    if options.get(CONF_ENABLE_HOME_ASSISTANT_API):
        if not home_assistant_api_supported():
            return apis or None, API_UNAVAILABLE
        if not await async_is_admin(hass, context):
            apis.remove(HOME_ASSISTANT_API)
            return apis or None, ADMIN_REQUIRED
    return apis or None, None


async def async_require_admin(hass: HomeAssistant, context: Context | None) -> None:
    """Recheck authorization before every administrator-capable tool invocation."""
    if not await async_is_admin(hass, context):
        raise HomeAssistantError(ADMIN_REQUIRED)
