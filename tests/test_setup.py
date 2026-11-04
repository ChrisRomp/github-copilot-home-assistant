"""Config-entry setup cleanup and Home Assistant shutdown contracts."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.github_copilot import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.github_copilot import runtime as runtime_module
from custom_components.github_copilot.const import (
    DATA_CREDENTIAL_LOCKS,
    DATA_RUNTIME_PRUNE,
    DOMAIN,
)


@pytest.fixture
async def setup_entry(ha_boundary, monkeypatch, tmp_path):
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
        is_running=True,
        is_stopping=False,
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        async_create_background_task=Mock(
            side_effect=lambda coro, name: create_background_task(None, coro, name)
        ),
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
        setup.entry, ["conversation", "ai_task"]
    )
    assert setup.hass.bus.async_listen_once.call_count == 2
    assert all(
        call.args[0] == "homeassistant_stop"
        for call in setup.hass.bus.async_listen_once.call_args_list
    )
    setup.hass.async_create_background_task.assert_called_once()
    assert DATA_RUNTIME_PRUNE in setup.hass.data
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
    listener = setup.hass.bus.async_listen_once.call_args_list[0].args[1]
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


async def test_entries_and_reload_share_one_cache_cleanup(setup_entry):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    assert await async_unload_entry(setup.hass, setup.entry)
    await async_setup_entry(setup.hass, setup.entry)
    second = SimpleNamespace(**{**vars(setup.entry), "entry_id": "second"})
    await async_setup_entry(setup.hass, second)
    setup.hass.async_create_background_task.assert_called_once()
    assert DATA_RUNTIME_PRUNE in setup.hass.data


async def test_failed_unload_keeps_runtime_and_shutdown_listener(setup_entry):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    setup.hass.config_entries.async_unload_platforms.return_value = False
    assert not await async_unload_entry(setup.hass, setup.entry)
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    setup.runtime.async_stop.assert_not_awaited()
    setup.hass.bus.async_listen_once.return_value.assert_not_called()


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_stop_failure_keeps_unloaded_runtime_registered_for_retry(
    setup_entry, error_type
):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    error = error_type("process still running")
    setup.runtime.async_stop.side_effect = error

    with pytest.raises(error_type) as raised:
        await async_unload_entry(setup.hass, setup.entry)
    assert raised.value is error
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime

    setup.runtime.async_stop.side_effect = None
    assert await async_unload_entry(setup.hass, setup.entry)
    assert DOMAIN not in setup.hass.data
    assert setup.runtime.async_stop.await_count == 2


async def test_concurrent_unloads_keep_stopping_entries_registered(setup_entry):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    second_entry = SimpleNamespace(entry_id="second")
    second_runtime = SimpleNamespace(async_stop=AsyncMock())
    setup.hass.data[DOMAIN]["second"] = second_runtime
    entered = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]

    async def stop(index):
        entered[index].set()
        await finish[index].wait()

    async def stop_first():
        await stop(0)

    async def stop_second():
        await stop(1)

    setup.runtime.async_stop.side_effect = stop_first
    second_runtime.async_stop.side_effect = stop_second
    tasks = [
        asyncio.create_task(async_unload_entry(setup.hass, entry))
        for entry in (setup.entry, second_entry)
    ]
    await asyncio.gather(*(event.wait() for event in entered))
    assert set(setup.hass.data[DOMAIN]) == {"entry", "second"}
    finish[0].set()
    assert await tasks[0]
    assert setup.hass.data[DOMAIN] == {"second": second_runtime}
    finish[1].set()
    assert await tasks[1]
    assert DOMAIN not in setup.hass.data


async def test_concurrent_unloads_of_same_entry_are_safe(setup_entry):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def stop():
        if setup.runtime.async_stop.await_count == 2:
            entered.set()
        await finish.wait()

    setup.runtime.async_stop.side_effect = stop
    tasks = [
        asyncio.create_task(async_unload_entry(setup.hass, setup.entry))
        for _ in range(2)
    ]
    await entered.wait()
    finish.set()
    assert await asyncio.gather(*tasks) == [True, True]
    assert DOMAIN not in setup.hass.data


async def test_unload_does_not_remove_replacement_runtime(setup_entry):
    setup = setup_entry
    await async_setup_entry(setup.hass, setup.entry)
    replacement = object()

    async def replace_runtime():
        setup.hass.data[DOMAIN] = {"entry": replacement}

    setup.runtime.async_stop.side_effect = replace_runtime
    assert await async_unload_entry(setup.hass, setup.entry)
    assert setup.hass.data[DOMAIN] == {"entry": replacement}


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
    setup.hass.async_create_background_task.assert_not_called()
    assert DATA_RUNTIME_PRUNE not in setup.hass.data


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
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    assert "Failed to stop the Copilot runtime after setup failed" in caplog.text


async def test_setup_retry_stops_retained_runtime_before_starting_replacement(
    setup_entry,
):
    from custom_components.github_copilot import runtime as runtime_module

    setup = setup_entry
    previous = SimpleNamespace(async_stop=AsyncMock())
    setup.hass.data[DOMAIN] = {"entry": previous}
    factory = runtime_module.CopilotRuntime

    async def stop():
        factory.assert_not_called()
        assert setup.hass.data[DOMAIN]["entry"] is previous

    previous.async_stop.side_effect = stop
    assert await async_setup_entry(setup.hass, setup.entry)
    previous.async_stop.assert_awaited_once()
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    setup.runtime.async_start.assert_awaited_once()


@pytest.mark.parametrize(
    "error_type", [OSError, RuntimeError, ValueError, asyncio.CancelledError]
)
async def test_setup_retry_cannot_overwrite_failed_cleanup(setup_entry, error_type):
    from homeassistant.exceptions import ConfigEntryNotReady

    from custom_components.github_copilot import runtime as runtime_module

    setup = setup_entry
    error = error_type("process still running")
    previous = SimpleNamespace(async_stop=AsyncMock(side_effect=error))
    setup.hass.data[DOMAIN] = {"entry": previous}

    expected = ConfigEntryNotReady if error_type is OSError else error_type
    with pytest.raises(expected) as raised:
        await async_setup_entry(setup.hass, setup.entry)
    if error_type is OSError:
        assert raised.value.__cause__ is error
    else:
        assert raised.value is error
    assert setup.hass.data[DOMAIN]["entry"] is previous
    runtime_module.CopilotRuntime.assert_not_called()


async def test_remove_stops_runtime_retained_after_failed_setup(
    setup_entry, monkeypatch
):
    from custom_components.github_copilot import runtime as runtime_module

    setup = setup_entry
    setup.runtime.async_start.side_effect = ValueError("setup failed")
    setup.runtime.async_stop.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(ValueError, match="setup failed"):
        await async_setup_entry(setup.hass, setup.entry)
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    setup.runtime.async_stop.side_effect = None

    def remove_directory(hass, entry_id):
        assert setup.runtime.async_stop.await_count == 2
        assert DOMAIN not in hass.data
        assert entry_id == setup.entry.entry_id

    remove = Mock(side_effect=remove_directory)
    monkeypatch.setattr(runtime_module, "remove_entry_directory", remove)
    await async_remove_entry(setup.hass, setup.entry)
    remove.assert_called_once_with(setup.hass, setup.entry.entry_id)
    assert DOMAIN not in setup.hass.data


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_remove_preserves_state_when_retained_runtime_cannot_stop(
    setup_entry, monkeypatch, error_type
):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.github_copilot import runtime as runtime_module

    setup = setup_entry
    error = error_type("still running")
    setup.runtime.async_stop.side_effect = error
    lock = object()
    setup.hass.data = {
        DOMAIN: {"entry": setup.runtime},
        DATA_CREDENTIAL_LOCKS: {"entry": lock},
    }
    remove = Mock()
    monkeypatch.setattr(runtime_module, "remove_entry_directory", remove)
    with pytest.raises(error_type) as raised:
        await async_remove_entry(setup.hass, setup.entry)
    assert raised.value is error
    remove.assert_not_called()
    ir.async_delete_issue.assert_not_called()
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    assert setup.hass.data[DATA_CREDENTIAL_LOCKS]["entry"] is lock


async def test_remove_unloaded_entry_preserves_other_runtime_and_lock(
    setup_entry, monkeypatch
):
    from custom_components.github_copilot import runtime as runtime_module

    setup = setup_entry
    other = SimpleNamespace(async_stop=AsyncMock())
    other_lock = object()
    setup.hass.data = {
        DOMAIN: {"other": other},
        DATA_CREDENTIAL_LOCKS: {"entry": object(), "other": other_lock},
    }
    remove = Mock()
    monkeypatch.setattr(runtime_module, "remove_entry_directory", remove)
    await async_remove_entry(setup.hass, setup.entry)
    remove.assert_called_once_with(setup.hass, setup.entry.entry_id)
    other.async_stop.assert_not_awaited()
    assert setup.hass.data[DOMAIN] == {"other": other}
    assert setup.hass.data[DATA_CREDENTIAL_LOCKS] == {"other": other_lock}


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
    assert setup.hass.data[DOMAIN]["entry"] is setup.runtime
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    setup.runtime.async_stop.assert_awaited_once()
    assert DOMAIN not in setup.hass.data


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
