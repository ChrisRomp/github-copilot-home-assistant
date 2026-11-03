"""Startup cleanup never guesses ownership or deletes through filesystem links."""

import asyncio
import logging
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from copilot._jsonrpc import JsonRpcError

from custom_components.github_copilot import session_cleanup as cleanup


@pytest.fixture
def storage(tmp_path):
    base = tmp_path / ".storage" / "github_copilot" / "entry"
    base.mkdir(parents=True)

    def session(number):
        path = base / "session-state" / str(UUID(int=number))
        path.mkdir(parents=True)
        return path

    return SimpleNamespace(base=base, session=session)


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
        await cleanup.async_cleanup_startup_sessions(client, storage.base)
        await cleanup.async_cleanup_startup_sessions(client, storage.base)
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
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
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
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
    client.delete_session.assert_not_awaited()
    assert (target / "preserve").read_text() == "keep"
    assert "skipped=1" in caplog.text


async def test_candidate_replaced_with_symlink_is_rechecked(
    storage, tmp_path, monkeypatch
):
    path = storage.session(1)
    target = tmp_path / "outside"
    target.mkdir()

    def inventory(base):
        path.rmdir()
        path.symlink_to(target, target_is_directory=True)
        return [path.name], 0, False

    monkeypatch.setattr(cleanup, "_find_sessions", inventory)
    client = SimpleNamespace(delete_session=AsyncMock())
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
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
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
    assert client.delete_session.await_count == 2
    assert first.is_dir() and second.is_dir()
    assert "failed=2" in caplog.text
    assert "PRIVATE_" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_cleanup_has_a_total_deadline(storage, monkeypatch, caplog):
    storage.session(1)
    storage.session(2)
    monkeypatch.setattr(cleanup, "CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def stuck(session_id):
        await asyncio.Event().wait()

    client = SimpleNamespace(delete_session=AsyncMock(side_effect=stuck))
    await asyncio.wait_for(
        cleanup.async_cleanup_startup_sessions(client, storage.base), timeout=1
    )
    client.delete_session.assert_awaited_once()
    assert "limit_reached=True" in caplog.text


async def test_cleanup_cancellation_propagates(storage):
    path = storage.session(1)
    client = SimpleNamespace(
        delete_session=AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await cleanup.async_cleanup_startup_sessions(client, storage.base)
    client.delete_session.assert_awaited_once()
    assert path.is_dir()


async def test_cleanup_has_a_deletion_count_limit(storage, monkeypatch, caplog):
    for number in range(1, 6):
        storage.session(number)
    monkeypatch.setattr(cleanup, "DELETE_LIMIT", 2)
    client = SimpleNamespace(delete_session=AsyncMock())
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
    assert client.delete_session.await_count == 2
    assert "limit_reached=True" in caplog.text


def test_inventory_does_not_scan_unbounded_directory_entries(storage, monkeypatch):
    storage.session(1)
    monkeypatch.setattr(cleanup, "SCAN_LIMIT", 3)
    entry = SimpleNamespace(name="not-a-session")
    seen = []

    def entries():
        for number in range(100):
            seen.append(number)
            yield entry

    class Scan:
        def __enter__(self):
            return entries()

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cleanup.os, "scandir", Mock(return_value=Scan()))
    assert cleanup._find_sessions(storage.base) == ([], 0, True)
    assert len(seen) == 3


async def test_unverified_native_runtime_skips_cleanup(storage, monkeypatch, caplog):
    from copilot import _cli_version

    storage.session(1)
    monkeypatch.setattr(_cli_version, "CLI_VERSION", "unverified")
    client = SimpleNamespace(delete_session=AsyncMock())
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
    client.delete_session.assert_not_awaited()
    assert "has not been verified" in caplog.text


async def test_empty_storage_needs_no_sdk_deletion(storage):
    client = SimpleNamespace(delete_session=AsyncMock())
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
    client.delete_session.assert_not_awaited()


async def test_invalid_names_and_regular_files_are_never_sent_to_sdk(storage):
    root = storage.base / "session-state"
    root.mkdir()
    for name in ("not-a-session", "runtime", UUID(int=1).hex):
        (root / name).mkdir()
    (root / str(UUID(int=2))).write_text("not a directory")
    client = SimpleNamespace(delete_session=AsyncMock())
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
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
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
    assert len(attempts) == 2
    assert sum(path.exists() for path in (first, second)) == 1
    assert "removed=1 failed=1" in caplog.text
    await cleanup.async_cleanup_startup_sessions(client, storage.base)
    assert not first.exists() and not second.exists()
    assert len(attempts) == 3
    assert "PRIVATE_" not in caplog.text
