"""Discoverable diagnostics expose status without private entry contents."""

import importlib
import json
from types import SimpleNamespace

import pytest

from custom_components.github_copilot.const import DOMAIN


@pytest.mark.parametrize("state", ["missing", "unstarted", "started", "no_backend"])
async def test_diagnostics_platform_returns_only_safe_status(state):
    diagnostics = importlib.import_module(
        "custom_components.github_copilot.diagnostics"
    )
    entry = SimpleNamespace(
        entry_id="entry",
        data={
            "auth_method": "oauth",
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "account_id": "private-account",
        },
        options={
            "model": "selected-model",
            "enable_device_control": True,
            "prompt": "private-system-prompt",
        },
    )
    runtime = SimpleNamespace(
        is_started=state == "started",
        backend=(
            None
            if state == "no_backend"
            else SimpleNamespace(
                active_model="active-model", messages=["private-conversation"]
            )
        ),
    )
    hass = SimpleNamespace(
        data={} if state == "missing" else {DOMAIN: {"entry": runtime}}
    )
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result == {
        "auth_method": "oauth",
        "has_access_token": True,
        "selected_model": "selected-model",
        "active_model": (
            None if state in ("missing", "no_backend") else "active-model"
        ),
        "device_control_enabled": True,
        "runtime_started": state == "started",
    }
    assert "private-" not in json.dumps(result)


async def test_diagnostics_handles_missing_optional_entry_values():
    from custom_components.github_copilot.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    result = await async_get_config_entry_diagnostics(
        SimpleNamespace(data={}),
        SimpleNamespace(entry_id="entry", data={}, options={}),
    )
    assert result == {
        "auth_method": None,
        "has_access_token": False,
        "selected_model": None,
        "active_model": None,
        "device_control_enabled": False,
        "runtime_started": False,
    }
