"""Conservative, bounded recovery of unused native runtime versions on Linux."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from uuid import uuid4

from .cleanup import async_wait_cleanup
from .const import DATA_PROVISION_LOCK, DATA_RUNTIME_PRUNE, DOMAIN
from .provisioning import runtime_cache_root

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
SCAN_LIMIT = 64
DELETE_LIMIT = 4
TREE_ENTRY_LIMIT = 1024
DEPTH_LIMIT = 32
PROCESS_LIMIT = 4096
PROC_BYTES_LIMIT = 32 * 1024 * 1024
PROC_READ_LIMIT = 4 * 1024 * 1024
PRUNE_TIMEOUT_SECONDS = 10
_PROC = Path("/proc")
_FDINFO = Path("/proc/self/fdinfo")
_VERSION = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_TRASH = re.compile(r"\.pruning-(.+)-[0-9a-f]{32}")


class _Deferred(Exception):
    """Safety cannot be established; do not authorize another mutation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


class _Limited(_Deferred):
    def __init__(self) -> None:
        super().__init__("budget")


@dataclass
class _Budget:
    deadline: float
    stopped: threading.Event = field(default_factory=threading.Event)
    processes: int = 0
    metadata_bytes: int = 0
    tree_entries: int = 0

    def check(self) -> None:
        if self.stopped.is_set():
            raise _Deferred("stopping")
        if time.monotonic() >= self.deadline:
            raise _Limited

    def read(self, path: Path) -> bytes:
        self.check()
        limit = min(PROC_READ_LIMIT, PROC_BYTES_LIMIT - self.metadata_bytes)
        if limit <= 0:
            raise _Limited
        with path.open("rb") as stream:
            data = stream.read(limit)
        self.metadata_bytes += len(data)
        # A full buffer is inconclusive, even if EOF might be exactly at the cap.
        if len(data) == limit:
            raise _Limited
        return data

    def entry(self) -> None:
        self.check()
        if self.tree_entries >= TREE_ENTRY_LIMIT:
            raise _Limited
        self.tree_entries += 1

    def link(self, path: Path) -> str:
        self.check()
        if PROC_BYTES_LIMIT - self.metadata_bytes < 4096:
            raise _Limited
        value = os.readlink(path)
        self.metadata_bytes += len(os.fsencode(value))
        return value


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _process_stat(data: bytes) -> tuple[bytes, int, int, int]:
    fields = data.rsplit(b") ", 1)[1].split()
    return fields[0], int(fields[6]), int(fields[19]), int(fields[17])


def _process_ids(budget: _Budget) -> set[str]:
    result = set()
    with os.scandir(_PROC) as entries:
        for entry in entries:
            budget.check()
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            if budget.processes >= PROCESS_LIMIT:
                raise _Limited
            budget.processes += 1
            result.add(entry.name)
    if str(os.getpid()) not in result:
        raise _Deferred("process_visibility")
    return result


def _check_proc_mount(budget: _Budget) -> None:
    found = False
    for line in os.fsdecode(budget.read(_PROC / "self/mountinfo")).splitlines():
        fields = line.split()
        if len(fields) < 10 or _unescape(fields[4]) != str(_PROC):
            continue
        separator = fields.index("-")
        options = fields[5].split(",") + fields[separator + 3].split(",")
        if (
            found
            or fields[3] != "/"
            or fields[separator + 1] != "proc"
            or any(
                option.startswith("hidepid=") and option != "hidepid=0"
                for option in options
            )
        ):
            raise _Deferred("process_visibility")
        found = True
    if not found:
        raise _Deferred("process_visibility")


def _referenced_version(value: str, root: Path) -> str | None:
    value = value.removesuffix(" (deleted)")
    if not value.startswith("/"):
        return None
    path = Path(os.path.normpath(value))
    if path.is_relative_to(root) and len(parts := path.relative_to(root).parts):
        return parts[0]
    return None


