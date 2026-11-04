"""Bounded maintenance never guesses ownership or follows filesystem links."""

import asyncio
import logging
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from copilot._jsonrpc import JsonRpcError

from custom_components.github_copilot import session_cleanup as cleanup


@pytest.fixture
async def storage(tmp_path):
    base = tmp_path / ".storage" / "github_copilot" / "entry"
    base.mkdir(parents=True)

    def session(number):
        path = base / "session-state" / str(UUID(int=number))
        path.mkdir(parents=True)
        return path

    cleaners = {}

    def cleaner(client):
        if id(client) not in cleaners:
            cleaners[id(client)] = cleanup.SessionCleanup(client, base, asyncio.Lock())
        return cleaners[id(client)]

    async def run(client, protected=lambda session_id: False, phase="startup"):
        await cleaner(client).async_run(phase, protected)

    yield SimpleNamespace(base=base, session=session, run=run, cleaner=cleaner)
    for owned in cleaners.values():
        await owned.async_close()


async def test_cleanup_deletes_incomplete_sessions_without_reading_their_contents(
    storage, caplog
):
    complete = storage.session(1)
    (complete / "events.jsonl").write_text("PRIVATE_SESSION_CONTENT")
    incomplete = storage.session(2)
    (incomplete / "workspace.yaml").write_text("not: [valid yaml")
    other = storage.base.parent / "other-entry" / "session-state" / complete.name
    other.mkdir(parents=True)
    credentials = storage.base / "credentials"
    credentials.write_text("PRIVATE_CREDENTIAL_PLACEHOLDER")
    unrecognized = storage.base / "session-state" / "unexpected-directory"
    unrecognized.mkdir()
    lock_directory = storage.base / "session-state" / ".session-operation-locks"
    lock_directory.mkdir()

    async def delete(session_id):
        shutil.rmtree(storage.base / "session-state" / session_id)

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=delete))
    with caplog.at_level(logging.INFO):
        await storage.run(client)
        await storage.run(client)
    assert client.delete_session.await_count == 2
    assert not complete.exists() and not incomplete.exists()
    assert other.is_dir() and unrecognized.is_dir() and lock_directory.is_dir()
    assert credentials.read_text() == "PRIVATE_CREDENTIAL_PLACEHOLDER"
    assert "removed=2" in caplog.text
    assert "PRIVATE_" not in caplog.text
    assert complete.name not in caplog.text


@pytest.mark.parametrize("linked_part", ["storage", "root", "entry", "sessions"])
async def test_cleanup_refuses_symlinked_storage_roots(
    storage, tmp_path, caplog, linked_part
):
    session = storage.session(1)
    path = {
        "storage": storage.base.parent.parent,
        "root": storage.base.parent,
        "entry": storage.base,
        "sessions": storage.base / "session-state",
    }[linked_part]
    target = tmp_path / "outside"
    path.rename(target)
    path.symlink_to(target, target_is_directory=True)
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    client.delete_session.assert_not_awaited()
    assert session.is_dir()
    assert "inventory could not be checked" in caplog.text


async def test_cleanup_skips_symlinked_session_directories(storage, tmp_path, caplog):
    target = tmp_path / "outside"
    target.mkdir()
    (target / "preserve").write_text("keep")
    root = storage.base / "session-state"
    root.mkdir()
    (root / str(UUID(int=1))).symlink_to(target, target_is_directory=True)
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    client.delete_session.assert_not_awaited()
    assert (target / "preserve").read_text() == "keep"
    assert "skipped=1" in caplog.text


async def test_candidate_replaced_with_symlink_is_rechecked(
    storage, tmp_path, monkeypatch
):
    path = storage.session(1)
    target = tmp_path / "outside"
    target.mkdir()

    def inventory(self):
        path.rmdir()
        path.symlink_to(target, target_is_directory=True)
        return [path.name], 0, False

    monkeypatch.setattr(cleanup._SessionInventory, "scan", inventory)
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    client.delete_session.assert_not_awaited()
    assert target.is_dir()


