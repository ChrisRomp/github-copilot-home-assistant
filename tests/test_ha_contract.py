"""Optional contracts against real HA, not the lightweight boundary doubles."""

import json
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
@pytest.mark.parametrize("native_intent", [False, True])
async def test_real_chat_log_records_external_tool_once_and_returns_speech(
    tmp_path, tool_failed, native_intent
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
    from homeassistant.helpers.intent import (
        IntentResponse,
        IntentResponseTarget,
        IntentResponseTargetType,
    )
    from homeassistant.helpers.llm import (
        IntentResponseDict,
        ToolResult,
        selector_serializer,
    )

    from custom_components.github_copilot.backend import ConversationBackend

    hass = HomeAssistant(str(tmp_path))
    session = create_autospec(CopilotSession, instance=True)
    session.session_id = "sdk-session"
    session.on.return_value = Mock()
    session.rpc = SimpleNamespace(options=SimpleNamespace(update=AsyncMock()))
    backend = ConversationBackend(
        hass,
        SimpleNamespace(
            entry_id="entry",
            data={"auth_method": "pat"},
            options={"model": "gpt-6-luna", "enable_device_control": True},
        ),
        SimpleNamespace(
            create_session=AsyncMock(return_value=session), delete_session=AsyncMock()
        ),
        AsyncMock(),
        AsyncMock(return_value=[SimpleNamespace(id="gpt-6-luna")]),
        access_token_getter=AsyncMock(return_value="test-only"),
    )
    intent_response = IntentResponse(language="en")
    intent_response.async_set_results(
        [
            IntentResponseTarget(
                "Kitchen", IntentResponseTargetType.ENTITY, "light.kitchen"
            )
        ],
        [
            IntentResponseTarget(
                "Office", IntentResponseTargetType.ENTITY, "light.office"
            )
        ],
    )
    data = (
        IntentResponseDict(intent_response)
        if native_intent
        else {"success": not tool_failed}
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
            return_value=ToolResult(data=data, error=tool_failed)
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
        if native_intent:
            assert result.response is intent_response
            assert result.response.success_results[0].id == "light.kitchen"
            assert result.response.failed_results[0].id == "light.office"
            recorded = next(
                content
                for content in chat.content
                if isinstance(content, ToolResultContent)
            )
            assert recorded.result.data is data
    finally:
        await backend.async_stop()
        await hass.async_stop()


async def test_real_live_context_filters_exposed_entities_without_promoting_data(
    tmp_path,
):
    from homeassistant.components.conversation import ConversationInput
    from homeassistant.components.conversation.chat_log import ChatLog
    from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
    from homeassistant.components.homeassistant.exposed_entities import (
        ExposedEntities,
        async_expose_entity,
    )
    from homeassistant.components.homeassistant.llm import GetLiveContextTool
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.llm import APIInstance, selector_serializer

    from custom_components.github_copilot.backend import ConversationBackend

    hass = HomeAssistant(str(tmp_path))
    backend = None
    try:
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        await ar.async_load(hass, load_empty=True)
        exposure = hass.data[DATA_EXPOSED_ENTITIES] = ExposedEntities(hass)
        await exposure.async_initialize()
        hass.states.async_set(
            "sensor.kitchen_temperature",
            "21",
            {
                "friendly_name": "Kitchen temperature",
                "unit_of_measurement": "C",
            },
        )
        hass.states.async_set(
            "sensor.private",
            "PRIVATE_VALUE",
            {
                "friendly_name": "Private sensor",
            },
        )
        hass.states.async_set(
            "media_player.living_room",
            "playing",
            {
                "friendly_name": "Living room",
                "media_title": "MEDIA_TITLE_INJECTION",
            },
        )
        for entity_id, exposed in (
            ("sensor.kitchen_temperature", True),
            ("sensor.private", False),
            ("media_player.living_room", True),
        ):
            async_expose_entity(hass, "conversation", entity_id, exposed)

        user = ConversationInput(
            text="What is the kitchen temperature?",
            context=Context(user_id="user"),
            conversation_id="conversation",
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id="conversation.github_copilot",
        )
        api = APIInstance(
            api=SimpleNamespace(hass=hass, id="assist"),
            api_prompt="Static catalog: DO_NOT_ELEVATE_THIS_CONTEXT",
            llm_context=user.as_llm_context("github_copilot"),
            tools=[GetLiveContextTool()],
            custom_serializer=selector_serializer,
        )
        session = create_autospec(CopilotSession, instance=True)
        session.session_id = "sdk-session"
        session.on.return_value = Mock()
        session.rpc = SimpleNamespace(options=SimpleNamespace(update=AsyncMock()))
        client = SimpleNamespace(
            create_session=AsyncMock(return_value=session), delete_session=AsyncMock()
        )
        backend = ConversationBackend(
            hass,
            SimpleNamespace(
                entry_id="entry",
                data={"auth_method": "pat"},
                options={"model": "gpt-6-luna", "enable_device_control": True},
            ),
            client,
            AsyncMock(),
            AsyncMock(return_value=[SimpleNamespace(id="gpt-6-luna")]),
            access_token_getter=AsyncMock(return_value="test-only"),
        )
        chat = ChatLog(hass, "conversation", llm_api=api)
        results = []
        filters = {"name": "Kitchen temperature", "domain": "sensor"}

        async def send(*args, **kwargs):
            tool = next(
                tool
                for tool in session.set_tools.await_args.args[0]
                if tool.name == "ha__homeassistant__GetLiveContext"
            )
            results.append(
                await tool.handler(
                    ToolInvocation(
                        tool_name=tool.name,
                        tool_call_id=f"call-{len(results)}",
                        arguments=filters,
                    )
                )
            )
            return SimpleNamespace(
                data=AssistantMessageData(content="Done.", message_id="reply")
            )

        session.send_and_wait.side_effect = send
        await backend.async_handle_message(user.agent_id, user, chat)
        assert results[0].result_type == "success"
        assert "Kitchen temperature" in results[0].text_result_for_llm
        assert "21" in results[0].text_result_for_llm
        assert "PRIVATE_VALUE" not in results[0].text_result_for_llm
        assert "MEDIA_TITLE_INJECTION" not in results[0].text_result_for_llm
        assert (
            client.create_session.await_args.kwargs["organization_custom_instructions"]
            == ""
        )
        assert "DO_NOT_ELEVATE_THIS_CONTEXT" not in json.dumps(
            client.create_session.await_args.kwargs["system_message"]
        )
        filters = {"name": "Private sensor", "domain": "sensor"}
        await backend.async_handle_message(user.agent_id, user, chat)
        assert results[1].result_type == "failure"
        assert "PRIVATE_VALUE" not in results[1].text_result_for_llm
    finally:
        if backend is not None:
            await backend.async_stop()
        await hass.async_stop()
