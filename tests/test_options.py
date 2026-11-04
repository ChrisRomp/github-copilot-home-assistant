"""Options selection tests with the HA flow boundary replaced by a double."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT

from custom_components.github_copilot.const import (
    CONF_ENABLE_DEVICE_CONTROL,
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
        CONF_LLM_HASS_API: ["assist"],
        CONF_PROMPT: "Updated instructions",
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
