"""Optional native SDK check with a loopback-only fake model, no GitHub token."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from copilot import CopilotClient
from copilot._cli_download import get_cache_dir
from copilot._cli_version import CLI_VERSION, get_runtime_platform
from copilot.client import RuntimeConnection
from copilot.generated.rpc import SessionUpdateOptionsParams

pytestmark = pytest.mark.skipif(
    os.environ.get("HA_COPILOT_NATIVE_TESTS") != "1",
    reason="Opt in to the cached native runtime contract test",
)


async def test_native_system_context_updates_without_accumulating_history(
    ha_boundary, tmp_path
):
    path = (
        get_cache_dir(CLI_VERSION)
        / "prebuilds"
        / get_runtime_platform()
        / "copilot-runtime"
    )
    if not path.is_file():
        pytest.skip("Native SDK runtime is not already cached")
    requests = []

    async def complete(request):
        payload = await request.json()
        requests.append(payload)
        events = [
            {
                "id": "test",
                "object": "chat.completion.chunk",
                "model": "gpt-4.1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "test",
                "object": "chat.completion.chunk",
                "model": "gpt-4.1",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return web.Response(
            text=body + "data: [DONE]\n\n", content_type="text/event-stream"
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(path=str(path)),
        use_logged_in_user=False,
        base_directory=str(tmp_path / "runtime"),
        mode="empty",
        log_level="error",
    )
    module = ha_boundary.load("backend")

    async def create_session(**kwargs):
        kwargs.pop("github_token")
        kwargs.pop("github_token_provider")
        kwargs["provider"] = {
            "type": "openai",
            "wire_api": "completions",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key": "local-test-not-a-credential",
        }
        return await client.create_session(**kwargs)

    backend = module.ConversationBackend(
        SimpleNamespace(),
        SimpleNamespace(entry_id="entry", data={}, options={"model": "gpt-4.1"}),
        SimpleNamespace(create_session=create_session),
        AsyncMock(),
        AsyncMock(return_value=[SimpleNamespace(id="gpt-4.1")]),
        access_token_getter=AsyncMock(return_value="not-used"),
    )
    context = {"instructions": "Current test state: STATE_OFF"}
    try:
        await client.start()
        session = await backend._async_create_sdk_session("gpt-4.1", [], context)
        first = await session.send_and_wait("First message", timeout=30)
        context["instructions"] = "Current test state: STATE_ON"
        await session.rpc.options.update(
            SessionUpdateOptionsParams(
                organization_custom_instructions=context["instructions"]
            )
        )
        second = await session.send_and_wait("Second message", timeout=30)
        assert first.data.content == second.data.content == "Hello"
        assert len(requests) == 2
        first_messages = requests[0]["messages"]
        second_messages = requests[1]["messages"]
        assert "STATE_OFF" in json.dumps(first_messages)
        assert "STATE_ON" in json.dumps(second_messages)
        assert "STATE_OFF" not in json.dumps(second_messages)
        for request in requests:
            assert all(
                "Current test state" not in json.dumps(message)
                for message in request["messages"]
                if message["role"] == "user"
            )
        await session.disconnect()
    finally:
        await client.stop()
        await runner.cleanup()