@pytest.mark.parametrize(
    "error", [RuntimeError("PRIVATE_ACTIVE_OWNER"), JsonRpcError(-32603, "PRIVATE_RPC")]
)
async def test_failed_or_protected_deletion_is_not_retried_or_logged_raw(
    storage, caplog, error
):
    first, second = storage.session(1), storage.session(2)
    client = SimpleNamespace(delete_session=AsyncMock(side_effect=error))
    await storage.run(client)
    assert client.delete_session.await_count == 2
    assert first.is_dir() and second.is_dir()
    assert "failed=2" in caplog.text
    assert "PRIVATE_" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_cleanup_has_a_total_deadline(storage, monkeypatch, caplog):
    storage.session(1)
    storage.session(2)
    assert cleanup.CLEANUP_TIMEOUT_SECONDS == 10
    monkeypatch.setattr(cleanup, "CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def stuck(session_id):
        await asyncio.Event().wait()

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=stuck))
    await asyncio.wait_for(storage.run(client), timeout=1)
    client.delete_session.assert_awaited_once()
    assert "limit_reached=True" in caplog.text


async def test_cleanup_cancellation_propagates(storage):
    path = storage.session(1)
    client = SimpleNamespace(
        delete_session=AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await storage.run(client)
    client.delete_session.assert_awaited_once()
    assert path.is_dir()


@pytest.mark.parametrize("limit", [2, 25])
async def test_cleanup_has_a_deletion_count_limit(storage, monkeypatch, caplog, limit):
    assert cleanup.DELETE_LIMIT == 25
    for number in range(1, limit + 4):
        storage.session(number)
    monkeypatch.setattr(cleanup, "DELETE_LIMIT", limit)
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    assert client.delete_session.await_count == limit
    assert "limit_reached=True" in caplog.text


@pytest.mark.parametrize("limit", [3, 200])
async def test_inventory_does_not_scan_unbounded_directory_entries(
    storage, monkeypatch, limit
):
    assert cleanup.SCAN_LIMIT == 200
    storage.session(1)
    monkeypatch.setattr(cleanup, "SCAN_LIMIT", limit)
    entry = SimpleNamespace(name="not-a-session")
    seen = []

    def entries():
        for number in range(limit + 100):
            seen.append(number)
            yield entry

    class Scan:
        def __iter__(self):
            return self

        def __next__(self):
            return next(iterator)

        def close(self):
            pass

    iterator = entries()
    monkeypatch.setattr(cleanup.os, "scandir", Mock(return_value=Scan()))
    inventory = cleanup._SessionInventory(storage.base)
    assert inventory.scan() == ([], 0, True)
    inventory.close()
    assert len(seen) == limit


async def test_unverified_native_runtime_skips_cleanup(storage, monkeypatch, caplog):
    from copilot import _cli_version

    storage.session(1)
    monkeypatch.setattr(_cli_version, "CLI_VERSION", "unverified")
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    client.delete_session.assert_not_awaited()
    assert "has not been verified" in caplog.text


async def test_empty_storage_needs_no_sdk_deletion(storage):
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    client.delete_session.assert_not_awaited()


async def test_invalid_names_and_regular_files_are_never_sent_to_sdk(storage):
    root = storage.base / "session-state"
    root.mkdir()
    for name in ("not-a-session", "runtime", UUID(int=1).hex):
        (root / name).mkdir()
    (root / str(UUID(int=2))).write_text("not a directory")
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    client.delete_session.assert_not_awaited()


async def test_failed_delete_does_not_prevent_later_recovery(storage, caplog):
    first, second = storage.session(1), storage.session(2)
    attempts = []

    async def delete(session_id):
        attempts.append(session_id)
        if len(attempts) == 1:
            raise OSError("PRIVATE_FAILURE")
        shutil.rmtree(storage.base / "session-state" / session_id)

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=delete))
    await storage.run(client)
    assert len(attempts) == 2
    assert sum(path.exists() for path in (first, second)) == 1
    assert "removed=1 failed=1" in caplog.text
    await storage.run(client)
    assert not first.exists() and not second.exists()
    assert len(attempts) == 3
    assert "PRIVATE_" not in caplog.text


