"""OAuth flow lifecycle and account binding regressions."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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
    result = await setup.flow.async_step_model({"model": "gpt-6-luna"})
    assert result["data"]["account_id"] == "123"
    setup.flow.async_remove()


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
    entry = SimpleNamespace(
        entry_id="entry",
        options={"model": "keep"},
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
    assert entry.options == {"model": "keep"}


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
