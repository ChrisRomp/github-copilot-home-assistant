"""Runtime cache ownership, filesystem confinement, and HA scheduling."""

import asyncio
import logging
import os
import shutil
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from copilot import _cli_version

from custom_components.github_copilot import runtime_cache as cache
from custom_components.github_copilot.const import DATA_PROVISION_LOCK
from custom_components.github_copilot.provisioning import _INSTALL_SCRIPT


@pytest.fixture
def filesystem(tmp_path, monkeypatch):
    root = tmp_path / ".storage/github_copilot/runtime"
    root.mkdir(parents=True)
    hass = SimpleNamespace(
        data={},
        is_stopping=False,
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
    )
    monkeypatch.setattr(cache, "_supported", lambda: True)
    monkeypatch.setattr(_cli_version, "CLI_VERSION", "1.0.103")
    if sys.platform != "linux":
        monkeypatch.setattr(cache, "_mount_id", lambda fd, budget: 1)

        def rename(fd, source, target):
            try:
                os.stat(target, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(source, target, src_dir_fd=fd, dst_dir_fd=fd)
            else:
                raise FileExistsError

        monkeypatch.setattr(cache, "_rename_noreplace", rename)

    def version(name, relative="prebuilds/linux-arm64/copilot-runtime"):
        directory = root / name
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("cached runtime")
        return directory

    return SimpleNamespace(hass=hass, root=root, version=version)


@pytest.fixture
def unused(filesystem, monkeypatch):
    guard = Mock(return_value=False)
    monkeypatch.setattr(cache, "_in_use", guard)
    filesystem.guard = guard
    return filesystem


def budget():
    return cache._Budget(time.monotonic() + 30)


@pytest.fixture
def procfs(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(cache, "_PROC", root)
    namespaces = tmp_path / "namespaces"
    namespaces.mkdir()
    for name in ("mnt", "pid"):
        (namespaces / name).write_text(name)
    executable = tmp_path / "system-python"
    executable.write_text("executable")

    def process(
        pid, *, exe=executable, cwd=tmp_path, args=None, maps="", start=100, state="S"
    ):
        directory = root / str(pid)
        directory.mkdir(exist_ok=True)
        fields = [state] + ["0"] * 19
        fields[1] = "1"  # Reparented processes must not be excluded.
        fields[19] = str(start)
        (directory / "stat").write_text(f"{pid} (runtime) {' '.join(fields)}\n")
        (directory / "ns").mkdir(exist_ok=True)
        for name in ("mnt", "pid"):
            (directory / "ns" / name).symlink_to(namespaces / name)
        (directory / "exe").symlink_to(exe)
        (directory / "cwd").symlink_to(cwd)
        if args is None:
            args = ["python", "-m", "unrelated"]
        (directory / "cmdline").write_bytes(
            b"\0".join(os.fsencode(arg) for arg in args) + b"\0"
        )
        (directory / "maps").write_text(maps)
        return directory

    current = process(os.getpid())
    (root / "self").symlink_to(current)
    (current / "mountinfo").write_text(f"1 0 0:1 / {root} rw - proc proc rw\n")
    return SimpleNamespace(root=root, process=process, current=current)


@pytest.mark.parametrize("pin,old", [("1.0.103", "1.0.102"), ("1.0.102", "1.0.103")])
async def test_upgrade_and_rollback_preserve_pin(unused, monkeypatch, pin, old):
    monkeypatch.setattr(_cli_version, "CLI_VERSION", pin)
    current = unused.version(pin, "prebuilds/.runtime-bundle-current/partial")
    previous = unused.version(old)
    contents = list(current.rglob("*"))
    await cache.async_prune_runtime_cache(unused.hass)
    assert current.is_dir()
    assert list(current.rglob("*")) == contents
    assert not previous.exists()
    assert not list(unused.root.glob(".pruning-*"))
    assert all(call.args[1] != pin for call in unused.guard.call_args_list)


@pytest.mark.parametrize(
    "name",
    ["1.0.102", "1.0.102-0.unstable.r123.gabc", "1.0.1-rc-test.1+build-2"],
)
async def test_obsolete_incomplete_downloads_are_eligible(unused, name):
    old = unused.version(name, "prebuilds/.runtime-bundle-abandoned/partial")
    await cache.async_prune_runtime_cache(unused.hass)
    assert not old.exists()


async def test_live_version_is_not_even_renamed(filesystem, procfs):
    old = filesystem.version("1.0.102", "copilot-runtime")
    procfs.process(12345, exe=old / "copilot-runtime")
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert (old / "copilot-runtime").is_file()
    assert not list(filesystem.root.glob(".pruning-*"))
    shutil.rmtree(procfs.root / "12345")
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert not old.exists()


@pytest.mark.parametrize("source", ["exe", "maps", "argument", "cwd", "relative"])
def test_process_evidence_protects_exact_version(filesystem, procfs, source):
    old = filesystem.version("1.0.102", "copilot-runtime")
    kwargs = {}
    if source == "exe":
        kwargs["exe"] = old / "copilot-runtime"
    elif source == "maps":
        kwargs["maps"] = f"1000-2000 r-xp 0 00:01 1 {old}/runtime.node\n"
    elif source == "cwd":
        kwargs["cwd"] = old
    elif source == "relative":
        kwargs["cwd"] = filesystem.root
        kwargs["args"] = ["launcher", "1.0.102/copilot-runtime"]
    else:
        kwargs["args"] = ["launcher", f"--runtime={old}/copilot-runtime"]
    procfs.process(12345, **kwargs)
    assert cache._in_use(filesystem.root, {old.name}, budget())
    assert not cache._in_use(filesystem.root, {"1.0.10"}, budget())


@pytest.mark.parametrize(
    "args",
    [
        ["python", "-c", _INSTALL_SCRIPT],
        ["python", "-m", "copilot", "download-runtime"],
        ["python", "-m", "homeassistant"],
        [
            "python",
            "/venv/lib/python3.14/site-packages/copilot/_cli_download.py",
        ],
        ["/unresolved/copilot-runtime"],
    ],
)
async def test_ambiguous_installer_or_launcher_defers_entire_pass(
    filesystem, procfs, args, caplog
):
    old = filesystem.version("1.0.102")
    procfs.process(12345, args=args)
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert old.is_dir()
    assert "reason=runtime_launcher" in caplog.text
    assert str(filesystem.root) not in caplog.text
    assert _INSTALL_SCRIPT not in caplog.text


@pytest.mark.parametrize("failure", ["permissions", "exe", "namespace", "hidepid"])
async def test_incomplete_process_visibility_never_authorizes_mutation(
    filesystem, procfs, monkeypatch, failure, caplog
):
    old = filesystem.version("1.0.102")
    other = procfs.process(12345)
    if failure == "permissions":
        read = cache._Budget.read

        def denied(self, path):
            if path == other / "maps":
                raise PermissionError("PRIVATE")
            return read(self, path)

        monkeypatch.setattr(cache._Budget, "read", denied)
    elif failure == "exe":
        (other / "exe").unlink()
    elif failure == "namespace":
        (other / "ns/mnt").unlink()
        (other / "ns/mnt").write_text("different namespace")
    else:
        (procfs.current / "mountinfo").write_text(
            f"1 0 0:1 / {procfs.root} rw - proc proc rw,hidepid=2\n"
        )
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert old.is_dir()
    assert not list(filesystem.root.glob(".pruning-*"))
    assert "deferred=1" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_pid_reuse_is_unknown_not_unused(filesystem, procfs, monkeypatch):
    read = cache._Budget.read
    calls = 0

    def reused(self, path):
        nonlocal calls
        data = read(self, path)
        if path == procfs.current / "stat":
            calls += 1
            if calls == 2:
                return data.replace(b" 100\n", b" 101\n")
        return data

    monkeypatch.setattr(cache._Budget, "read", reused)
    with pytest.raises(cache._Deferred):
        cache._in_use(filesystem.root, {"1.0.102"}, budget())


def test_process_state_changes_are_not_pid_reuse(filesystem, procfs, monkeypatch):
    read = cache._Budget.read
    calls = 0

    def running(self, path):
        nonlocal calls
        data = read(self, path)
        if path == procfs.current / "stat":
            calls += 1
            if calls == 2:
                return data.replace(b") S ", b") R ")
        return data

    monkeypatch.setattr(cache._Budget, "read", running)
    assert not cache._in_use(filesystem.root, {"1.0.102"}, budget())


@pytest.mark.parametrize("limit", ["processes", "bytes", "read", "deadline"])
def test_process_inventory_is_bounded(filesystem, procfs, monkeypatch, limit):
    bounds = budget()
    if limit == "processes":
        monkeypatch.setattr(cache, "PROCESS_LIMIT", 1)
    elif limit == "bytes":
        monkeypatch.setattr(cache, "PROC_BYTES_LIMIT", 10)
    elif limit == "read":
        monkeypatch.setattr(cache, "PROC_READ_LIMIT", 10)
    else:
        bounds.deadline = 0
    with pytest.raises(cache._Limited):
        cache._in_use(filesystem.root, {"1.0.102"}, bounds)
    assert bounds.processes <= cache.PROCESS_LIMIT
    assert bounds.metadata_bytes <= cache.PROC_BYTES_LIMIT


async def test_valid_trash_is_retried_including_the_current_pin(unused):
    trash = unused.version(f".pruning-1.0.100-{'a' * 32}")
    retained = [
        unused.version(".pruning-not-owned"),
        unused.version("not-a-version"),
    ]
    await cache.async_prune_runtime_cache(unused.hass)
    assert not trash.exists()
    assert all(path.is_dir() for path in retained)


async def test_live_original_version_argv_protects_claimed_trash(filesystem, procfs):
    old = filesystem.version("1.0.102", "copilot-runtime")
    pruning = cache._PrunePass(filesystem.root, "1.0.103", budget())
    try:
        pruning.claim()
        assert pruning.claimed
        trash_name, identity = pruning.claimed[0]
        assert not old.exists()

        # A wrapper started after the claim still has its original argv path;
        # unlike /proc exe and maps paths, cmdline does not follow the rename.
        procfs.process(
            12345,
            args=["launcher", f"{filesystem.root}/1.0.102/copilot-runtime"],
        )
        pruning.delete((trash_name, identity))
        assert (filesystem.root / trash_name / "copilot-runtime").is_file()
        assert pruning.outcome.protected == 1
    finally:
        pruning.close()


async def test_current_pin_trash_is_removed_after_its_process_exits(filesystem, procfs):
    old = filesystem.version("1.0.102", "copilot-runtime")
    trash = filesystem.root / f".pruning-1.0.102-{'a' * 32}"
    old.rename(trash)
    filesystem.version("1.0.102")
    procfs.process(
        12345, args=["launcher", f"{filesystem.root}/1.0.102/copilot-runtime"]
    )
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert trash.is_dir()
    shutil.rmtree(procfs.root / "12345")
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert not trash.exists()


async def test_live_trash_is_retained(filesystem, procfs):
    trash = filesystem.version(f".pruning-1.0.100-{'a' * 32}", "copilot-runtime")
    procfs.process(12345, exe=trash / "copilot-runtime")
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert (trash / "copilot-runtime").is_file()


@pytest.mark.parametrize("component", [".storage", "github_copilot", "runtime"])
async def test_symlinked_storage_components_are_rejected(unused, tmp_path, component):
    old = unused.version("1.0.102")
    path = {
        ".storage": tmp_path / ".storage",
        "github_copilot": unused.root.parent,
        "runtime": unused.root,
    }[component]
    outside = tmp_path / "outside"
    relative = old.relative_to(path)
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=True)
    await cache.async_prune_runtime_cache(unused.hass)
    assert (outside / relative).is_dir()
    unused.guard.assert_not_called()


async def test_links_and_unknown_files_do_not_escape_or_warn(unused, tmp_path, caplog):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep")
    links = [
        unused.root / "1.0.101",
        unused.root / f".pruning-1.0.100-{'a' * 32}",
    ]
    for link in links:
        link.symlink_to(outside, target_is_directory=True)
    (unused.root / ".DS_Store").write_text("unknown")
    old = unused.version("1.0.102")
    (old / "nested-link").symlink_to(outside, target_is_directory=True)
    caplog.set_level(logging.INFO)
    await cache.async_prune_runtime_cache(unused.hass)
    assert sentinel.read_text() == "keep"
    assert all(link.is_symlink() for link in links)
    assert (unused.root / ".DS_Store").exists()
    assert not old.exists()
    assert all(record.levelno < logging.WARNING for record in caplog.records)


async def test_root_swap_cannot_redirect_claim(unused, tmp_path, monkeypatch):
    old = unused.version("1.0.102")
    original = tmp_path / "original"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / old.name
    sentinel.mkdir()

    def swap(*args):
        unused.root.rename(original)
        unused.root.symlink_to(outside, target_is_directory=True)
        return False

    monkeypatch.setattr(cache, "_in_use", swap)
    await cache.async_prune_runtime_cache(unused.hass)
    assert (original / old.name).is_dir()
    assert sentinel.is_dir()


async def test_candidate_swap_before_claim_is_retained(unused, monkeypatch):
    old = unused.version("1.0.102")
    moved = unused.root / "operator-moved"

    def swap(*args):
        old.rename(moved)
        old.mkdir()
        (old / "sentinel").write_text("replacement")
        return False

    monkeypatch.setattr(cache, "_in_use", swap)
    await cache.async_prune_runtime_cache(unused.hass)
    assert moved.is_dir()
    assert (old / "sentinel").read_text() == "replacement"
    assert not list(unused.root.glob(".pruning-*"))


async def test_nested_mounts_are_not_traversed(unused, monkeypatch, caplog):
    unused.version("1.0.102", "nested/sentinel")
    monkeypatch.setattr(cache, "_mount_id", Mock(side_effect=[1, 1, 2, 1]))
    await cache.async_prune_runtime_cache(unused.hass)
    assert list(unused.root.glob(".pruning-*/nested/sentinel"))
    assert "reason=cache_mount" in caplog.text


@pytest.mark.parametrize("stage", ["rename", "delete"])
async def test_failures_are_sanitized_and_retryable(unused, monkeypatch, caplog, stage):
    old = unused.version("1.0.102")
    with monkeypatch.context() as patch:
        if stage == "rename":
            patch.setattr(
                cache, "_rename_noreplace", Mock(side_effect=OSError("PRIVATE /path"))
            )
        else:
            patch.setattr(
                cache._PrunePass,
                "_remove_tree",
                Mock(side_effect=PermissionError("PRIVATE /path")),
            )
        await cache.async_prune_runtime_cache(unused.hass)
    assert "failed=1" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert str(unused.root) not in caplog.text
    if stage == "rename":
        assert old.is_dir()
    else:
        assert list(unused.root.glob(".pruning-*"))
    await cache.async_prune_runtime_cache(unused.hass)
    assert not list(unused.root.iterdir())


async def test_rename_does_not_replace_existing_destination(unused):
    source = unused.version("1.0.101")
    destination = unused.root / "occupied"
    destination.mkdir()
    fd = cache._open_root(unused.root)
    try:
        with pytest.raises(OSError):
            cache._rename_noreplace(fd, source.name, destination.name)
    finally:
        os.close(fd)
    assert source.is_dir()
    assert destination.is_dir()


async def test_scan_and_tree_attempt_limits(unused, monkeypatch, caplog):
    for version in range(8):
        unused.version(f"1.0.{version}")
    monkeypatch.setattr(cache, "SCAN_LIMIT", 5)
    monkeypatch.setattr(cache, "DELETE_LIMIT", 2)
    await cache.async_prune_runtime_cache(unused.hass)
    assert len(list(unused.root.iterdir())) == 6
    assert "limit_reached=True" in caplog.text
    assert unused.guard.call_count == 4  # One check before each claim and deletion.


async def test_large_tree_makes_bounded_progress_and_resumes(
    unused, monkeypatch, caplog
):
    old = unused.root / "1.0.102"
    old.mkdir()
    for index in range(40):
        (old / f"file-{index}").write_text("cached")
    monkeypatch.setattr(cache, "TREE_ENTRY_LIMIT", 12)
    await cache.async_prune_runtime_cache(unused.hass)
    trash = next(unused.root.glob(".pruning-*"))
    assert len(list(trash.iterdir())) == 28
    assert "limit_reached=True" in caplog.text
    for _ in range(3):
        await cache.async_prune_runtime_cache(unused.hass)
    assert not list(unused.root.iterdir())


async def test_depth_limit_preserves_deep_tree(unused, monkeypatch, caplog):
    unused.version("1.0.102", "a/b/c/d/file")
    monkeypatch.setattr(cache, "DEPTH_LIMIT", 2)
    await cache.async_prune_runtime_cache(unused.hass)
    assert list(unused.root.glob(".pruning-*/a/b/c/d/file"))
    assert "limit_reached=True" in caplog.text


async def test_lock_acquisition_has_a_deadline(unused, monkeypatch, caplog):
    old = unused.version("1.0.102")
    lock = unused.hass.data[DATA_PROVISION_LOCK] = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(cache, "PRUNE_TIMEOUT_SECONDS", 0.01)
    try:
        await cache.async_prune_runtime_cache(unused.hass)
        assert old.is_dir()
        unused.guard.assert_not_called()
        assert "limit_reached=True" in caplog.text
    finally:
        lock.release()


def test_expired_root_recheck_cannot_issue_a_claim(unused, monkeypatch):
    old = unused.version("1.0.102")
    verify = cache._PrunePass._verify_root

    def expired(self):
        fd = verify(self)
        self.budget.deadline = 0
        return fd

    monkeypatch.setattr(cache._PrunePass, "_verify_root", expired)
    pruning = cache._PrunePass(unused.root, "1.0.103", budget())
    try:
        pruning.claim()
        assert pruning.outcome.limited
        assert old.is_dir()
        assert not pruning.claimed
    finally:
        pruning.close()


@pytest.mark.parametrize("operation", ["unlink", "rmdir"])
def test_expired_stat_cannot_issue_a_deletion(unused, monkeypatch, operation):
    old = unused.root / "1.0.102"
    old.mkdir()
    if operation == "unlink":
        (old / "file").write_text("keep")
    pruning = cache._PrunePass(unused.root, "1.0.103", budget())
    try:
        pruning.claim()
        candidate = pruning.claimed[0]
        name = "file" if operation == "unlink" else candidate[0]
        stat = os.stat

        def expired(path, *args, **kwargs):
            info = stat(path, *args, **kwargs)
            if path == name:
                pruning.budget.deadline = 0
            return info

        monkeypatch.setattr(cache.os, "stat", expired)
        pruning.delete(candidate)
        assert pruning.outcome.limited
        trash = unused.root / candidate[0]
        assert trash.is_dir()
        if operation == "unlink":
            assert (trash / "file").read_text() == "keep"
    finally:
        pruning.close()


@pytest.mark.parametrize("stage", ["claim", "delete"])
async def test_cancellation_drains_workers_before_releasing_ownership(
    unused, monkeypatch, stage
):
    unused.version("1.0.102")
    entered = threading.Event()
    finish = threading.Event()
    original = getattr(cache._PrunePass, stage)
    observed = []

    def blocked(self, *args):
        entered.set()
        assert finish.wait(5)
        if stage == "claim":
            assert unused.hass.data[DATA_PROVISION_LOCK].locked()
        observed.append(self.budget.stopped.is_set())
        return original(self, *args)

    monkeypatch.setattr(cache._PrunePass, stage, blocked)
    task = asyncio.create_task(cache.async_prune_runtime_cache(unused.hass))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        if stage == "claim":
            assert unused.hass.data[DATA_PROVISION_LOCK].locked()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert observed == [True]
    assert not unused.hass.data[DATA_PROVISION_LOCK].locked()
    assert list(unused.root.iterdir())


@pytest.mark.parametrize("skip", ["platform", "pin", "stopping"])
async def test_unavailable_or_stopping_hosts_do_not_prune(unused, monkeypatch, skip):
    old = unused.version("1.0.102")
    if skip == "platform":
        monkeypatch.setattr(cache, "_supported", lambda: False)
    elif skip == "pin":
        monkeypatch.setattr(_cli_version, "CLI_VERSION", None)
    else:
        unused.hass.is_stopping = True
    await cache.async_prune_runtime_cache(unused.hass)
    assert old.is_dir()
    unused.guard.assert_not_called()


@pytest.fixture
async def scheduler(ha_boundary, monkeypatch):
    tasks = []
    listeners = {}
    prune = AsyncMock()
    monkeypatch.setattr(cache, "async_prune_runtime_cache", prune)

    def listen(event, callback):
        listeners.setdefault(event, []).append(callback)
        return Mock(side_effect=lambda: listeners[event].remove(callback))

    def background(coro, name):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    hass = SimpleNamespace(
        data={},
        is_running=False,
        is_stopping=False,
        bus=SimpleNamespace(async_listen_once=Mock(side_effect=listen)),
        async_create_background_task=Mock(side_effect=background),
    )
    yield SimpleNamespace(hass=hass, listeners=listeners, tasks=tasks, prune=prune)
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("already_started", [False, True])
async def test_shared_schedule_waits_for_start_and_runs_only_once(
    scheduler, already_started
):
    scheduler.hass.is_running = already_started
    for _ in range(3):
        cache.async_schedule_runtime_prune(scheduler.hass)
    if not already_started:
        scheduler.hass.async_create_background_task.assert_not_called()
        scheduler.listeners["homeassistant_started"][0](None)
    scheduler.hass.async_create_background_task.assert_called_once()
    await asyncio.gather(*scheduler.tasks)
    scheduler.prune.assert_awaited_once()


async def test_shutdown_before_start_unregisters_pending_cleanup(scheduler):
    cache.async_schedule_runtime_prune(scheduler.hass)
    start = scheduler.listeners["homeassistant_started"][0]
    scheduler.hass.is_stopping = True
    await scheduler.listeners["homeassistant_stop"][0](None)
    assert not scheduler.listeners["homeassistant_started"]
    start(None)
    scheduler.hass.async_create_background_task.assert_not_called()
    scheduler.prune.assert_not_called()


async def test_shutdown_drains_ha_owned_task(scheduler):
    entered = asyncio.Event()
    draining = asyncio.Event()
    finish = asyncio.Event()

    async def prune(hass, stopped):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert stopped.is_set()
            draining.set()
            await finish.wait()
            raise

    scheduler.prune.side_effect = prune
    scheduler.hass.is_running = True
    cache.async_schedule_runtime_prune(scheduler.hass)
    await entered.wait()
    stopping = asyncio.create_task(scheduler.listeners["homeassistant_stop"][0](None))
    await draining.wait()
    assert not stopping.done()
    finish.set()
    await stopping
    assert scheduler.tasks[0].done()


async def test_task_creation_failure_is_sanitized(scheduler, caplog):
    scheduler.hass.async_create_background_task.side_effect = RuntimeError("PRIVATE")
    cache.async_schedule_runtime_prune(scheduler.hass)
    scheduler.listeners["homeassistant_started"][0](None)
    assert "Unable to start Copilot runtime cache cleanup (RuntimeError)" in caplog.text
    assert "PRIVATE" not in caplog.text


async def test_registration_failure_is_sanitized_and_unsubscribes(
    scheduler, monkeypatch, caplog
):
    start = sys.modules["homeassistant.helpers.start"]
    monkeypatch.setattr(
        start, "async_at_started", Mock(side_effect=RuntimeError("PRIVATE"))
    )
    cache.async_schedule_runtime_prune(scheduler.hass)
    assert not scheduler.listeners["homeassistant_stop"]
    assert "Unable to schedule Copilot runtime cache cleanup" in caplog.text
    assert "PRIVATE" not in caplog.text
    scheduler.prune.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs contract")
@pytest.mark.parametrize("orphaned", [False, True])
async def test_linux_live_executable_in_obsolete_cache_is_protected(
    filesystem, orphaned
):
    old = filesystem.root / "1.0.102"
    old.mkdir()
    executable = old / "sleep"
    shutil.copy2(shutil.which("sleep"), executable)
    if orphaned:
        # A separate subreaper owns/reaps the orphan without changing pytest's
        # process-wide child handling or leaving cleanup to the host's init.
        supervisor = """
import ctypes
import os
import signal
import subprocess
import sys
if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
    raise RuntimeError("Cannot establish test child ownership")
launcher = (
    "import subprocess, sys; "
    "p = subprocess.Popen([sys.argv[1], '60'], stdin=subprocess.DEVNULL, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)"
)
parent = subprocess.run(
    [sys.executable, "-c", launcher, sys.argv[1]],
    capture_output=True, text=True, check=True,
)
pid = int(parent.stdout)
try:
    print(pid, flush=True)
    sys.stdin.read(1)
finally:
    os.kill(pid, signal.SIGTERM)
    os.waitpid(pid, 0)
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            supervisor,
            str(executable),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(str(executable), "60")
    try:
        if orphaned:
            pid = int(await asyncio.wait_for(process.stdout.readline(), 10))
            fields = (cache._PROC / str(pid) / "stat").read_bytes()
            parent_pid = int(fields.rsplit(b") ", 1)[1].split()[1])
            assert parent_pid == process.pid
            assert parent_pid != os.getpid()
        else:
            pid = process.pid
        # Inspect a real non-HA process without relying on process names or PPID.
        assert old.name in cache._process_versions(str(pid), filesystem.root, budget())
        # An incompletely visible host must defer, never mistake it for no users.
        await cache.async_prune_runtime_cache(filesystem.hass)
        assert executable.exists()
        assert not list(filesystem.root.glob(".pruning-*"))
    finally:
        if orphaned:
            await asyncio.wait_for(process.communicate(b"\n"), 10)
            assert process.returncode == 0
        else:
            if process.returncode is None:
                process.terminate()
            await process.wait()
    try:
        assert not cache._in_use(filesystem.root, {old.name}, budget())
    except cache._Deferred:
        pytest.skip("This Linux host does not expose a complete process inventory")
    await cache.async_prune_runtime_cache(filesystem.hass)
    assert not old.exists()


@pytest.mark.skipif(
    os.environ.get("HA_COPILOT_HA_TESTS") != "1",
    reason="Requires real Home Assistant",
)
@pytest.mark.parametrize("already_started", [False, True])
async def test_real_ha_scheduling_and_shutdown(tmp_path, monkeypatch, already_started):
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState, HomeAssistant

    hass = HomeAssistant(str(tmp_path))
    prune = AsyncMock()
    monkeypatch.setattr(cache, "async_prune_runtime_cache", prune)
    try:
        if already_started:
            hass.set_state(CoreState.running)
        cache.async_schedule_runtime_prune(hass)
        cache.async_schedule_runtime_prune(hass)
        if not already_started:
            prune.assert_not_called()
            hass.set_state(CoreState.running)
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done(wait_background_tasks=True)
        prune.assert_awaited_once()
    finally:
        await hass.async_stop(force=True)


@pytest.mark.skipif(
    os.environ.get("HA_COPILOT_HA_TESTS") != "1",
    reason="Requires real Home Assistant",
)
async def test_real_ha_shutdown_before_start(tmp_path, monkeypatch):
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant(str(tmp_path))
    prune = AsyncMock()
    monkeypatch.setattr(cache, "async_prune_runtime_cache", prune)
    cache.async_schedule_runtime_prune(hass)
    await hass.async_stop(force=True)
    prune.assert_not_called()