async def test_periodic_cleanup_protects_local_sessions_without_authentication(
    storage, caplog
):
    protected = {storage.session(1).name, storage.session(2).name}
    abandoned = storage.session(3)

    async def delete(session_id):
        shutil.rmtree(storage.base / "session-state" / session_id)

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=delete))
    with caplog.at_level(logging.INFO):
        await storage.run(client, protected.__contains__, phase="periodic")
    client.delete_session.assert_awaited_once_with(abandoned.name)
    assert all((storage.base / "session-state" / name).is_dir() for name in protected)
    assert "protected=2" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_protection_is_rechecked_after_filesystem_await(storage, monkeypatch):
    path = storage.session(1)
    protected = set()
    loop = asyncio.get_running_loop()

    def check(base, session_id):
        loop.call_soon_threadsafe(protected.add, session_id)
        return True

    monkeypatch.setattr(cleanup, "_is_session_directory", check)
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client, protected.__contains__, phase="periodic")
    client.delete_session.assert_not_awaited()
    assert path.is_dir()


async def test_repeated_passes_progress_past_invalid_protected_and_failed_prefix(
    storage, monkeypatch, caplog
):
    paths = [storage.session(number) for number in range(1, 8)]
    names = ["unknown"] * 4 + [path.name for path in paths]
    seen = []

    class Scan:
        def __init__(self):
            self.names = iter(names)

        def __iter__(self):
            return self

        def __next__(self):
            name = next(self.names)
            seen.append(name)
            return SimpleNamespace(
                name=name,
                is_symlink=lambda: False,
                is_dir=lambda **kwargs: True,
            )

        def close(self):
            pass

    monkeypatch.setattr(cleanup.os, "scandir", Mock(side_effect=lambda root: Scan()))
    monkeypatch.setattr(cleanup, "SCAN_LIMIT", 3)
    monkeypatch.setattr(cleanup, "DELETE_LIMIT", 2)
    protected = {paths[0].name, paths[1].name}
    failed = {paths[2].name, paths[3].name}

    async def delete(session_id):
        if session_id in failed:
            raise RuntimeError("PRIVATE_DELETE_FAILURE")

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=delete))
    for _ in range(8):
        before_scan = len(seen)
        before_delete = client.delete_session.await_count
        await storage.run(client, protected.__contains__, phase="periodic")
        assert len(seen) - before_scan <= 3
        assert client.delete_session.await_count - before_delete <= 2
    attempted = {call.args[0] for call in client.delete_session.await_args_list}
    assert attempted == {path.name for path in paths} - protected
    assert "PRIVATE_" not in caplog.text
    assert not any(path.name in caplog.text for path in paths)


async def test_replaced_inventory_root_resets_scan_position(storage, monkeypatch):
    storage.session(1)
    storage.session(2)
    monkeypatch.setattr(cleanup, "DELETE_LIMIT", 1)
    client = SimpleNamespace(delete_session=AsyncMock())
    await storage.run(client)
    root = storage.base / "session-state"
    root.rename(storage.base / "old-sessions")
    new = storage.session(3)
    await storage.run(client, phase="periodic")
    assert client.delete_session.await_args.args == (new.name,)


async def test_timed_out_delete_remains_owned_and_serializes_entries(
    storage, monkeypatch, caplog
):
    storage.session(1)
    storage.session(2)
    monkeypatch.setattr(cleanup, "CLEANUP_TIMEOUT_SECONDS", 0.02)
    finished = asyncio.Event()

    async def delete(session_id):
        await finished.wait()

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=delete))
    first = storage.cleaner(client)
    other_base = storage.base.parent / "other-entry"
    (other_base / "session-state" / str(UUID(int=3))).mkdir(parents=True)
    other_client = SimpleNamespace(delete_session=AsyncMock())
    second = cleanup.SessionCleanup(other_client, other_base, first._lock)
    try:
        await storage.run(client)
        assert not first._task.done()
        assert first._lock.locked()
        await storage.run(client, phase="periodic")
        client.delete_session.assert_awaited_once()
        assert "previous operation is still pending" in caplog.text
        await second.async_run("periodic", lambda session_id: False)
        other_client.delete_session.assert_not_awaited()
        finished.set()
        await first._task
        client.delete_session.assert_awaited_once()
        assert not first._lock.locked()
        await second.async_run("periodic", lambda session_id: False)
        other_client.delete_session.assert_awaited_once_with(str(UUID(int=3)))
    finally:
        finished.set()
        await second.async_close()


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_interrupted_inventory_is_drained_before_cursor_close(
    storage, monkeypatch, interrupt
):
    storage.session(1)
    client = SimpleNamespace(delete_session=AsyncMock())
    owned = storage.cleaner(client)
    entered = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    original = owned._inventory.scan
    closed = Mock(wraps=owned._inventory.close)
    monkeypatch.setattr(owned._inventory, "close", closed)

    def scan():
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(3)
        return original()

    monkeypatch.setattr(owned._inventory, "scan", scan)
    if interrupt == "timeout":
        monkeypatch.setattr(cleanup, "CLEANUP_TIMEOUT_SECONDS", 0.02)
    caller = asyncio.create_task(storage.run(client))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if interrupt == "cancel":
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        else:
            await asyncio.wait_for(caller, 1)
        await storage.run(client, phase="periodic")
        closing = asyncio.create_task(owned.async_close())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done()
        closed.assert_not_called()
        assert owned._lock.locked()
        finish.set()
        await asyncio.wait_for(closing, 1)
        assert owned._inventory._entries is None
        assert not owned._lock.locked()
        client.delete_session.assert_not_awaited()
    finally:
        finish.set()
        await asyncio.gather(caller, return_exceptions=True)
        if closing is not None:
            await closing


