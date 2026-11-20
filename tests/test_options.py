"""Options selection tests with the HA flow boundary replaced by a double."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT

from custom_components.github_copilot.const import (
    CONF_ALLOW_ALL_SATELLITES,
    CONF_ALLOWED_SATELLITES,
    CONF_ENABLE_DEVICE_CONTROL,
    CONF_ENABLE_HOME_ASSISTANT_API,
    CONF_MODEL,
    CONF_MODEL_BILLING,
    DOMAIN,
)


@pytest.fixture
def options_setup(ha_boundary):
    module = ha_boundary.load("config_flow")
    entry = SimpleNamespace(
        entry_id="entry",
        options={
            CONF_MODEL: "gpt-6-sol",
            "ai_task_model": "gpt-6-sol",
            "ai_task_timeout": 180,
            CONF_PROMPT: "Previous instructions",
            CONF_ENABLE_DEVICE_CONTROL: True,
            "enable_shared_memory": False,
            CONF_LLM_HASS_API: ["assist"],
        },
        data={
            "account_id": "123",
            "access_token": "test-token",
            CONF_MODEL_BILLING: {"model": "gpt-6-sol", "multiplier": 1.0},
            "ai_task_model_billing": {"model": "gpt-6-sol", "multiplier": 0.5},
        },
    )
    runtime = SimpleNamespace(
        is_started=True,
        async_list_models=AsyncMock(
            return_value=[
                SimpleNamespace(id="gpt-6-sol"),
                SimpleNamespace(id="gpt-6.1-sol"),
            ]
        ),
    )
    flow = module.GitHubCopilotOptionsFlow(entry)
    flow.hass = SimpleNamespace(
        data={DOMAIN: {entry.entry_id: runtime}},
        config_entries=SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, *, data: setattr(entry, "data", data)
            )
        ),
    )
    return SimpleNamespace(flow=flow, entry=entry, runtime=runtime)


@pytest.mark.parametrize("multiplier", [0.0, 0.5])
async def test_prompt_edit_preserves_unchanged_model_billing(options_setup, multiplier):
    setup = options_setup
    snapshot = {"model": "gpt-6-sol", "multiplier": multiplier}
    setup.entry.data[CONF_MODEL_BILLING] = snapshot
    original_data = dict(setup.entry.data)
    form = await setup.flow.async_step_init()
    submitted = form["data_schema"]({CONF_PROMPT: "Updated instructions"})

    result = await setup.flow.async_step_init(submitted)

    assert result["data"] == {
        CONF_MODEL: "gpt-6-sol",
        "ai_task_model": "gpt-6-sol",
        "ai_task_timeout": 180,
        CONF_ENABLE_DEVICE_CONTROL: True,
        CONF_ALLOW_ALL_SATELLITES: False,
        CONF_ALLOWED_SATELLITES: [],
        CONF_LLM_HASS_API: ["assist"],
        CONF_PROMPT: "Updated instructions",
        "enable_shared_memory": False,
    }
    assert setup.entry.data == original_data
    setup.flow.hass.config_entries.async_update_entry.assert_called_once()


@pytest.mark.parametrize("snapshot_model", ["gpt-6-sol", "gpt-6.1-sol"])
async def test_changed_model_without_billing_resets_snapshot(
    options_setup, snapshot_model
):
    setup = options_setup
    setup.entry.data[CONF_MODEL_BILLING]["model"] = snapshot_model
    form = await setup.flow.async_step_init()
    submitted = form["data_schema"]({CONF_MODEL: "gpt-6.1-sol"})

    result = await setup.flow.async_step_init(submitted)

    assert result["data"][CONF_MODEL] == "gpt-6.1-sol"
    assert setup.entry.data[CONF_MODEL_BILLING] == {
        "model": "gpt-6.1-sol",
        "multiplier": None,
    }
    assert setup.entry.data["account_id"] == "123"
    assert setup.entry.data["access_token"] == "test-token"


@pytest.mark.parametrize("selected_model", ["gpt-6-sol", "gpt-6.1-sol"])
@pytest.mark.parametrize("multiplier", [0.0, 0.5, 2.0])
async def test_fresh_billing_replaces_snapshot(
    options_setup, selected_model, multiplier
):
    setup = options_setup
    setup.runtime.async_list_models.return_value = [
        SimpleNamespace(
            id=selected_model,
            billing=SimpleNamespace(multiplier=multiplier),
        )
    ]
    form = await setup.flow.async_step_init()
    submitted = form["data_schema"]({CONF_MODEL: selected_model})

    await setup.flow.async_step_init(submitted)

    assert setup.entry.data[CONF_MODEL_BILLING] == {
        "model": selected_model,
        "multiplier": multiplier,
    }


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"model": "gpt-6.1-sol", "multiplier": 1.0},
        {"model": "gpt-6-sol", "multiplier": None},
        {"model": "gpt-6-sol", "multiplier": True},
        {"model": "gpt-6-sol", "multiplier": -1.0},
        {"model": "gpt-6-sol", "multiplier": float("inf")},
        {"model": "gpt-6-sol", "multiplier": float("nan")},
        {"model": "gpt-6-sol", "multiplier": "1.0"},
    ],
)
async def test_invalid_or_mismatched_billing_is_not_preserved(options_setup, snapshot):
    setup = options_setup
    setup.entry.data[CONF_MODEL_BILLING] = snapshot
    form = await setup.flow.async_step_init()

    await setup.flow.async_step_init(
        form["data_schema"]({CONF_PROMPT: "Updated instructions"})
    )

    assert setup.entry.data[CONF_MODEL_BILLING] == {
        "model": "gpt-6-sol",
        "multiplier": None,
    }


@pytest.mark.parametrize("prompt_input", [{}, {CONF_PROMPT: ""}])
@pytest.mark.parametrize("device_control_enabled", [False, True])
async def test_optional_prompt_can_be_cleared(
    options_setup, prompt_input, device_control_enabled
):
    setup = options_setup
    setup.entry.options[CONF_ENABLE_DEVICE_CONTROL] = device_control_enabled
    form = await setup.flow.async_step_init()
    prompt_field = next(key for key in form["data_schema"].schema if key == CONF_PROMPT)
    assert prompt_field.description == {"suggested_value": "Previous instructions"}
    submitted = form["data_schema"](prompt_input)

    result = await setup.flow.async_step_init(submitted)

    assert result["data"].get(CONF_PROMPT, "") == ""
    assert (CONF_PROMPT in result["data"]) == (CONF_PROMPT in prompt_input)
    assert result["data"][CONF_ENABLE_DEVICE_CONTROL] is device_control_enabled
    assert result["data"][CONF_LLM_HASS_API] == (
        ["assist"] if device_control_enabled else None
    )


@pytest.mark.parametrize("device_control", [False, True])
@pytest.mark.parametrize("administrator_tools", [False, True])
async def test_option_changes_preserve_independent_api_selection(
    options_setup, ha_boundary, monkeypatch, device_control, administrator_tools
):
    monkeypatch.setattr(
        ha_boundary.llm, "LLM_API_HOME_ASSISTANT", "homeassistant", raising=False
    )
    setup = options_setup
    form = await setup.flow.async_step_init()
    assert form["data_schema"]({})[CONF_ENABLE_HOME_ASSISTANT_API] is False
    result = await setup.flow.async_step_init(
        form["data_schema"](
            {
                CONF_ENABLE_DEVICE_CONTROL: device_control,
                CONF_ENABLE_HOME_ASSISTANT_API: administrator_tools,
            }
        )
    )

    expected_apis = []
    if device_control:
        expected_apis.append("assist")
    if administrator_tools:
        expected_apis.append("homeassistant")
    assert result["data"][CONF_LLM_HASS_API] == (expected_apis or None)
    assert result["data"][CONF_ENABLE_HOME_ASSISTANT_API] is administrator_tools


async def test_satellite_options_store_registry_ids_and_preserve_selection(
    options_setup, ha_boundary, monkeypatch
):
    bedroom = SimpleNamespace(
        id="stable-bedroom-id",
        domain="assist_satellite",
        entity_id="assist_satellite.bedroom",
        disabled_by=None,
    )
    office = SimpleNamespace(
        id="stable-office-id",
        domain="assist_satellite",
        entity_id="assist_satellite.office",
    )
    registry = SimpleNamespace(
        entities={"bedroom": bedroom, "office": office},
        async_get=Mock(
            side_effect=lambda entity_id: {
                bedroom.entity_id: bedroom,
                office.entity_id: office,
            }.get(entity_id)
        ),
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        SimpleNamespace(async_get=Mock(return_value=registry)),
        raising=False,
    )
    setup = options_setup
    setup.entry.options[CONF_ALLOWED_SATELLITES] = [bedroom.id]
    form = await setup.flow.async_step_init()
    field = next(
        key for key in form["data_schema"].schema if key == CONF_ALLOWED_SATELLITES
    )
    assert field.description == {"suggested_value": [bedroom.entity_id]}
    submitted = form["data_schema"]({CONF_ALLOWED_SATELLITES: [bedroom.entity_id]})
    assert submitted[CONF_ALLOWED_SATELLITES] == [bedroom.entity_id]
    assert submitted[CONF_ALLOW_ALL_SATELLITES] is False

    submitted[CONF_ALLOW_ALL_SATELLITES] = True
    submitted[CONF_ALLOWED_SATELLITES] = [office.entity_id]
    result = await setup.flow.async_step_init(submitted)

    assert result["data"][CONF_ALLOW_ALL_SATELLITES] is True
    assert result["data"][CONF_ALLOWED_SATELLITES] == [office.id]


async def test_options_can_clear_last_satellite_and_preserve_disabled_selection(
    options_setup, monkeypatch
):
    setup = options_setup
    bedroom = SimpleNamespace(
        id="stable-bedroom-id",
        domain="assist_satellite",
        entity_id="assist_satellite.bedroom",
        disabled_by="user",
    )
    registry = SimpleNamespace(
        entities={"bedroom": bedroom},
        async_get=Mock(return_value=bedroom),
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "entity_registry",
        SimpleNamespace(async_get=Mock(return_value=registry)),
        raising=False,
    )
    setup.entry.options[CONF_ALLOWED_SATELLITES] = [bedroom.id]

    form = await setup.flow.async_step_init()
    field = next(
        key for key in form["data_schema"].schema if key == CONF_ALLOWED_SATELLITES
    )
    assert field.description == {"suggested_value": [bedroom.entity_id]}
    initial = form["data_schema"]({CONF_ALLOWED_SATELLITES: [bedroom.entity_id]})
    preserved = await setup.flow.async_step_init(initial)
    assert preserved["data"][CONF_ALLOWED_SATELLITES] == [bedroom.id]

    form = await setup.flow.async_step_init()
    result = await setup.flow.async_step_init(form["data_schema"]({}))

    assert result["data"][CONF_ALLOWED_SATELLITES] == []


@pytest.mark.parametrize("enabled", [False, True])
async def test_shared_memory_option_is_independent_and_round_trips(
    options_setup, enabled
):
    setup = options_setup
    setup.entry.options.update(
        enable_shared_memory=enabled, enable_device_control=False
    )
    form = await setup.flow.async_step_init()
    submitted = form["data_schema"]({})
    assert submitted["enable_shared_memory"] is enabled
    result = await setup.flow.async_step_init(submitted)
    assert result["data"]["enable_shared_memory"] is enabled
    assert result["data"][CONF_LLM_HASS_API] is None


async def test_downgrade_allows_disabling_unavailable_administrator_tools(
    options_setup,
):
    setup = options_setup
    setup.entry.options[CONF_ENABLE_HOME_ASSISTANT_API] = True
    form = await setup.flow.async_step_init()
    assert form["data_schema"]({})[CONF_ENABLE_HOME_ASSISTANT_API] is True
    rejected = await setup.flow.async_step_init(form["data_schema"]({}))
    assert rejected["errors"] == {
        CONF_ENABLE_HOME_ASSISTANT_API: "home_assistant_api_unavailable"
    }
    setup.flow.hass.config_entries.async_update_entry.assert_not_called()
    result = await setup.flow.async_step_init(
        rejected["data_schema"]({CONF_ENABLE_HOME_ASSISTANT_API: False})
    )
    assert result["data"][CONF_LLM_HASS_API] == ["assist"]
    assert result["data"][CONF_ENABLE_HOME_ASSISTANT_API] is False


@pytest.mark.parametrize(
    ("saved", "available", "expected", "error"),
    [
        ("claude-sonnet-4.5", ["claude-sonnet-4.5"], "claude-sonnet-4.5", None),
        (
            "claude-sonnet-4.5",
            ["claude-opus-6", "claude-sonnet-5"],
            "claude-sonnet-5",
            "model_unavailable_fallback",
        ),
        ("claude-sonnet-4.5", ["gpt-6-sol"], "gpt-6-sol", "model_unavailable"),
    ],
)
async def test_options_default_is_always_selectable(
    ha_boundary, saved, available, expected, error
):
    module = ha_boundary.load("config_flow")
    entry = SimpleNamespace(entry_id="entry", options={CONF_MODEL: saved}, data={})
    flow = module.GitHubCopilotOptionsFlow(entry)
    runtime = SimpleNamespace(
        is_started=True,
        async_list_models=AsyncMock(
            return_value=[SimpleNamespace(id=model_id) for model_id in available]
        ),
    )
    flow.hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    form = await flow.async_step_init()
    runtime.async_list_models.assert_awaited_once_with(force_refresh=True)
    values = form["data_schema"]({})
    assert values[CONF_MODEL] == expected
    assert form["errors"] == (
        {"base": error, "ai_task_model": "task_model_unavailable"} if error else {}
    )
    if error:
        assert form["description_placeholders"]["model"] == saved
        with pytest.raises(vol.Invalid):
            form["data_schema"]({CONF_MODEL: saved})


async def test_reopening_options_refreshes_models_and_empty_list_aborts(ha_boundary):
    module = ha_boundary.load("config_flow")
    entry = SimpleNamespace(entry_id="entry", options={CONF_MODEL: "gpt-6-sol"})
    runtime = SimpleNamespace(
        is_started=True,
        async_list_models=AsyncMock(
            side_effect=[[SimpleNamespace(id="gpt-6-sol")], []]
        ),
    )
    for expected in ("form", "abort"):
        flow = module.GitHubCopilotOptionsFlow(entry)
        flow.hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
        result = await flow.async_step_init()
        assert result["type"] == expected
    assert result["reason"] == "no_models"
    assert runtime.async_list_models.await_count == 2


async def test_options_reports_discovery_quota(ha_boundary):
    module = ha_boundary.load("config_flow")
    flow = module.GitHubCopilotOptionsFlow(
        SimpleNamespace(entry_id="entry", options={CONF_MODEL: "gpt-6-sol"})
    )
    runtime = SimpleNamespace(
        is_started=True,
        async_list_models=AsyncMock(side_effect=RuntimeError("Quota exceeded")),
    )
    flow.hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    assert await flow.async_step_init() == {"type": "abort", "reason": "quota_exceeded"}


async def test_conversation_model_change_does_not_change_task_model(options_setup):
    setup = options_setup
    form = await setup.flow.async_step_init()
    result = await setup.flow.async_step_init(
        form["data_schema"]({CONF_MODEL: "gpt-6.1-sol", "ai_task_timeout": 240})
    )
    assert result["data"]["ai_task_model"] == "gpt-6-sol"
    assert result["data"]["ai_task_timeout"] == 240
    assert setup.entry.data["ai_task_model_billing"]["model"] == "gpt-6-sol"


@pytest.mark.parametrize("timeout", [0, -1, "invalid"])
async def test_invalid_task_timeout_is_rejected(options_setup, timeout):
    form = await options_setup.flow.async_step_init()
    with pytest.raises(vol.Invalid):
        form["data_schema"]({"ai_task_timeout": timeout})


async def test_mcp_servers_bind_entries_keep_unloaded_and_reselect_replaced(
    options_setup, monkeypatch
):
    class ModelContextProtocolAPI:
        name = "Notes"

        def __init__(self, api_id, entry_id):
            self.id = api_id
            self.coordinator = SimpleNamespace(
                config_entry=SimpleNamespace(entry_id=entry_id)
            )

    ModelContextProtocolAPI.__module__ = "homeassistant.components.mcp"
    llm = sys.modules["homeassistant.helpers.llm"]
    apis = [
        ModelContextProtocolAPI("mcp-notes", "entry-new"),
        ModelContextProtocolAPI("mcp-fresh", "entry-fresh"),
    ]
    monkeypatch.setattr(llm, "async_get_apis", lambda hass: apis, raising=False)
    setup = options_setup
    setup.entry.options.update(
        mcp_servers=["mcp-offline", "mcp-notes"],
        mcp_write_servers=["mcp-notes"],
        mcp_server_entries={"mcp-offline": "entry-offline", "mcp-notes": "entry-old"},
    )
    form = await setup.flow.async_step_init()
    suggested = {
        str(key): key.description["suggested_value"]
        for key in form["data_schema"].schema
        if str(key) in ("mcp_servers", "mcp_write_servers")
    }

    # The replaced mcp-notes entry is not preselected; mcp-offline keeps its grant.
    assert suggested == {"mcp_servers": ["mcp-offline"], "mcp_write_servers": []}
    submitted = form["data_schema"](
        {
            "mcp_servers": ["mcp-offline", "mcp-fresh"],
            "mcp_write_servers": ["mcp-notes", "mcp-offline"],
        }
    )

    result = await setup.flow.async_step_init(submitted)

    assert result["data"]["mcp_servers"] == ["mcp-offline", "mcp-fresh"]
    assert result["data"]["mcp_write_servers"] == ["mcp-offline"]
    assert result["data"]["mcp_server_entries"] == {
        "mcp-offline": "entry-offline",
        "mcp-fresh": "entry-fresh",
    }
    with pytest.raises(vol.Invalid):
        form["data_schema"]({"mcp_servers": ["mcp-unknown"]})
