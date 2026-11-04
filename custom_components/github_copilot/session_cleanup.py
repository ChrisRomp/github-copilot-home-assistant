"""Bounded recovery of abandoned native sessions before admitting new requests."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from copilot._jsonrpc import JsonRpcError

if TYPE_CHECKING:
    from copilot import CopilotClient

_LOGGER = logging.getLogger(__name__)
VERIFIED_RUNTIME = "1.0.103"
SCAN_LIMIT = 200
DELETE_LIMIT = 25
CLEANUP_TIMEOUT_SECONDS = 10


def _session_root(base_directory: Path) -> Path:
    root = base_directory / "session-state"
    for path in (
        base_directory.parent.parent,
        base_directory.parent,
        base_directory,
        root,
    ):
        if path.is_symlink():
            raise OSError("Session storage contains a symlink")
    return root


def _find_sessions(base_directory: Path) -> tuple[list[str], int, bool]:
    root = _session_root(base_directory)
    if not root.exists():
        return [], 0, False
    sessions = []
    skipped = examined = 0
    limited = False
    with os.scandir(root) as entries:
        for entry in islice(entries, SCAN_LIMIT):
            examined += 1
            try:
                if str(UUID(entry.name)) != entry.name:
                    continue
            except ValueError:
                continue
            if entry.is_symlink():
                skipped += 1
            elif entry.is_dir(follow_symlinks=False):
                sessions.append(entry.name)
                if len(sessions) == DELETE_LIMIT:
                    limited = True
                    break
    return sessions, skipped, limited or examined == SCAN_LIMIT


def _is_session_directory(base_directory: Path, session_id: str) -> bool:
    path = _session_root(base_directory) / session_id
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


async def async_cleanup_startup_sessions(
    client: CopilotClient, base_directory: Path
) -> None:
    """Use native ownership locks to delete only inactive, entry-owned sessions."""
    from copilot._cli_version import CLI_VERSION

    if CLI_VERSION != VERIFIED_RUNTIME:
        _LOGGER.warning(
            "Copilot startup session cleanup skipped: native ownership protection "
            "has not been verified for this runtime"
        )
        return

    removed = failed = skipped = 0
    limited = False
    try:
        async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
            sessions, skipped, limited = await asyncio.to_thread(
                _find_sessions, base_directory
            )
            for session_id in sessions:
                if not await asyncio.to_thread(
                    _is_session_directory, base_directory, session_id
                ):
                    skipped += 1
                    continue
                try:
                    # The pinned SDK rejects deletion while another process owns it.
                    await client.delete_session(session_id)
                except (OSError, RuntimeError, JsonRpcError):
                    failed += 1
                else:
                    removed += 1
    except TimeoutError:
        limited = True
    except (OSError, RuntimeError) as err:
        failed += 1
        _LOGGER.warning(
            "Copilot startup session inventory could not be checked (%s)",
            type(err).__name__,
        )

    if failed or skipped or limited:
        _LOGGER.warning(
            "Copilot startup session cleanup: removed=%d failed=%d skipped=%d "
            "limit_reached=%s; some session data may remain, including sessions "
            "owned by another runtime. Cleanup will be attempted at the next startup",
            removed,
            failed,
            skipped,
            limited,
        )
    elif removed:
        _LOGGER.info("Copilot startup session cleanup: removed=%d", removed)
