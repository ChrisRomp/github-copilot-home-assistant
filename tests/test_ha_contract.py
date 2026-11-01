"""Optional contracts against real HA, not the lightweight boundary doubles."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
import voluptuous as vol
from copilot.session import CopilotSession
from copilot.session_events import AssistantMessageData
from copilot.tools import ToolInvocation

pytestmark = pytest.mark.skipif(
    os.environ.get("HA_COPILOT_HA_TESTS") != "1",
    reason="Requires the full pinned Home Assistant dependencies",
)


@pytest.mark.parametrize("tool_failed", [False, True])
async def test_real_chat_log_records_external_tool_once_and_returns_speech(
    tmp_path, tool_failed
):
    from homeassistant.components.conversation import (
        ConversationInput,
        async_get_result_from_chat_log,
    )
    from homeassistant.components.conversation.chat_log import (
        ChatLog,
        ToolResultContent,
        UserContent,
    )
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.helpers.llm import ToolResult, selector_serializer

    from custom_components.github_copilot.backend import ConversationBackend

    hass = HomeAssistant(str(tmp_path))
    session = create_autospec(CopilotSession, instance=True)
    session.on.return_value = Mock()
    session.rpc = SimpleNamespace(options=SimpleNamespace(update=AsyncMock()))
    backend = ConversationBackend(
        hass,
        SimpleNamespace(
            entry_id="entry",
            data={"auth_method": "pat"},
            options={"model": "gpt-6-luna", "enable_device_control": True},
        ),
        SimpleNamespace(create_session=AsyncMock(return_value=session)),
        AsyncMock(),
        AsyncMock(return_value=[SimpleNamespace(id="gpt-6-luna")]),
        access_token_getter=AsyncMock(return_value="test-only"),
    )
    api = SimpleNamespace(
        api_prompt="Use only exposed tools.",
        custom_serializer=selector_serializer,
        tools=[
            SimpleNamespace(
                name="assist_satellite__HassBroadcast",
                description="Broadcast",
                parameters=vol.Schema({vol.Required("message"): str}),
            )
        ],
        async_call_tool=AsyncMock(
            return_value=ToolResult(
                data={"success": not tool_failed}, error=tool_failed
            )
        ),
    )
    chat = ChatLog(hass, "conversation", llm_api=api)
    chat.async_add_user_content(UserContent(content="Test broadcast"))
    user = ConversationInput(
        text="Test broadcast",
        context=Context(user_id="user"),
        conversation_id="conversation",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="conversation.github_copilot",
    )

    async def send(*args, **kwargs):
        tool = session.set_tools.await_args.args[0][0]
        result = await tool.handler(
            ToolInvocation(
                tool_name=tool.name,
                tool_call_id="call",
                arguments={"message": "test"},
            )
        )
        assert result.result_type == ("failure" if tool_failed else "success")
        return SimpleNamespace(
            data=AssistantMessageData(content="Done.", message_id="reply")
        )

    session.send_and_wait.side_effect = send
    try:
        await backend.async_handle_message(user.agent_id, user, chat)
        api.async_call_tool.assert_awaited_once()
        assert api.async_call_tool.await_args.args[0].external is True
        assert (
            len(
                [
                    content
                    for content in chat.content
                    if isinstance(content, ToolResultContent)
                ]
            )
            == 1
        )
        result = async_get_result_from_chat_log(user, chat)
        assert result.response.speech["plain"]["speech"] == "Done."
    finally:
        await backend.async_stop()
        await hass.async_stop()
