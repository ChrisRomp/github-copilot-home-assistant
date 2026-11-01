"""Options selection tests with the HA flow boundary replaced by a double."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from custom_components.github_copilot.const import CONF_MODEL, DOMAIN


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
    assert form["errors"] == ({"base": error} if error else {})
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
