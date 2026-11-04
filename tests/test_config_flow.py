"""Initial configuration, OAuth lifecycle, and account binding regressions."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol

from custom_components.github_copilot.auth import OAuthError


@pytest.fixture
def flow_setup(ha_boundary, monkeypatch):
    module = ha_boundary.load("config_flow")
    flow = module.GitHubCopilotConfigFlow()
    flow.hass = SimpleNamespace(
        data={},
        async_create_task=lambda coro, *args: asyncio.create_task(coro),
        config_entries=SimpleNamespace(
            async_update_entry=Mock(),
            async_schedule_reload=Mock(),
        ),
    )
    device = AsyncMock(
        return_value={
            "device_code": "opaque",
            "user_code": "ABCD",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 60,
        }
    )
    identity = AsyncMock(return_value="123")
    probe = AsyncMock(return_value=[SimpleNamespace(id="gpt-6-luna")])
    monkeypatch.setattr(module, "request_device_code", device)
    monkeypatch.setattr(module, "async_get_account_id", identity)
    monkeypatch.setattr(module, "async_probe_models", probe)
    return SimpleNamespace(
        module=module, flow=flow, device=device, identity=identity, probe=probe
    )


async def test_oauth_progress_reuses_one_poll_and_returns_model_form(
    flow_setup, monkeypatch
):
    setup = flow_setup
    finish = asyncio.Event()

    async def poll(*args, **kwargs):
        await finish.wait()
        return {"auth_method": "oauth", "access_token": "test-token"}

    poll_mock = AsyncMock(side_effect=poll)
    monkeypatch.setattr(setup.module, "poll_for_device_token", poll_mock)
    first, second = await asyncio.gather(
        setup.flow.async_step_oauth(), setup.flow.async_step_oauth({})
    )
    assert first["type"] == second["type"] == "progress"
    assert first["progress_task"] is second["progress_task"]
    assert first["description_placeholders"]["user_code"] == "ABCD"
    component = Path(__file__).parents[1] / "custom_components/github_copilot"
    for filename in ("strings.json", "translations/en.json"):
        translations = json.loads((component / filename).read_text())
        progress_text = translations["config"]["progress"][first["progress_action"]]
        rendered = progress_text.format(**first["description_placeholders"])
        assert "[GitHub device activation](https://github.com/login/device)" in rendered
        assert "**ABCD**" in rendered
        assert "automatically" in rendered
        assert "cancel" in rendered
        assert "opaque" not in rendered
    setup.device.assert_awaited_once()
    finish.set()
    await first["progress_task"]
    poll_mock.assert_awaited_once()
    assert (await setup.flow.async_step_oauth())["next_step_id"] == "oauth_complete"
    form = await setup.flow.async_step_oauth_complete()
    assert form["step_id"] == "model"
    submitted = form["data_schema"]({"model": "gpt-6-luna"})
    assert submitted == {
        "model": "gpt-6-luna",
        "enable_device_control": False,
        "ai_task_timeout": 180,
    }
    result = await setup.flow.async_step_model(submitted)
    assert result["data"]["account_id"] == "123"
    assert result["options"] == {
        "model": "gpt-6-luna",
        "ai_task_model": "gpt-6-luna",
        "ai_task_timeout": 180,
        "enable_device_control": False,
        "llm_hass_api": None,
        "prompt": "",
    }
    setup.flow.async_remove()


@pytest.mark.parametrize("auth_method", ["oauth", "pat"])
@pytest.mark.parametrize("device_control_enabled", [False, True])
@pytest.mark.parametrize("prompt", [None, "", "Keep {{ user }} instructions literal."])
async def test_initial_conversation_options_are_saved(
    flow_setup, monkeypatch, auth_method, device_control_enabled, prompt
):
    setup = flow_setup
    setup.probe.return_value = [
        SimpleNamespace(id="gpt-6-luna", billing=SimpleNamespace(multiplier=0.5))
    ]
    if auth_method == "oauth":
        monkeypatch.setattr(
            setup.module,
            "poll_for_device_token",
            AsyncMock(
                return_value={"auth_method": "oauth", "access_token": "test-token"}
            ),
        )
        progress = await setup.flow.async_step_oauth()
        await progress["progress_task"]
        form = await setup.flow.async_step_oauth_complete()
    else:
        form = await setup.flow.async_step_pat({"token": "github_pat_test"})

    assert form["step_id"] == "model"
    user_input = {
        "model": "gpt-6-luna",
        "enable_device_control": device_control_enabled,
    }
    if prompt is not None:
        user_input["prompt"] = prompt
    result = await setup.flow.async_step_model(form["data_schema"](user_input))

    assert result["type"] == "create_entry"
    assert result["options"] == {
        "model": "gpt-6-luna",
        "ai_task_model": "gpt-6-luna",
        "ai_task_timeout": 180,
        "enable_device_control": device_control_enabled,
        "llm_hass_api": ["assist"] if device_control_enabled else None,
        "prompt": prompt if prompt is not None else "",
    }
    assert result["data"]["auth_method"] == auth_method
    assert result["data"]["account_id"] == "123"
    assert result["data"]["model_billing"] == {
        "model": "gpt-6-luna",
        "multiplier": 0.5,
    }
    assert result["data"]["ai_task_model_billing"] == result["data"]["model_billing"]
    setup.flow.async_remove()


async def test_initial_options_require_an_available_model_and_default_control_off(
    flow_setup,
):
    form = await flow_setup.flow.async_step_pat({"token": "github_pat_test"})
    schema = form["data_schema"]
    assert schema({"model": "gpt-6-luna"}) == {
        "model": "gpt-6-luna",
        "ai_task_timeout": 180,
        "enable_device_control": False,
    }
    with pytest.raises(vol.Invalid):
        schema({})
    with pytest.raises(vol.Invalid):
        schema({"model": "unavailable"})
    prompt_field = next(key for key in schema.schema if key == "prompt")
    assert prompt_field.description == {"suggested_value": ""}


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_initial_options_have_matching_translations(filename):
    component = Path(__file__).parents[1] / "custom_components/github_copilot"
    translations = json.loads((component / filename).read_text())
    setup = translations["config"]["step"]["model"]
    options = translations["options"]["step"]["init"]
    assert setup["data"] == options["data"]
    assert setup["data_description"] == options["data_description"]


async def test_closing_oauth_flow_cancels_poll_and_never_saves(flow_setup, monkeypatch):
    setup = flow_setup
    entered = asyncio.Event()

    async def poll(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(setup.module, "poll_for_device_token", poll)
    result = await setup.flow.async_step_oauth()
    await entered.wait()
    setup.flow.async_remove()
    with pytest.raises(asyncio.CancelledError):
        await result["progress_task"]
    assert (await setup.flow.async_step_oauth_complete())["type"] == "abort"
    assert setup.flow._credentials == {}
    setup.probe.assert_not_awaited()
    setup.flow.hass.config_entries.async_update_entry.assert_not_called()


async def test_late_validation_cannot_create_entry_after_abort(flow_setup):
    setup = flow_setup
    entered, finish = asyncio.Event(), asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        await finish.wait()
        return [SimpleNamespace(id="gpt-6-luna")]

    setup.probe.side_effect = probe
    task = asyncio.create_task(setup.flow.async_step_pat({"token": "github_pat_test"}))
    await entered.wait()
    setup.flow.async_remove()
    finish.set()
    assert (await task)["type"] == "abort"
    assert (await setup.flow.async_step_model({"model": "gpt-6-luna"}))[
        "type"
    ] == "abort"
    setup.flow.hass.config_entries.async_update_entry.assert_not_called()


async def test_different_account_reauth_is_rejected_before_copilot_probe(flow_setup):
    setup = flow_setup
    setup.flow._reauth_entry = SimpleNamespace(data={"account_id": "456"})
    result = await setup.flow.async_step_pat({"token": "github_pat_test"})
    assert result == {"type": "abort", "reason": "account_mismatch"}
    setup.probe.assert_not_awaited()
    setup.flow.hass.config_entries.async_update_entry.assert_not_called()


async def test_same_account_reauth_preserves_settings_and_reloads_once(flow_setup):
    setup = flow_setup
    options = {
        "model": "keep",
        "enable_device_control": True,
        "llm_hass_api": ["assist"],
        "prompt": "Keep these instructions.",
    }
    entry = SimpleNamespace(
        entry_id="entry",
        options=dict(options),
        data={
            "account_id": "123",
            "refresh_token": "old",
            "access_token_expires_at": 100,
            "unrelated": "keep",
        },
    )
    setup.flow._reauth_entry = entry
    assert await setup.flow.async_step_pat({"token": "github_pat_test"}) == {
        "type": "abort",
        "reason": "reauth_successful",
    }
    update = setup.flow.hass.config_entries.async_update_entry
    update.assert_called_once()
    assert update.call_args.kwargs["data"] == {
        "account_id": "123",
        "unrelated": "keep",
        "auth_method": "pat",
        "access_token": "github_pat_test",
    }
    setup.flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
        "entry"
    )
    assert entry.options == options


async def test_poll_failure_is_reported(flow_setup, monkeypatch):
    setup = flow_setup
    monkeypatch.setattr(
        setup.module,
        "poll_for_device_token",
        AsyncMock(side_effect=OAuthError("Denied")),
    )
    result = await setup.flow.async_step_oauth()
    await asyncio.gather(result["progress_task"], return_exceptions=True)
    assert await setup.flow.async_step_oauth_complete() == {
        "type": "abort",
        "reason": "oauth_failed",
    }
    setup.flow.async_remove()


async def test_successful_reauth_supersedes_inflight_refresh(flow_setup, monkeypatch):
    from custom_components.github_copilot import auth

    setup = flow_setup
    entered, finish = asyncio.Event(), asyncio.Event()
    entry = SimpleNamespace(
        entry_id="entry",
        data={
            "account_id": "123",
            "auth_method": "oauth",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "access_token_expires_at": 1,
            "model_billing": {"model": "keep", "multiplier": 0.5},
        },
        options={"model": "keep"},
    )
    setup.flow._reauth_entry = entry
    setup.flow.hass.config_entries.async_update_entry.side_effect = (
        lambda entry, *, data: setattr(entry, "data", data)
    )

    async def refresh(*args, **kwargs):
        entered.set()
        await finish.wait()
        return {
            "auth_method": "oauth",
            "access_token": "stale-rotated-access",
            "refresh_token": "stale-rotated-refresh",
            "access_token_expires_at": 9999999999,
        }

    monkeypatch.setattr(auth, "refresh_oauth_token", refresh)
    store = auth.CredentialStore(setup.flow.hass, entry, "client")
    pending = asyncio.create_task(store.async_get_access_token())
    await entered.wait()
    result = await setup.flow.async_step_pat({"token": "github_pat_replacement"})
    assert result == {"type": "abort", "reason": "reauth_successful"}
    finish.set()
    assert await pending == "github_pat_replacement"
    assert entry.data["auth_method"] == "pat"
    assert "refresh_token" not in entry.data
    assert "access_token_expires_at" not in entry.data
    assert entry.data["model_billing"] == {"model": "keep", "multiplier": 0.5}
    setup.flow.hass.config_entries.async_update_entry.assert_called_once()
    setup.flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
        "entry"
    )


@pytest.mark.parametrize("runtime_loaded", [False, True])
async def test_rejected_pat_discovery_starts_account_bound_reauth(
    flow_setup, monkeypatch, runtime_loaded
):
    from custom_components.github_copilot.auth import ReauthenticationRequired

    setup = flow_setup
    entry = SimpleNamespace(
        entry_id="entry",
        data={
            "account_id": "123",
            "auth_method": "pat",
            "access_token": "revoked-pat",
        },
        options={"model": "keep"},
        async_start_reauth=Mock(),
    )
    flow = setup.module.GitHubCopilotOptionsFlow(entry)
    flow.hass = setup.flow.hass
    discovery = AsyncMock(side_effect=ReauthenticationRequired("GitHub rejected token"))
    if runtime_loaded:
        flow.hass.data["github_copilot"] = {
            entry.entry_id: SimpleNamespace(
                is_started=True, async_list_models=discovery
            )
        }
    else:
        monkeypatch.setattr(setup.module, "async_probe_models", discovery)
    assert await flow.async_step_init() == {
        "type": "abort",
        "reason": "reauth_required",
    }
    entry.async_start_reauth.assert_called_once_with(flow.hass)
    assert entry.data["account_id"] == "123"
    assert entry.options["model"] == "keep"
    flow.hass.config_entries.async_update_entry.assert_not_called()
