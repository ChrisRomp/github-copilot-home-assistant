"""Runtime installer isolation, error reporting, and cancellation."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from copilot import _cli_download, _cli_version
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


async def test_installer_only_inherits_execution_and_network_settings(
    installer, monkeypatch
):
    allowed = {
        "PATH": "/configured/bin",
        "PYTHONHOME": "/configured/python",
        "PYTHONPATH": "/configured/modules",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_CTYPE": "C.UTF-8",
        "SystemRoot": "C:\\Windows",
        "WINDIR": "C:\\Windows",
        "TMPDIR": "/configured/scratch",
        "TMP": "/configured/scratch",
        "TEMP": "/configured/scratch",
        "SSL_CERT_FILE": "/configured/ca.pem",
        "SSL_CERT_DIR": "/configured/certs",
        "REQUESTS_CA_BUNDLE": "/configured/requests-ca.pem",
        "CURL_CA_BUNDLE": "/configured/curl-ca.pem",
        "HTTP_PROXY": "http://upper-proxy.invalid:8080",
        "HTTPS_PROXY": "http://upper-proxy.invalid:8443",
        "ALL_PROXY": "http://upper-proxy.invalid:8080",
        "NO_PROXY": "upper.internal",
        "http_proxy": "http://lower-proxy.invalid:8080",
        "https_proxy": "http://lower-proxy.invalid:8443",
        "all_proxy": "http://lower-proxy.invalid:8080",
        "no_proxy": "lower.internal",
        "COPILOT_CLI_DOWNLOAD_BASE_URL": "https://mirror.invalid/releases",
        "COPILOT_SKIP_CLI_DOWNLOAD": "1",
    }
    parent_env = {
        **allowed,
        "HA_ACCESS_TOKEN": "test-ha-token",
        "GITHUB_TOKEN": "test-github-token",
        "DATABASE_PASSWORD": "test-password",
        "UNRELATED_SETTING": "not-needed",
        "COPILOT_CLI_EXTRACT_DIR": "/ignored/cache",
    }
    monkeypatch.setattr(provisioning.os, "environ", parent_env.copy())

    await provisioning.async_runtime_path(installer.hass)

    assert installer.start.call_args.kwargs["env"] == {
        **allowed,
        "COPILOT_CLI_EXTRACT_DIR": str(installer.binary.parent),
    }
    assert os.environ == parent_env


async def test_installer_does_not_add_unconfigured_environment_settings(
    installer, monkeypatch
):
    monkeypatch.setattr(provisioning.os, "environ", {})

    await provisioning.async_runtime_path(installer.hass)

    assert installer.start.call_args.kwargs["env"] == {
        "COPILOT_CLI_EXTRACT_DIR": str(installer.binary.parent)
    }
    assert os.environ == {}


@pytest.mark.parametrize(
    ("sdk_error", "exit_code"),
    [
        (RuntimeError("Checksum mismatch for copilot.tgz"), 4),
        (RuntimeError("CHECKSUM MISMATCH for copilot.tgz"), 4),
        (RuntimeError("Failed to download checksums from SHA256SUMS.txt"), 5),
        (RuntimeError("SHA256SUMS.txt does not contain copilot.tgz."), 1),
        (RuntimeError("Failed to download from GitHub"), 5),
        (RuntimeError("Automatic downloads are disabled."), 1),
        (RuntimeError("Unsafe runtime package path: ../copilot"), 1),
        (RuntimeError("Unexpected SDK failure"), 1),
        (TimeoutError("The read operation timed out"), 5),
        (ConnectionError("Connection failed"), 5),
        (ConnectionResetError("Connection reset by peer"), 5),
        (PermissionError("Permission denied"), 3),
        (OSError("No space left on device"), 3),
    ],
)
def test_install_script_classifies_sdk_failures(
    monkeypatch, capsys, sdk_error, exit_code
):
    monkeypatch.setattr(_cli_version, "get_runtime_platform", Mock(return_value="test"))
    install = Mock(side_effect=sdk_error)
    monkeypatch.setattr(_cli_download, "ensure_runtime_wrapper", install)

    with pytest.raises(SystemExit) as error:
        exec(provisioning._INSTALL_SCRIPT, {})

    assert error.value.code == exit_code
    install.assert_called_once_with()
    assert capsys.readouterr() == ("", "")


def test_install_script_rejects_unsupported_platform_before_install(monkeypatch):
    monkeypatch.setattr(
        _cli_version,
        "get_runtime_platform",
        Mock(side_effect=RuntimeError("Unsupported")),
    )
    install = Mock()
    monkeypatch.setattr(_cli_download, "ensure_runtime_wrapper", install)

    with pytest.raises(SystemExit) as error:
        exec(provisioning._INSTALL_SCRIPT, {})

    assert error.value.code == 2
    install.assert_not_called()


def test_install_script_prints_runtime_path(monkeypatch, capsys):
    monkeypatch.setattr(_cli_version, "get_runtime_platform", Mock(return_value="test"))
    install = Mock(return_value="/configured/cache/copilot-runtime")
    monkeypatch.setattr(_cli_download, "ensure_runtime_wrapper", install)

    exec(provisioning._INSTALL_SCRIPT, {})

    install.assert_called_once_with()
    assert capsys.readouterr() == ("/configured/cache/copilot-runtime\n", "")


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
    assert isinstance(error.value, provisioning.RuntimeDownloadError) is (code == 5)


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
