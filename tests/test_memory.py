"""Shared memory contracts using real HA Store and real disk, without a model."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import storage as ha_storage
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.github_copilot.const import (
    MEMORY_FORGET_TOOL,
    MEMORY_LIST_TOOL,
    MEMORY_SAVE_TOOL,
)
from custom_components.github_copilot.memory import MemoryManager, MemoryStorageError


@pytest.fixture
async def memory(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    manager = MemoryManager(hass, "entry")
    yield manager
    await manager.async_close()
    await hass.async_stop()


async def save(manager, text="Warm reading lights", revision=0, **kwargs):
    return await manager.async_tool(
        MEMORY_SAVE_TOOL,
        {"text": text, "source": text, "expected_revision": revision, **kwargs},
        f"We prefer {text} from now on.",
    )


async def test_private_atomic_persistence_and_entry_isolation(memory, monkeypatch):
    write_atomic = Mock(wraps=ha_storage.write_utf8_file_atomic)
    monkeypatch.setattr(ha_storage, "write_utf8_file_atomic", write_atomic)
    result = await save(memory)
    assert result["status"] == "saved"
    snapshot = result["snapshot"]
    assert set(snapshot["facts"][0]) == {"id", "text"}
    assert set(snapshot) == {
        "revision",
        "capacity",
        "max_fact_characters",
        "count",
        "facts",
    }
    path = Path(memory.store.path)
    assert path.stat().st_mode & 0o777 == 0o600
    write_atomic.assert_called_once()
    assert write_atomic.call_args.args[0] == str(path)
    assert write_atomic.call_args.args[2] is True
    stored = json.loads(path.read_text())
    fact = stored["data"]["facts"][0]
    assert fact["source"] == "Warm reading lights"
    assert fact["created_at"] == fact["updated_at"]
    assert stored["version"] == 1
    # Both a new HA Store and our non-cached reader must see the committed data.
    assert await Store(memory.hass, 1, memory.key).async_load() == stored["data"]
    await memory.async_close()
    restored = MemoryManager(memory.hass, "entry")
    assert await restored.async_snapshot() == snapshot
    other = MemoryManager(memory.hass, "other")
    assert (await other.async_snapshot())["facts"] == []
    await other.async_close()
    await restored.async_close()


async def test_fresh_ha_runtime_recalls_saved_fact_after_restart(memory, tmp_path):
    from custom_components.github_copilot.runtime import CopilotRuntime

    snapshot = (await save(memory))["snapshot"]
    await memory.async_close()
    fresh_hass = HomeAssistant(str(tmp_path))
    runtime = CopilotRuntime(
        fresh_hass,
        SimpleNamespace(
            entry_id="entry", options={"enable_shared_memory": True}, data={}
        ),
    )
    try:
        assert await runtime.memory.async_snapshot() == snapshot
        await runtime.async_stop()
        assert Path(memory.store.path).is_file()
    finally:
        await fresh_hass.async_stop()


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("", "invalid_input"),
        (" ", "invalid_input"),
        ("x", "saved"),
        ("x" * 300, "saved"),
        ("x" * 301, "invalid_input"),
        (42, "invalid_input"),
        ("\ud800", "invalid_input"),
    ],
)
async def test_fact_and_source_character_boundaries(memory, text, status):
    assert (await save(memory, text))["status"] == status


@pytest.mark.parametrize(
    "arguments",
    [
        {"source": "Invented citation"},
        {"source": ""},
        {"source": ["Warm reading lights"]},
        {"expected_revision": True},
        {"expected_revision": -1},
        {"expected_revision": "0"},
        {"id": "missing"},
        {"id": None},
        {"entry_id": "other"},
        {"path": "/tmp/memory"},
    ],
)
async def test_invalid_arguments_and_fabricated_provenance_never_write(
    memory, arguments
):
    result = await save(memory, **arguments)
    assert result["status"] == "invalid_input"
    assert not Path(memory.store.path).exists()


async def test_quote_is_bounded_exact_current_user_excerpt(memory):
    result = await memory.async_tool(
        MEMORY_SAVE_TOOL,
        {"text": "Reading preference", "source": "q" * 300, "expected_revision": 0},
        "prefix " + "q" * 300 + " suffix",
    )
    assert result["status"] == "saved"
    result = await memory.async_tool(
        MEMORY_SAVE_TOOL,
        {"text": "Another fact", "source": "q" * 301, "expected_revision": 1},
        "q" * 301,
    )
    assert result["status"] == "invalid_input"


async def test_capacity_duplicate_and_same_call_replacement(memory):
    for index in range(50):
        result = await save(memory, f"Fact {index}", revision=index)
        assert result["status"] == "saved"
    snapshot = result["snapshot"]
    assert snapshot["count"] == snapshot["capacity"] == 50
    full = await save(memory, "New fact", revision=50)
    assert full["status"] == "full_store"
    assert full["snapshot"] == snapshot
    duplicate = await save(memory, "  Fact 1  ", revision=50)
    assert duplicate["status"] == "unchanged"
    assert duplicate["snapshot"] == snapshot
    identifier = snapshot["facts"][0]["id"]
    collision = await save(memory, "Fact 1", revision=50, id=identifier)
    assert collision["status"] == collision["error"] == "invalid_input"
    assert collision["duplicate_id"] == snapshot["facts"][1]["id"]
    assert collision["snapshot"] == snapshot
    assert await memory.async_snapshot() == snapshot
    same_fact = await save(memory, "Fact 0", revision=50, id=identifier)
    assert same_fact == {"status": "unchanged", "snapshot": snapshot}
    replaced = await save(memory, "Brighter reading lights", revision=50, id=identifier)
    assert replaced["status"] == "saved"
    assert replaced["snapshot"]["revision"] == 51
    assert replaced["snapshot"]["count"] == 50
    assert replaced["snapshot"]["facts"][0] == {
        "id": identifier,
        "text": "Brighter reading lights",
    }
    assert replaced["snapshot"]["facts"][1:] == snapshot["facts"][1:]


async def test_concurrent_stale_edits_and_all_or_nothing_forget(memory):
    results = await asyncio.gather(save(memory, "Fact one"), save(memory, "Fact two"))
    assert sorted(r["status"] for r in results) == ["saved", "stale_revision"]
    snapshot = next(r["snapshot"] for r in results if r["status"] == "saved")
    assert all(r["snapshot"] == snapshot for r in results)
    identifier = snapshot["facts"][0]["id"]
    for ids in ([], [identifier, "missing"], [identifier, identifier], [None], "all"):
        result = await memory.async_tool(
            MEMORY_FORGET_TOOL, {"expected_revision": 1, "ids": ids}, "Forget it"
        )
        assert result["status"] == "invalid_input"
        assert await memory.async_snapshot() == snapshot
    deleted = await memory.async_tool(
        MEMORY_FORGET_TOOL, {"expected_revision": 1, "ids": [identifier]}, "Forget it"
    )
    assert deleted["status"] == "deleted"
    assert deleted["snapshot"]["facts"] == []
    assert deleted["snapshot"]["revision"] == 2
    listed = await memory.async_tool(MEMORY_LIST_TOOL, {}, "What do you remember?")
    assert listed == {"status": "listed", "snapshot": deleted["snapshot"]}
    assert (await memory.async_tool(MEMORY_LIST_TOOL, {"id": identifier}, ""))[
        "status"
    ] == "invalid_input"


@pytest.mark.parametrize(
    "fault", ["malformed", "version", "schema", "too_large", "utf8"]
)
async def test_corrupt_storage_is_preserved_and_reported(memory, fault, caplog):
    await save(memory)
    path = Path(memory.store.path)
    envelope = json.loads(path.read_text())
    if fault == "version":
        envelope["version"] = 2
        raw = json.dumps(envelope).encode()
    elif fault == "schema":
        envelope["data"]["facts"][0]["source"] = None
        raw = json.dumps(envelope).encode()
    else:
        raw = {
            "malformed": b'{"SECRET_STORAGE_CONTENT":',
            "too_large": b" " * 512_001,
            "utf8": b"\xff",
        }[fault]
    path.write_bytes(raw)
    with pytest.raises(MemoryStorageError, match="not been reset"):
        await memory.async_snapshot()
    result = await save(memory, "Another fact", revision=1)
    assert result["status"] == "persistence_failure"
    assert path.read_bytes() == raw
    assert not list(path.parent.glob("*.corrupt.*"))
    assert "SECRET_STORAGE_CONTENT" not in caplog.text
    await memory.async_remove()
    assert not path.exists()


async def test_real_store_swallowed_write_error_is_not_success(
    memory, monkeypatch, caplog
):
    original = (await save(memory))["snapshot"]

    def fail(*args, **kwargs):
        raise WriteError("Permission denied")

    monkeypatch.setattr(ha_storage, "write_utf8_file_atomic", fail)
    result = await save(memory, "SECRET_NEW_FACT", revision=1)
    assert "Error writing config" in caplog.text  # HA swallowed the actual WriteError.
    assert result["status"] == "persistence_failure"
    assert await memory.async_snapshot() == original
    assert "SECRET_NEW_FACT" not in caplog.text
    assert "Warm reading lights" not in caplog.text


async def test_unreadable_storage_and_unverified_commit_fail_closed(
    memory, monkeypatch
):
    await save(memory)
    original_read = memory._read

    def unreadable(path):
        raise PermissionError("SECRET_RAW_ERROR")

    monkeypatch.setattr(memory, "_read", unreadable)
    result = await save(memory, "Another fact", revision=1)
    assert result["status"] == "persistence_failure"
    assert "SECRET" not in json.dumps(result)
    monkeypatch.setattr(memory, "_read", original_read)
    original_save = memory.store.async_save

    async def save_then_lose_read(data):
        await original_save(data)
        monkeypatch.setattr(memory, "_read", unreadable)

    monkeypatch.setattr(memory.store, "async_save", save_then_lose_read)
    result = await save(memory, "Another fact", revision=1)
    assert result["status"] == "persistence_failure"
    monkeypatch.setattr(memory, "_read", original_read)
    assert (await memory.async_snapshot())["count"] == 2


@pytest.mark.parametrize("stage", ["write", "verify"])
async def test_cancelled_write_drains_before_unlock_and_shutdown(
    memory, monkeypatch, stage
):
    await save(memory)
    entered, finish = threading.Event(), threading.Event()
    target, method = (
        (ha_storage, "write_utf8_file_atomic")
        if stage == "write"
        else (memory, "_read")
    )
    original = getattr(target, method)

    def blocked(*args, **kwargs):
        # Only block the verification read, not the pre-mutation read.
        if (
            stage == "write"
            or json.loads(Path(args[0]).read_text())["data"]["revision"] == 2
        ):
            entered.set()
            assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, blocked)
    task = asyncio.create_task(save(memory, "Second fact", revision=1))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        closing = asyncio.create_task(memory.async_close())
        await asyncio.sleep(0)
        assert not task.done()
        assert not closing.done()
        assert memory._lock.locked()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        assert not memory._lock.locked()
        assert (await memory.async_snapshot())["count"] == 2
        assert (await save(memory, "Third fact", revision=2))["status"] == "unavailable"
    finally:
        finish.set()
        await asyncio.gather(
            task, *([closing] if closing else []), return_exceptions=True
        )


async def test_shutdown_never_schedules_a_deferred_acknowledged_write(memory):
    memory.hass.set_state(CoreState.stopping)
    try:
        assert (await save(memory))["status"] == "unavailable"
        assert not Path(memory.store.path).exists()
        assert memory.store._data is None
        assert memory.store._unsub_final_write_listener is None
    finally:
        memory.hass.set_state(CoreState.not_running)


async def test_queued_mutation_rechecks_conversation_lifecycle(memory):
    active = True
    await memory._lock.acquire()
    task = asyncio.create_task(
        memory.async_tool(
            MEMORY_SAVE_TOOL,
            {"text": "Fact", "source": "Fact", "expected_revision": 0},
            "Fact",
            is_active=lambda: active,
        )
    )
    await asyncio.sleep(0)
    active = False
    memory._lock.release()
    assert (await task)["status"] == "unavailable"
    assert not Path(memory.store.path).exists()


async def test_entry_removal_deletes_memory_when_disabled_and_unloaded(
    memory, monkeypatch
):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.github_copilot import async_remove_entry

    await save(memory)
    other = MemoryManager(memory.hass, "other")
    await save(other, "Other entry fact")
    monkeypatch.setattr(ir, "async_delete_issue", Mock())
    await memory.async_close()
    await async_remove_entry(memory.hass, SimpleNamespace(entry_id="entry", options={}))
    assert not Path(memory.store.path).exists()
    assert (await other.async_snapshot())["count"] == 1
    await other.async_close()


async def test_memory_unlink_failure_still_cleans_other_entry_state(
    memory, monkeypatch, caplog
):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.github_copilot import async_remove_entry
    from custom_components.github_copilot.const import DATA_CREDENTIAL_LOCKS, DOMAIN

    await save(memory)
    original = Path(memory.store.path).read_bytes()
    session_state = Path(memory.hass.config.path(".storage", DOMAIN, "entry"))
    session_state.mkdir(parents=True)
    (session_state / "state").write_text("Disposable SDK state")
    other_lock = object()
    memory.hass.data[DATA_CREDENTIAL_LOCKS] = {"entry": object(), "other": other_lock}
    delete_issue = Mock()
    monkeypatch.setattr(ir, "async_delete_issue", delete_issue)
    create_issue = Mock()
    monkeypatch.setattr(ir, "async_create_issue", create_issue)
    original_remove = Store.async_remove

    async def fail_memory_remove(store):
        if store.key == memory.key:
            raise PermissionError("PRIVATE_FILESYSTEM_DETAIL")
        await original_remove(store)

    monkeypatch.setattr(Store, "async_remove", fail_memory_remove)
    await async_remove_entry(memory.hass, SimpleNamespace(entry_id="entry", options={}))
    assert Path(memory.store.path).read_bytes() == original
    assert not session_state.exists()
    assert memory.hass.data[DATA_CREDENTIAL_LOCKS] == {"other": other_lock}
    assert delete_issue.call_count == 5
    create_issue.assert_called_once()
    assert create_issue.call_args.kwargs["translation_key"] == "memory_cleanup_failed"
    assert create_issue.call_args.kwargs["translation_placeholders"] == {
        "filename": memory.key
    }
    assert "continuing other entry cleanup" in caplog.text
    assert "PRIVATE_FILESYSTEM_DETAIL" not in caplog.text
    assert "Warm reading lights" not in caplog.text
