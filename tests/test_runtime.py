"""Tests for the Copilot runtime's credential-provider boundary."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
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
