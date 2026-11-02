"""Tests for the Copilot runtime's credential-provider boundary."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from copilot._jsonrpc import JsonRpcError
from copilot.client import GitHubTokenProviderArgs
from copilot.generated.rpc import GitHubTokenAcquireReason

from custom_components.github_copilot.auth import OAuthError, ReauthenticationRequired
from custom_components.github_copilot.runtime import CopilotRuntime


@pytest.fixture
def oauth_runtime(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 10000)
    runtime = CopilotRuntime.__new__(CopilotRuntime)
    runtime.entry = SimpleNamespace(data={"access_token_expires_at": 10000 + 28800})
    runtime.credentials = SimpleNamespace(
        async_get_access_token=AsyncMock(return_value="oauth_access_token")
    )
    return runtime


@pytest.mark.parametrize(
    "host", ["github.com", "https://github.com", "https://github.com/"]
)
@pytest.mark.parametrize("reason", list(GitHubTokenAcquireReason))
async def test_token_provider_accepts_sdk_github_com_host(oauth_runtime, host, reason):
    result = await oauth_runtime.async_github_token_provider(
        GitHubTokenProviderArgs(host=host, session_id="session", reason=reason)
    )
    assert result == {
        "kind": "token",
        "accessToken": "oauth_access_token",
        "expiresIn": 28800,
    }


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "https://github.com.attacker.invalid",
        "https://github.com@attacker.invalid",
        "http://github.com",
        "https://github.com:8443",
        "https://github.com/path",
        "",
    ],
)
async def test_token_provider_refuses_other_authorities_without_reading_credentials(
    oauth_runtime, host, caplog
):
    result = await oauth_runtime.async_github_token_provider(
        GitHubTokenProviderArgs(
            host=host, session_id=None, reason=GitHubTokenAcquireReason.INITIAL
        )
    )
    assert result == {"kind": "cancelled"}
    oauth_runtime.credentials.async_get_access_token.assert_not_awaited()
    assert "Refusing credentials" in caplog.text


@pytest.mark.parametrize("seconds_remaining", [-1, 0, 60, 3600, 3601])
async def test_token_provider_enforces_sdk_minimum_remaining_lifetime(
    oauth_runtime, seconds_remaining
):
    oauth_runtime.entry.data["access_token_expires_at"] = 10000 + seconds_remaining
    request = GitHubTokenProviderArgs(
        host="https://github.com",
        session_id=None,
        reason=GitHubTokenAcquireReason.REFRESH,
    )
    if seconds_remaining <= 3600:
        with pytest.raises(OAuthError, match="expires too soon"):
            await oauth_runtime.async_github_token_provider(request)
    else:
        result = await oauth_runtime.async_github_token_provider(request)
        assert result["expiresIn"] == seconds_remaining


async def test_token_provider_does_not_invent_oauth_expiry(oauth_runtime):
    oauth_runtime.entry.data.clear()
    with pytest.raises(ReauthenticationRequired, match="no token expiry"):
        await oauth_runtime.async_github_token_provider(
            GitHubTokenProviderArgs(
                host="https://github.com",
                session_id=None,
                reason=GitHubTokenAcquireReason.INITIAL,
            )
        )


def _runtime_for_models() -> CopilotRuntime:
    import asyncio

    runtime = CopilotRuntime.__new__(CopilotRuntime)
    runtime.entry = SimpleNamespace(
        entry_id="entry", data={}, options={"model": "model-1"}
    )
    runtime.hass = SimpleNamespace()
    runtime.backend = None
    runtime._models = None
    runtime._models_fetched_at = 0.0
    runtime._models_lock = asyncio.Lock()

    async def get_access_token() -> str:
        return "token"

    runtime.credentials = SimpleNamespace(async_get_access_token=get_access_token)
    return runtime


@pytest.mark.asyncio
async def test_model_list_is_cached_until_forced() -> None:
    runtime = _runtime_for_models()
    calls = 0

    async def list_rpc(token: str) -> list[SimpleNamespace]:
        nonlocal calls
        calls += 1
        return [SimpleNamespace(id=f"model-{calls}")]

    runtime._async_list_models_rpc = list_rpc

    assert [m.id for m in await runtime.async_list_models()] == ["model-1"]
    assert [m.id for m in await runtime.async_list_models()] == ["model-1"]
    forced = await runtime.async_list_models(force_refresh=True)
    assert [m.id for m in forced] == ["model-2"]
    assert calls == 2


@pytest.mark.asyncio
async def test_model_list_falls_back_to_temporary_client(monkeypatch) -> None:
    from custom_components.github_copilot import runtime as runtime_module

    runtime = _runtime_for_models()

    async def failing_rpc(token: str) -> list[SimpleNamespace]:
        raise RuntimeError("experimental RPC unavailable")

    async def probe(token: str, *, flow_id: str, hass) -> list[SimpleNamespace]:
        assert token == "token"
        return [SimpleNamespace(id="probed-model")]

    runtime._async_list_models_rpc = failing_rpc
    monkeypatch.setattr(runtime_module, "async_probe_models", probe)

    models = await runtime.async_list_models()
    assert [model.id for model in models] == ["probed-model"]


@pytest.mark.asyncio
async def test_model_discovery_quota_is_reported_without_temporary_client_retry(
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from custom_components.github_copilot import runtime as runtime_module

    runtime = _runtime_for_models()
    runtime.backend = SimpleNamespace(report_quota_exceeded=Mock())
    runtime._async_list_models_rpc = AsyncMock(
        side_effect=RuntimeError("Quota exceeded")
    )
    probe = AsyncMock()
    monkeypatch.setattr(runtime_module, "async_probe_models", probe)
    with pytest.raises(RuntimeError, match="quota exceeded"):
        await runtime.async_list_models()
    runtime.backend.report_quota_exceeded.assert_called_once()
    probe.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("HTTP 401 Unauthorized"),
        JsonRpcError(-32603, "Request rejected", data={"statusCode": 401}),
        JsonRpcError(
            -32603, "Request rejected", data={"error": {"code": "invalid_token"}}
        ),
    ],
)
async def test_running_discovery_rejected_pat_requires_reauth_without_retry(
    monkeypatch, error
):
    from custom_components.github_copilot import runtime as runtime_module

    runtime = _runtime_for_models()
    runtime.entry.data = {"auth_method": "pat", "access_token": "revoked-pat"}
    runtime._async_list_models_rpc = AsyncMock(side_effect=error)
    probe = AsyncMock()
    monkeypatch.setattr(runtime_module, "async_probe_models", probe)
    with pytest.raises(ReauthenticationRequired):
        await runtime.async_list_models()
    probe.assert_not_awaited()
    assert runtime._models is None
    assert runtime.entry.options["model"] == "model-1"


@pytest.fixture
def sdk_runtime(ha_boundary, monkeypatch):
    import copilot

    module = ha_boundary.load("runtime")
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=Mock(return_value=str(Path.cwd()))),
    )
    entry = SimpleNamespace(entry_id="entry", data={}, options={})
    runtime = module.CopilotRuntime(hass, entry)
    runtime.base_directory = Mock()
    runtime.credentials.async_ensure_account_id = AsyncMock(return_value="123")
    client = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        force_stop=AsyncMock(),
        list_models=AsyncMock(return_value=[SimpleNamespace(id="model")]),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(copilot, "CopilotClient", factory)
    monkeypatch.setattr(
        module, "async_runtime_path", AsyncMock(return_value="/unused-runtime")
    )
    backend = SimpleNamespace(async_stop=AsyncMock())
    monkeypatch.setitem(
        sys.modules,
        "custom_components.github_copilot.backend",
        SimpleNamespace(ConversationBackend=Mock(return_value=backend)),
    )
    directory = SimpleNamespace(name=str(Path.cwd()), cleanup=Mock())
    monkeypatch.setattr(
        module.tempfile, "TemporaryDirectory", Mock(return_value=directory)
    )
    return SimpleNamespace(
        module=module,
        runtime=runtime,
        client=client,
        factory=factory,
        backend=backend,
        directory=directory,
    )


async def test_runtime_disables_native_expiry_so_backend_owns_session_lifetime(
    sdk_runtime,
):
    await sdk_runtime.runtime.async_start()
    assert sdk_runtime.factory.call_args.kwargs["session_idle_timeout_seconds"] == 0
    assert sdk_runtime.runtime.is_started
    await sdk_runtime.runtime.async_stop()


@pytest.mark.parametrize("failed_stage", ["start", "list_models"])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("HTTP 401 Unauthorized"), ReauthenticationRequired),
        (
            JsonRpcError(
                -32603, "Request rejected", data={"error": {"code": "bad_credentials"}}
            ),
            ReauthenticationRequired,
        ),
        (RuntimeError("HTTP 403 Forbidden: model policy"), RuntimeError),
        (RuntimeError("HTTP 429 Too many requests"), RuntimeError),
        (RuntimeError("Quota exceeded"), RuntimeError),
        (RuntimeError("Model not available"), RuntimeError),
    ],
)
async def test_probe_classifies_only_confirmed_auth_rejections_and_cleans_up(
    sdk_runtime, failed_stage, error, expected
):
    getattr(sdk_runtime.client, failed_stage).side_effect = error
    with pytest.raises(expected):
        await sdk_runtime.module.async_probe_models(
            "test-pat", flow_id="flow", hass=sdk_runtime.runtime.hass
        )
    sdk_runtime.client.stop.assert_awaited_once()
    sdk_runtime.directory.cleanup.assert_called_once()


async def test_runtime_stop_drains_backend_before_stopping_client_and_is_idempotent(
    sdk_runtime,
):
    runtime = sdk_runtime.runtime
    await runtime.async_start()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def stop_backend():
        entered.set()
        await finish.wait()

    sdk_runtime.backend.async_stop.side_effect = stop_backend
    first = asyncio.create_task(runtime.async_stop())
    await entered.wait()
    second = asyncio.create_task(runtime.async_stop())
    await asyncio.sleep(0)
    assert not second.done()
    sdk_runtime.client.stop.assert_not_awaited()
    finish.set()
    await asyncio.gather(first, second)
    await runtime.async_stop()
    sdk_runtime.backend.async_stop.assert_awaited_once()
    sdk_runtime.client.stop.assert_awaited_once()
    assert runtime.backend is runtime.client is None
    assert not runtime.is_started


@pytest.mark.parametrize("failed_stage", ["backend", "client"])
async def test_runtime_stop_clears_references_even_if_cleanup_fails(
    sdk_runtime, failed_stage
):
    runtime = sdk_runtime.runtime
    await runtime.async_start()
    if failed_stage == "backend":
        sdk_runtime.backend.async_stop.side_effect = RuntimeError("Cleanup failed")
    else:
        sdk_runtime.client.stop.side_effect = RuntimeError("Cleanup failed")
    with pytest.raises(RuntimeError, match="Cleanup failed"):
        await runtime.async_stop()
    await runtime.async_stop()
    sdk_runtime.backend.async_stop.assert_awaited_once()
    sdk_runtime.client.stop.assert_awaited_once()
    if failed_stage == "client":
        sdk_runtime.client.force_stop.assert_awaited_once()
    else:
        sdk_runtime.client.force_stop.assert_not_awaited()
    assert runtime.backend is runtime.client is None
    assert not runtime.is_started


async def test_runtime_stop_retains_client_for_retry_if_force_stop_also_fails(
    sdk_runtime,
):
    runtime = sdk_runtime.runtime
    await runtime.async_start()
    sdk_runtime.client.stop.side_effect = RuntimeError("Cleanup failed")
    sdk_runtime.client.force_stop.side_effect = OSError("Process could not exit")
    with pytest.raises(OSError, match="Process could not exit"):
        await runtime.async_stop()
    assert runtime.client is sdk_runtime.client
    sdk_runtime.client.stop.side_effect = None
    await runtime.async_stop()
    assert runtime.client is None
    sdk_runtime.backend.async_stop.assert_awaited_once()
    assert sdk_runtime.client.stop.await_count == 2


async def test_runtime_stop_force_stops_client_if_graceful_stop_is_cancelled(
    sdk_runtime,
):
    runtime = sdk_runtime.runtime
    await runtime.async_start()
    sdk_runtime.client.stop.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await runtime.async_stop()
    sdk_runtime.client.force_stop.assert_awaited_once()
    assert runtime.client is None
    await runtime.async_stop()
    sdk_runtime.client.stop.assert_awaited_once()
