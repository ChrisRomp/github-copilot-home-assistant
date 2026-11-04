"""Bounded startup and periodic recovery of abandoned native sessions."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from copilot._jsonrpc import JsonRpcError

from .cleanup import async_wait_cleanup

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


class _SessionInventory:
    """Keep one bounded traversal, without repeatedly revisiting its prefix."""

    def __init__(self, base_directory: Path) -> None:
        self.base_directory = base_directory
        self._entries: os.ScandirIterator[str] | None = None
        self._identity: tuple[int, int] | None = None

    def close(self) -> None:
        if self._entries is not None:
            self._entries.close()
            self._entries = None
        self._identity = None

    def scan(self) -> tuple[list[str], int, bool]:
        try:
            root = _session_root(self.base_directory)
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("Session storage is not a directory")
            identity = (info.st_dev, info.st_ino)
            if identity != self._identity:
                self.close()
                self._entries = os.scandir(root)
                self._identity = identity
            assert self._entries is not None
            sessions = []
            skipped = examined = 0
            for entry in islice(self._entries, SCAN_LIMIT):
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
                        return sessions, skipped, True
            if examined == SCAN_LIMIT:
                return sessions, skipped, True
            self.close()
            return sessions, skipped, False
        except FileNotFoundError:
            self.close()
            return [], 0, False
        except OSError:
            self.close()
            raise


def _is_session_directory(base_directory: Path, session_id: str) -> bool:
    path = _session_root(base_directory) / session_id
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


@dataclass
class _Outcome:
    removed: int = 0
    failed: int = 0
    skipped: int = 0
    protected: int = 0
    limited: bool = False
    cancelled: bool = False


class SessionCleanup:
    """Own scan progress and outstanding maintenance until the runtime stops."""

    def __init__(
        self,
        client: CopilotClient,
        base_directory: Path,
        maintenance_lock: asyncio.Lock,
    ) -> None:
        self.client = client
        self.base_directory = base_directory
        self._inventory = _SessionInventory(base_directory)
        self._pending: deque[str] = deque()
        self._lock = maintenance_lock
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    def stop(self) -> None:
        """Prevent more operations without abandoning an in-flight SDK request."""
        self._closed = True

    async def async_close(self) -> None:
        """Release resources after native termination, draining filesystem work."""
        self.stop()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._async_close())
        await async_wait_cleanup(self._close_task)

    async def _async_close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await asyncio.to_thread(self._inventory.close)
        self._pending.clear()

    async def async_run(
        self,
        phase: Literal["startup", "periodic"],
        is_protected: Callable[[str], bool],
    ) -> None:
        """Bound caller latency while retaining ownership of unfinished work."""
        from copilot._cli_version import CLI_VERSION

        if self._closed:
            return
        if CLI_VERSION != VERIFIED_RUNTIME:
            _LOGGER.warning(
                "Copilot %s session cleanup skipped: native ownership protection "
                "has not been verified for this runtime",
                phase,
            )
            return
        if self._task is not None and not self._task.done():
            _LOGGER.warning(
                "Copilot %s session cleanup deferred: previous operation "
                "is still pending",
                phase,
            )
            return

        outcome = _Outcome()
        deadline = asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS
        task = self._task = asyncio.create_task(
            self._async_pass(deadline, outcome, is_protected)
        )
        task.add_done_callback(lambda done: self._report(done, phase, outcome))
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(task)
        except TimeoutError:
            outcome.limited = True
            _LOGGER.warning(
                "Copilot %s session cleanup: removed=%d failed=%d skipped=%d "
                "protected=%d limit_reached=True; unfinished work remains owned "
                "and no further deletions will be issued by this pass",
                phase,
                outcome.removed,
                outcome.failed,
                outcome.skipped,
                outcome.protected,
            )
        except asyncio.CancelledError:
            outcome.cancelled = True
            raise

    def _finished(self, deadline: float, outcome: _Outcome) -> bool:
        if self._closed or outcome.cancelled:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            outcome.limited = True
            return True
        return False

    async def _async_pass(
        self,
        deadline: float,
        outcome: _Outcome,
        is_protected: Callable[[str], bool],
    ) -> None:
        try:
            async with asyncio.timeout_at(deadline):
                await self._lock.acquire()
        except TimeoutError:
            outcome.limited = True
            return
        try:
            if self._finished(deadline, outcome):
                return
            if not self._pending:
                sessions, outcome.skipped, outcome.limited = await async_wait_cleanup(
                    asyncio.create_task(asyncio.to_thread(self._inventory.scan))
                )
                self._pending.extend(sessions)
            while self._pending:
                if self._finished(deadline, outcome):
                    break
                session_id = self._pending.popleft()
                if is_protected(session_id):
                    outcome.protected += 1
                    continue
                is_directory = await async_wait_cleanup(
                    asyncio.create_task(
                        asyncio.to_thread(
                            _is_session_directory, self.base_directory, session_id
                        )
                    )
                )
                if self._finished(deadline, outcome):
                    break
                if not is_directory:
                    outcome.skipped += 1
                    continue
                # Protection can change while the filesystem check is in flight.
                if is_protected(session_id):
                    outcome.protected += 1
                    continue
                try:
                    # The pinned native runtime protects other live processes.
                    await self.client.delete_session(session_id)
                except (OSError, RuntimeError, JsonRpcError):
                    outcome.failed += 1
                else:
                    outcome.removed += 1
        except (OSError, RuntimeError) as err:
            outcome.failed += 1
            _LOGGER.warning(
                "Copilot session inventory could not be checked (%s)",
                type(err).__name__,
            )
        finally:
            self._lock.release()

    @staticmethod
    def _report(
        task: asyncio.Task[None],
        phase: str,
        outcome: _Outcome,
    ) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            outcome.failed += 1
            _LOGGER.error(
                "Copilot %s session cleanup failed unexpectedly (%s)",
                phase,
                type(error).__name__,
            )
        log = (
            _LOGGER.warning
            if outcome.failed or outcome.skipped or outcome.limited
            else _LOGGER.info
        )
        if any(
            (
                outcome.removed,
                outcome.failed,
                outcome.skipped,
                outcome.protected,
                outcome.limited,
            )
        ):
            log(
                "Copilot %s session cleanup: removed=%d failed=%d skipped=%d "
                "protected=%d limit_reached=%s; remaining data will be revisited "
                "by a later maintenance pass or startup",
                phase,
                outcome.removed,
                outcome.failed,
                outcome.skipped,
                outcome.protected,
                outcome.limited,
            )