def _process_versions(pid: str, root: Path, budget: _Budget) -> set[str]:
    directory = _PROC / pid
    before = _process_stat(budget.read(directory / "stat"))
    if before[1] & 0x00200000:
        # PF_KTHREAD identifies kernel threads, which own no userspace files.
        return set()
    if before[0] in (b"Z", b"X"):
        if before[3] > 1:
            # A leader that exited first stays a zombie while its threads run.
            raise _Deferred("process_visibility")
        return set()
    for namespace in ("pid", "mnt"):
        if _identity((directory / "ns" / namespace).stat()) != _identity(
            (_PROC / "self/ns" / namespace).stat()
        ):
            raise _Deferred("process_visibility")
    executable = budget.link(directory / "exe")
    executable_id = _identity((directory / "exe").stat())
    cwd = budget.link(directory / "cwd")
    arguments = os.fsdecode(budget.read(directory / "cmdline")).split("\0")
    mappings = os.fsdecode(budget.read(directory / "maps")).splitlines()
    references = [executable, cwd]
    for argument in arguments:
        references.append(os.path.join(cwd, argument.split("=", 1)[-1]))
    for mapping in mappings:
        fields = mapping.split(maxsplit=5)
        if len(fields) < 5:
            raise _Deferred("process_metadata")
        if len(fields) == 6:
            references.append(_unescape(fields[5]))
    versions = {
        version
        for reference in references
        if (version := _referenced_version(reference, root)) is not None
    }
    if (
        _process_stat(budget.read(directory / "stat"))[2] != before[2]
        or _identity((directory / "exe").stat()) != executable_id
        or budget.link(directory / "exe") != executable
        or budget.link(directory / "cwd") != cwd
    ):
        raise _Deferred("process_changed")
    for index, argument in enumerate(arguments):
        executable_argument = Path(argument)
        if not executable_argument.is_absolute():
            executable_argument = Path(cwd) / executable_argument
        if "copilot" in executable_argument.parts and executable_argument.suffix in (
            ".py",
            ".pyc",
        ):
            raise _Deferred("runtime_launcher")
        if argument == "-c" and index + 1 < len(arguments):
            script = arguments[index + 1]
            if "copilot" in script or str(root) in script:
                raise _Deferred("runtime_launcher")
        if argument == "-m" and index + 1 < len(arguments):
            module = arguments[index + 1]
            if module.startswith("copilot") or (
                module == "homeassistant" and pid != str(os.getpid())
            ):
                raise _Deferred("runtime_launcher")
    if not versions and any(
        Path(argument).name in ("copilot", "copilot-runtime")
        or (Path(argument).name == "hass" and pid != str(os.getpid()))
        or str(root) in argument
        for argument in arguments
    ):
        raise _Deferred("runtime_launcher")
    return versions


def _in_use(root: Path, names: set[str], budget: _Budget) -> bool:
    """A complete inventory is required even if no matching process is found."""
    try:
        _check_proc_mount(budget)
        pids = _process_ids(budget)
        versions: set[str] = set()
        for pid in pids:
            budget.check()
            try:
                versions.update(_process_versions(pid, root, budget))
            except FileNotFoundError:
                # Only a missing PID directory confirms exit, not a missing exe.
                try:
                    (_PROC / pid).lstat()
                except FileNotFoundError:
                    continue
                else:
                    raise _Deferred("process_changed") from None
        if _process_ids(budget) - pids:
            raise _Deferred("process_changed")
        return bool(names & versions)
    except (OSError, ValueError, IndexError):
        raise _Deferred("process_metadata") from None


def _rename_noreplace(root_fd: int, source: str, target: str) -> None:
    # Linux renameat2 avoids overwriting even an empty, concurrently created dir.
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(root_fd, os.fsencode(source), root_fd, os.fsencode(target), 1):
        raise OSError(ctypes.get_errno(), "Runtime cache claim failed")


def _supported() -> bool:
    return (
        sys.platform == "linux"
        and hasattr(os, "O_NOFOLLOW")
        and {os.open, os.stat, os.unlink, os.rmdir}.issubset(os.supports_dir_fd)
        and hasattr(ctypes.CDLL(None), "renameat2")
    )


def _open_directory(name: str | Path, parent: int | None = None) -> int:
    return os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
    )


