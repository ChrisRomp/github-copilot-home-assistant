"""Runtime installer isolation, error reporting, and cancellation."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from copilot._cli_version import CLI_VERSION

from custom_components.github_copilot import provisioning


@pytest.fixture
def installer(tmp_path, monkeypatch):
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
    )
    binary = (
        tmp_path / ".storage/github_copilot/runtime" / CLI_VERSION / "copilot-runtime"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text("test executable")
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(str(binary).encode(), b"")),
        kill=Mock(),
        wait=AsyncMock(),
    )
    start = AsyncMock(return_value=process)
    monkeypatch.setattr(provisioning.asyncio, "create_subprocess_exec", start)
    return SimpleNamespace(hass=hass, binary=binary, process=process, start=start)


async def test_cache_is_versioned_persistent_and_environment_is_child_only(installer):
    before = dict(os.environ)
    assert await provisioning.async_runtime_path(installer.hass) == str(
        installer.binary
    )
    assert dict(os.environ) == before
    args = installer.start.call_args
    assert args.kwargs["env"]["COPILOT_CLI_EXTRACT_DIR"] == str(installer.binary.parent)
    assert "ensure_runtime_wrapper" in args.args[2]


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (2, "CPU architecture"),
        (3, "storage permissions"),
        (4, "checksum"),
        (5, "download"),
        (1, "installer failed"),
    ],
)
async def test_installer_errors_are_specific_without_raw_output(
    installer, code, message
):
    installer.process.returncode = code
    installer.process.communicate.return_value = (b"", b"private details")
    with pytest.raises(RuntimeError, match=message) as error:
        await provisioning.async_runtime_path(installer.hass)
    assert "private details" not in str(error.value)


async def test_cancelled_install_terminates_and_reaps_child(installer):
    entered = asyncio.Event()

    async def communicate():
        entered.set()
        await asyncio.Event().wait()

    installer.process.returncode = None
    installer.process.communicate.side_effect = communicate
    task = asyncio.create_task(provisioning.async_runtime_path(installer.hass))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    installer.process.kill.assert_called_once()
    installer.process.wait.assert_awaited_once()


async def test_installer_cannot_return_outside_cache(installer, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("not the pinned runtime")
    installer.process.communicate.return_value = (str(outside).encode(), b"")
    with pytest.raises(RuntimeError, match="invalid Copilot runtime path"):
        await provisioning.async_runtime_path(installer.hass)
