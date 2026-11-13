"""Small HA boundary doubles for isolated integration logic tests.

These tests run the integration's real code and SDK types, but not an HA server.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextvars import ContextVar
from enum import Enum, IntFlag
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import homeassistant  # noqa: F401 - initialize HA's validator compatibility layer first
import pytest
import voluptuous as vol


class _HomeAssistantError(Exception):
    pass


class _Unauthorized(_HomeAssistantError):
    pass


class _IssueSeverity(Enum):
    WARNING = "warning"
    ERROR = "error"


class _ConversationEntityFeature(IntFlag):
    CONTROL = 1


class _Flow:
    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_show_progress(self, **kwargs):
        return {"type": "progress", **kwargs}

    def async_show_progress_done(self, **kwargs):
        return {"type": "progress_done", **kwargs}

    def async_remove(self):
        pass

    def async_update_reload_and_abort(self, entry, *, data, reason):
        self.hass.config_entries.async_update_entry(entry, data=data)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason=reason)


@pytest.fixture
def ha_boundary(monkeypatch):
    """Mock only the HA services consumed by these unit tests."""
    issues = {}

    def create_issue(hass, domain, issue_id, **kwargs):
        issues[(domain, issue_id)] = kwargs

    def delete_issue(hass, domain, issue_id):
        issues.pop((domain, issue_id), None)

    def at_started(hass, callback):
        if hass.is_running:
            callback(hass)
            return Mock()
        return hass.bus.async_listen_once(
            "homeassistant_started", lambda event: callback(hass)
        )

    attributes = {
        "homeassistant.components": {},
        "homeassistant.components.conversation": {
            "SystemContent": type("SystemContent", (), {}),
            "ConversationEntity": type("ConversationEntity", (), {}),
            "ConversationEntityFeature": _ConversationEntityFeature,
        },
        "homeassistant.core": {
            "HomeAssistant": SimpleNamespace,
            "callback": lambda fn: fn,
        },
        "homeassistant.exceptions": {
            "HomeAssistantError": _HomeAssistantError,
            "Unauthorized": _Unauthorized,
            "ConfigEntryNotReady": type("ConfigEntryNotReady", (Exception,), {}),
        },
        "homeassistant.config_entries": {
            "ConfigEntry": SimpleNamespace,
            "ConfigFlow": _Flow,
            "OptionsFlow": _Flow,
        },
        "homeassistant.const": {
            "EVENT_HOMEASSISTANT_STOP": "homeassistant_stop",
            "EVENT_HOMEASSISTANT_STARTED": "homeassistant_started",
            "CONF_LLM_HASS_API": "llm_hass_api",
            "CONF_PROMPT": "prompt",
            "MATCH_ALL": "*",
            "Platform": SimpleNamespace(CONVERSATION="conversation", AI_TASK="ai_task"),
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.start": {"async_at_started": at_started},
        "homeassistant.helpers.chat_session": {
            "current_session": ContextVar("current_session", default=None),
        },
        "homeassistant.helpers.json": {"json_dumps": json.dumps},
        "homeassistant.helpers.entity_platform": {
            "AddEntitiesCallback": Mock,
        },
        "homeassistant.helpers.issue_registry": {
            "IssueSeverity": _IssueSeverity,
            "async_create_issue": Mock(side_effect=create_issue),
            "async_delete_issue": Mock(side_effect=delete_issue),
        },
        "homeassistant.helpers.llm": {"ToolInput": SimpleNamespace},
        "homeassistant.helpers.aiohttp_client": {"async_get_clientsession": Mock()},
        "homeassistant.helpers.selector": {
            "BooleanSelector": lambda: vol.Boolean(),
            "SelectOptionDict": dict,
            "SelectSelectorConfig": dict,
            "SelectSelector": lambda config: vol.In(
                [item["value"] for item in config["options"]]
            ),
            "TextSelector": lambda *args, **kwargs: str,
            "TextSelectorConfig": dict,
            "TextSelectorType": SimpleNamespace(PASSWORD="password"),
        },
    }
    for name, attrs in attributes.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)

    def load(name):
        path = (
            Path(__file__).parents[1]
            / "custom_components/github_copilot"
            / f"{name}.py"
        )
        module_name = f"custom_components.github_copilot._test_{name}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, module_name, module)
        spec.loader.exec_module(module)
        return module

    api_path = Path(__file__).parents[1] / "custom_components/github_copilot/llm_api.py"
    api_name = "custom_components.github_copilot.llm_api"
    api_spec = importlib.util.spec_from_file_location(api_name, api_path)
    assert api_spec is not None and api_spec.loader is not None
    api_module = importlib.util.module_from_spec(api_spec)
    monkeypatch.setitem(sys.modules, api_name, api_module)
    api_spec.loader.exec_module(api_module)

    return SimpleNamespace(
        load=load,
        issues=issues,
        llm=sys.modules["homeassistant.helpers.llm"],
        HomeAssistantError=_HomeAssistantError,
        Unauthorized=_Unauthorized,
    )
