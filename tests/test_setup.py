"""Config-entry setup cleanup and Home Assistant shutdown contracts."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.github_copilot import async_setup_entry, async_unload_entry
from custom_components.github_copilot.const import DOMAIN


@pytest.fixture
async def setup_entry(ha_boundary, monkeypatch):
    from custom_components.github_copilot import runtime as runtime_module

    monkeypatch.setattr(
        sys.modules["homeassistant.const"],
        "EVENT_HOMEASSISTANT_STOP",
        "homeassistant_stop",
        raising=False,
    )
    runtime = SimpleNamespace(
        async_start=AsyncMock(),
        async_stop=AsyncMock(),
        backend=SimpleNamespace(async_check_model=AsyncMock()),
    )
    monkeypatch.setattr(runtime_module, "CopilotRuntime", Mock(return_value=runtime))
    callbacks = []
    tasks = []

    def create_background_task(hass, coro, name):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    entry = SimpleNamespace(
        entry_id="entry",
        async_on_unload=Mock(side_effect=callbacks.append),
        add_update_listener=Mock(return_value=Mock()),
        async_create_background_task=Mock(side_effect=create_background_task),
    )
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
        bus=SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
    )
    yield SimpleNamespace(hass=hass, entry=entry, runtime=runtime, callbacks=callbacks)
    await asyncio.gather(*tasks)


async def test_successful_setup_registers_shutdown_and_unload_cleanup(setup_entry):
    setup = setup_entry
    assert await async_setup_entry(setup.hass, setup.entry)
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    setup.runtime.async_start.assert_awaited_once()
    setup.hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        setup.entry, ["conversation"]
    )
    setup.hass.bus.async_listen_once.assert_called_once()
    assert setup.hass.bus.async_listen_once.call_args.args[0] == "homeassistant_stop"
    setup.entry.async_create_background_task.assert_called_once()
    setup.runtime.async_stop.assert_not_awaited()

    assert await async_unload_entry(setup.hass, setup.entry)
    setup.runtime.async_stop.assert_awaited_once()
    assert DOMAIN not in setup.hass.data

    # Core runs entry callbacks after the integration's unload hook succeeds.
    for callback in reversed(setup.callbacks):
        callback()
    setup.hass.bus.async_listen_once.return_value.assert_called_once_with()
    setup.entry.add_update_listener.return_value.assert_called_once_with()


async def test_shutdown_listener_awaits_runtime_cleanup(setup_entry):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    listener = setup.hass.bus.async_listen_once.call_args.args[1]
    stopping = asyncio.Event()
    stopped = asyncio.Event()

    async def stop():
        stopping.set()
        await stopped.wait()

    setup.runtime.async_stop.side_effect = stop
    task = asyncio.create_task(listener(SimpleNamespace()))
    await stopping.wait()
    assert not task.done()
    stopped.set()
    await task
    setup.runtime.async_stop.assert_awaited_once()


async def test_failed_unload_keeps_runtime_and_shutdown_listener(setup_entry):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    setup.hass.config_entries.async_unload_platforms.return_value = False
    assert not await async_unload_entry(setup.hass, setup.entry)
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    setup.runtime.async_stop.assert_not_awaited()
    setup.hass.bus.async_listen_once.return_value.assert_not_called()


@pytest.mark.parametrize("stage", ["start", "forward"])
@pytest.mark.parametrize(
    "error_type",
    [ValueError, RuntimeError, ModuleNotFoundError, asyncio.CancelledError],
)
async def test_setup_preserves_unexpected_errors_and_cancellation(
    setup_entry, stage, error_type
):
    setup = setup_entry
    error = error_type("original setup failure")
    if stage == "start":
        setup.runtime.async_start.side_effect = error
    else:
        setup.hass.config_entries.async_forward_entry_setups.side_effect = error
    with pytest.raises(error_type) as raised:
        await async_setup_entry(setup.hass, setup.entry)
    assert raised.value is error
    setup.runtime.async_stop.assert_awaited_once()
    assert DOMAIN not in setup.hass.data
    setup.entry.async_create_background_task.assert_not_called()


@pytest.mark.parametrize("error_type", [OSError, TimeoutError, ConnectionError])
async def test_setup_translates_known_transport_failures(setup_entry, error_type):
    from homeassistant.exceptions import ConfigEntryNotReady

    setup = setup_entry
    error = error_type("transient failure")
    setup.runtime.async_start.side_effect = error
    with pytest.raises(ConfigEntryNotReady) as raised:
        await async_setup_entry(setup.hass, setup.entry)
    assert raised.value.__cause__ is error
    setup.runtime.async_stop.assert_awaited_once()
    assert DOMAIN not in setup.hass.data


async def test_setup_translates_known_download_failure(setup_entry):
    from homeassistant.exceptions import ConfigEntryNotReady

    from custom_components.github_copilot.provisioning import RuntimeDownloadError

    setup = setup_entry
    error = RuntimeDownloadError("download temporarily unavailable")
    setup.runtime.async_start.side_effect = error
    with pytest.raises(ConfigEntryNotReady) as raised:
        await async_setup_entry(setup.hass, setup.entry)
    assert raised.value.__cause__ is error
    setup.runtime.async_stop.assert_awaited_once()
    assert DOMAIN not in setup.hass.data


@pytest.mark.parametrize("cleanup_error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("setup_error", [ValueError, OSError, asyncio.CancelledError])
async def test_cleanup_failure_does_not_replace_setup_failure(
    setup_entry, cleanup_error, setup_error, caplog
):
    from homeassistant.exceptions import ConfigEntryNotReady

    setup = setup_entry
    error = setup_error("original failure")
    setup.runtime.async_start.side_effect = error
    setup.runtime.async_stop.side_effect = cleanup_error("cleanup failure")
    expected = ConfigEntryNotReady if setup_error is OSError else setup_error
    with pytest.raises(expected) as raised:
        await async_setup_entry(setup.hass, setup.entry)
    if setup_error is OSError:
        assert raised.value.__cause__ is error
    else:
        assert raised.value is error
    assert DOMAIN not in setup.hass.data
    assert "Failed to stop the Copilot runtime after setup failed" in caplog.text


async def test_cancelled_setup_waits_for_cleanup(setup_entry):
    setup = setup_entry
    starting = asyncio.Event()
    stopping = asyncio.Event()
    stopped = asyncio.Event()

    async def start():
        starting.set()
        await asyncio.Event().wait()

    async def stop():
        stopping.set()
        await stopped.wait()

    setup.runtime.async_start.side_effect = start
    setup.runtime.async_stop.side_effect = stop
    task = asyncio.create_task(async_setup_entry(setup.hass, setup.entry))
    await starting.wait()
    task.cancel()
    await stopping.wait()
    assert not task.done()
    assert DOMAIN not in setup.hass.data
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    setup.runtime.async_stop.assert_awaited_once()


async def test_setup_failure_preserves_other_entry_state(setup_entry):
    setup = setup_entry
    other = object()
    setup.hass.data = {DOMAIN: {"other": other}, "unrelated": "state"}
    setup.runtime.async_start.side_effect = ValueError("setup failure")
    with pytest.raises(ValueError):
        await async_setup_entry(setup.hass, setup.entry)
    assert setup.hass.data == {DOMAIN: {"other": other}, "unrelated": "state"}


async def test_failure_registering_listeners_still_stops_runtime(setup_entry):
    setup = setup_entry
    error = ValueError("listener failure")
    setup.entry.add_update_listener.side_effect = error
    with pytest.raises(ValueError) as raised:
        await async_setup_entry(setup.hass, setup.entry)
    assert raised.value is error
    setup.runtime.async_stop.assert_awaited_once()
    assert DOMAIN not in setup.hass.data

    # Core also processes registered callbacks for a failed setup attempt.
    for callback in reversed(setup.callbacks):
        callback()
    setup.hass.bus.async_listen_once.return_value.assert_called_once_with()


async def test_failure_creating_model_check_still_stops_runtime(setup_entry):
    setup = setup_entry
    error = ValueError("background task failure")

    def fail(hass, coro, name):
        coro.close()
        raise error

    setup.entry.async_create_background_task.side_effect = fail
    with pytest.raises(ValueError) as raised:
        await async_setup_entry(setup.hass, setup.entry)
    assert raised.value is error
    setup.runtime.async_stop.assert_awaited_once()
    assert DOMAIN not in setup.hass.data


def test_conversation_dependency_precedes_runtime_startup():
    manifest = json.loads(
        (
            Path(__file__).parents[1] / "custom_components/github_copilot/manifest.json"
        ).read_text()
    )
    assert "conversation" in manifest["dependencies"]


@pytest.mark.skipif(
    os.environ.get("HA_COPILOT_HA_TESTS") != "1",
    reason="Requires the full pinned Home Assistant dependencies",
)
async def test_real_home_assistant_shutdown_awaits_runtime(monkeypatch, tmp_path):
    from homeassistant.core import CoreState, HomeAssistant

    from custom_components.github_copilot import runtime as runtime_module

    hass = HomeAssistant(str(tmp_path))
    stop_states = []

    async def stop():
        await asyncio.sleep(0)
        stop_states.append(hass.state)

    runtime = SimpleNamespace(
        async_start=AsyncMock(),
        async_stop=AsyncMock(side_effect=stop),
        backend=SimpleNamespace(async_check_model=AsyncMock()),
    )
    monkeypatch.setattr(runtime_module, "CopilotRuntime", Mock(return_value=runtime))
    hass.config_entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
    entry = SimpleNamespace(
        entry_id="entry",
        async_on_unload=Mock(),
        add_update_listener=Mock(return_value=Mock()),
        async_create_background_task=lambda hass, coro, name: (
            hass.async_create_background_task(coro, name)
        ),
    )
    try:
        assert await async_setup_entry(hass, entry)
        await hass.async_stop(force=True)
        runtime.async_stop.assert_awaited_once()
        assert stop_states == [CoreState.stopping]
        assert hass.state is CoreState.stopped
    finally:
        if hass.state is not CoreState.stopped:
            await hass.async_stop(force=True)