async def test_cancelled_lock_wait_never_deletes_and_does_not_release_foreign_lock(
    storage,
):
    storage.session(1)
    client = SimpleNamespace(delete_session=AsyncMock())
    owned = storage.cleaner(client)
    await owned._lock.acquire()
    caller = asyncio.create_task(storage.run(client))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await owned.async_close()
    assert owned._lock.locked()
    client.delete_session.assert_not_awaited()
    owned._lock.release()


async def test_cancelled_deletion_finishes_without_starting_another(storage):
    storage.session(1)
    storage.session(2)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delete(session_id):
        entered.set()
        await finish.wait()

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=delete))
    caller = asyncio.create_task(storage.run(client))
    await asyncio.wait_for(entered.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    owned = storage.cleaner(client)
    assert not owned._task.done()
    finish.set()
    await owned._task
    client.delete_session.assert_awaited_once()
    await owned.async_close()
    await storage.run(client, phase="periodic")
    client.delete_session.assert_awaited_once()


async def test_deadline_keeps_unattempted_candidates_for_the_next_pass(
    storage, monkeypatch
):
    paths = [storage.session(number) for number in range(1, 4)]
    monkeypatch.setattr(cleanup, "CLEANUP_TIMEOUT_SECONDS", 0.02)
    finish = asyncio.Event()
    slow = None

    async def delete(session_id):
        nonlocal slow
        if slow is None:
            slow = session_id
        if session_id == slow:
            await finish.wait()
            raise RuntimeError("Slow deletion failed")

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=delete))
    owned = storage.cleaner(client)
    await storage.run(client)
    client.delete_session.assert_awaited_once()
    assert len(owned._pending) == 2
    finish.set()
    await owned._task
    await storage.run(client, phase="periodic")
    assert client.delete_session.await_count == 3
    assert {call.args[0] for call in client.delete_session.await_args_list} == {
        path.name for path in paths
    }
    assert not owned._pending


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_interrupted_directory_check_requeues_candidate_after_pending_work(
    storage, monkeypatch, interrupt
):
    paths = [storage.session(number) for number in range(1, 4)]
    client = SimpleNamespace(delete_session=AsyncMock())
    owned = storage.cleaner(client)
    entered = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    original = cleanup._is_session_directory
    checked = []

    def check(base, session_id):
        checked.append(session_id)
        if len(checked) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert finish.wait(3)
        return original(base, session_id)

    monkeypatch.setattr(cleanup, "_is_session_directory", check)
    if interrupt == "timeout":
        monkeypatch.setattr(cleanup, "CLEANUP_TIMEOUT_SECONDS", 0.02)
    caller = asyncio.create_task(storage.run(client))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        remaining = list(owned._pending)
        assert len(remaining) == 2
        if interrupt == "cancel":
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        else:
            await asyncio.wait_for(caller, 1)
        finish.set()
        await asyncio.wait_for(owned._task, 1)
        assert list(owned._pending) == [*remaining, checked[0]]
        client.delete_session.assert_not_awaited()

        await storage.run(client, phase="periodic")
        attempted = [call.args[0] for call in client.delete_session.await_args_list]
        assert attempted == [*remaining, checked[0]]
        assert set(attempted) == {path.name for path in paths}
        assert len(checked) == 4
        assert not owned._pending
    finally:
        finish.set()
        await asyncio.gather(caller, return_exceptions=True)