def _open_root(root: Path) -> int:
    fd = _open_directory(root.parent.parent.parent)
    try:
        for name in (".storage", DOMAIN, "runtime"):
            child = _open_directory(name, fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _mount_id(fd: int, budget: _Budget) -> int:
    for line in budget.read(_FDINFO / str(fd)).splitlines():
        if line.startswith(b"mnt_id:"):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                break
    raise _Deferred("cache_mount")


@dataclass
class _Outcome:
    removed: int = 0
    failed: int = 0
    skipped: int = 0
    protected: int = 0
    deferred: int = 0
    limited: bool = False
    reason: str = "complete"

    def defer(self, error: _Deferred) -> None:
        self.deferred += 1
        self.limited |= isinstance(error, _Limited)
        self.reason = error.reason

    def report(self) -> None:
        log = (
            _LOGGER.warning
            if self.failed or self.limited or self.deferred
            else _LOGGER.info
        )
        if any(
            (
                self.removed,
                self.failed,
                self.skipped,
                self.protected,
                self.deferred,
                self.limited,
            )
        ):
            log(
                "Copilot runtime cache cleanup: removed=%d failed=%d skipped=%d "
                "protected=%d deferred=%d limit_reached=%s reason=%s",
                self.removed,
                self.failed,
                self.skipped,
                self.protected,
                self.deferred,
                self.limited,
                self.reason,
            )


class _PrunePass:
    def __init__(self, root: Path, pin: str, budget: _Budget) -> None:
        self.root = root
        self.pin = pin
        self.budget = budget
        self.outcome = _Outcome()
        self.root_fd: int | None = None
        self.claimed: list[tuple[str, tuple[int, int]]] = []

    def _verify_root(self) -> int:
        assert self.root_fd is not None
        fd = _open_root(self.root)
        try:
            if _identity(os.fstat(fd)) != _identity(os.fstat(self.root_fd)):
                raise _Deferred("cache_changed")
        finally:
            os.close(fd)
        return self.root_fd

    def claim(self) -> None:
        try:
            self.budget.check()
            self.root = (
                self.root.parent.parent.parent.resolve()
                / ".storage"
                / DOMAIN
                / "runtime"
            )
            try:
                self.root_fd = _open_root(self.root)
            except FileNotFoundError:
                return
            root_fd = self.root_fd
            names = []
            with os.scandir(root_fd) as entries:
                for _ in range(SCAN_LIMIT):
                    self.budget.check()
                    entry = next(entries, None)
                    if entry is None:
                        break
                    names.append(entry.name)
                else:
                    self.outcome.limited = True
            attempted = 0
            for name in names:
                self.budget.check()
                trash = _TRASH.fullmatch(name)
                version = trash[1] if trash else name
                if version == self.pin and not trash:
                    continue
                if not _VERSION.fullmatch(version):
                    self.outcome.skipped += 1
                    continue
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        self.outcome.skipped += 1
                        continue
                    if attempted >= DELETE_LIMIT:
                        self.outcome.limited = True
                        break
                    attempted += 1
                    trash = _TRASH.fullmatch(name)
                    versions = {name}
                    if trash is not None:
                        versions.add(trash[1])
                    if _in_use(self.root, versions, self.budget):
                        self.outcome.protected += 1
                        continue
                    self.budget.check()
                    self._verify_root()
                    if _identity(
                        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    ) != _identity(info):
                        raise _Deferred("cache_changed")
                    target = name if trash else f".pruning-{name}-{uuid4().hex}"
                    if not trash:
                        self.budget.check()
                        _rename_noreplace(root_fd, name, target)
                    if _identity(
                        os.stat(target, dir_fd=root_fd, follow_symlinks=False)
                    ) != _identity(info):
                        raise _Deferred("cache_changed")
                    self.claimed.append((target, _identity(info)))
                except OSError:
                    self.outcome.failed += 1
        except _Deferred as error:
            self.outcome.defer(error)
        except OSError:
            self.outcome.failed += 1
            self.outcome.reason = "cache_unavailable"

    def _remove_tree(
        self, parent: int, name: str, identity: tuple[int, int], depth: int
    ) -> None:
        self.budget.check()
        if depth > DEPTH_LIMIT:
            raise _Limited
        fd = _open_directory(name, parent)
        try:
            if _identity(os.fstat(fd)) != identity:
                raise _Deferred("cache_changed")
            if _mount_id(fd, self.budget) != _mount_id(parent, self.budget):
                raise _Deferred("cache_mount")
            while True:
                # Close each small inventory before mutating it. Reopening also
                # lets oversized flat directories make progress across startups.
                self.budget.check()
                remaining = TREE_ENTRY_LIMIT - self.budget.tree_entries
                if remaining <= 0:
                    raise _Limited
                names = []
                with os.scandir(fd) as entries:
                    for _ in range(min(32, remaining)):
                        entry = next(entries, None)
                        if entry is None:
                            break
                        self.budget.entry()
                        names.append(entry.name)
                if not names:
                    break
                for child in names:
                    self.budget.check()
                    info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if info.st_dev != identity[0]:
                            raise _Deferred("cache_mount")
                        self._remove_tree(fd, child, _identity(info), depth + 1)
                    else:
                        self.budget.check()
                        os.unlink(child, dir_fd=fd)
            self.budget.check()
            if (
                _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
                != identity
            ):
                raise _Deferred("cache_changed")
            self.budget.check()
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(fd)

    def delete(self, candidate: tuple[str, tuple[int, int]]) -> None:
        name, identity = candidate
        try:
            self.budget.check()
            trash = _TRASH.fullmatch(name)
            versions = {name}
            if trash is not None:
                versions.add(trash[1])
            if _in_use(self.root, versions, self.budget):
                self.outcome.protected += 1
                return
            root_fd = self._verify_root()
            self._remove_tree(root_fd, name, identity, 0)
            self.outcome.removed += 1
        except _Deferred as error:
            self.outcome.defer(error)
        except OSError:
            self.outcome.failed += 1

    async def worker(self, call: Callable[[], _T]) -> _T:
        task = asyncio.create_task(asyncio.to_thread(call))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.budget.stopped.set()
            try:
                await async_wait_cleanup(task)
            except Exception as error:
                _LOGGER.error(
                    "Copilot runtime cache worker failed during cancellation (%s)",
                    type(error).__name__,
                )
            raise

    def close(self) -> None:
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None


async def async_prune_runtime_cache(
    hass: HomeAssistant, stopped: threading.Event | None = None
) -> None:
    """Run one best-effort pass without abandoning filesystem ownership."""
    try:
        from copilot._cli_version import CLI_VERSION
    except Exception as error:
        _LOGGER.error(
            "Copilot runtime cache cleanup could not load the SDK pin (%s)",
            type(error).__name__,
        )
        return

    if not _supported() or not isinstance(CLI_VERSION, str):
        _LOGGER.debug("Copilot runtime cache cleanup unavailable on this host")
        return
    if not _VERSION.fullmatch(CLI_VERSION):
        _LOGGER.warning("Copilot runtime cache cleanup deferred: unrecognized SDK pin")
        return
    budget = _Budget(time.monotonic() + PRUNE_TIMEOUT_SECONDS)
    if stopped is not None:
        budget.stopped = stopped
    pruning = _PrunePass(runtime_cache_root(hass), CLI_VERSION, budget)
    lock = hass.data.setdefault(DATA_PROVISION_LOCK, asyncio.Lock())
    try:
        try:
            async with asyncio.timeout(max(0, budget.deadline - time.monotonic())):
                await lock.acquire()
        except TimeoutError:
            pruning.outcome.defer(_Limited())
            return
        try:
            if hass.is_stopping:
                budget.stopped.set()
            await pruning.worker(pruning.claim)
        finally:
            lock.release()
        for candidate in pruning.claimed:
            if hass.is_stopping:
                budget.stopped.set()
            if pruning.outcome.deferred or budget.stopped.is_set():
                break
            await pruning.worker(partial(pruning.delete, candidate))
    except asyncio.CancelledError:
        budget.stopped.set()
        pruning.outcome.defer(_Deferred("stopping"))
        raise
    except Exception as error:
        pruning.outcome.failed += 1
        pruning.outcome.reason = "unexpected_error"
        _LOGGER.error(
            "Copilot runtime cache cleanup failed unexpectedly (%s)",
            type(error).__name__,
        )
    finally:
        pruning.close()
        pruning.outcome.report()


def async_schedule_runtime_prune(hass: HomeAssistant) -> None:
    """Schedule shared maintenance only once, independently of any one entry."""
    if DATA_RUNTIME_PRUNE in hass.data:
        return
    hass.data[DATA_RUNTIME_PRUNE] = True
    unsubscribe_start: Callable[[], None] | None = None
    unsubscribe_stop: Callable[[], None] | None = None
    task: asyncio.Task[None] | None = None
    stopped = threading.Event()
    try:
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP
        from homeassistant.core import callback
        from homeassistant.helpers.start import async_at_started

        @callback
        def start(_: HomeAssistant) -> None:
            nonlocal task
            if task is None and not stopped.is_set() and not hass.is_stopping:
                coroutine = async_prune_runtime_cache(hass, stopped)
                try:
                    task = hass.async_create_background_task(
                        coroutine, f"{DOMAIN} runtime cache cleanup"
                    )
                except Exception as error:
                    coroutine.close()
                    stopped.set()
                    _LOGGER.error(
                        "Unable to start Copilot runtime cache cleanup (%s)",
                        type(error).__name__,
                    )

        async def stop(_event: object) -> None:
            stopped.set()
            if unsubscribe_start is not None:
                unsubscribe_start()
            if task is not None:
                task.cancel()

                async def drain() -> None:
                    await asyncio.gather(task, return_exceptions=True)

                await async_wait_cleanup(asyncio.create_task(drain()))

        unsubscribe_stop = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
        unsubscribe_start = async_at_started(hass, start)
    except Exception as error:
        stopped.set()
        if unsubscribe_start is not None:
            unsubscribe_start()
        if unsubscribe_stop is not None:
            unsubscribe_stop()
        _LOGGER.error(
            "Unable to schedule Copilot runtime cache cleanup (%s)",
            type(error).__name__,
        )
