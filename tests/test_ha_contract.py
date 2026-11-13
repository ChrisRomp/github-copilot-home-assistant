"""Optional contracts against real HA, not the lightweight boundary doubles."""

import asyncio
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
import voluptuous as vol
from copilot.session import CopilotSession
from copilot.session_events import AssistantMessageData, AssistantMessageDeltaData
from copilot.tools import ToolInvocation

pytestmark = pytest.mark.skipif(
    os.environ.get("HA_COPILOT_HA_TESTS") != "1",
    reason="Requires the full pinned Home Assistant dependencies",
)


@pytest.mark.parametrize(
    ("time_zone", "offset_hours"),
    [("Asia/Tokyo", 9), ("Pacific/Honolulu", -10)],
)
async def test_request_time_uses_real_home_assistant_configured_zone(
    tmp_path, time_zone, offset_hours
):
    from homeassistant.components.conversation import ConversationInput
    from homeassistant.components.conversation.chat_log import ChatLog
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.util import dt as dt_util

    from custom_components.github_copilot.backend import ConversationBackend

    previous_zone = dt_util.DEFAULT_TIME_ZONE
    hass = HomeAssistant(str(tmp_path))
    try:
        await hass.config.async_set_time_zone(time_zone)
        user = ConversationInput(
            text="What time is it?",
            context=Context(user_id="user"),
            conversation_id="conversation",
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id="conversation.github_copilot",
        )
        before = dt_util.utcnow()
        result = ConversationBackend._context_tool_result(
            {"section": "request"}, user, ChatLog(hass, "conversation")
        )
        after = dt_util.utcnow()
        current_time = datetime.fromisoformat(result["current_time"])
        assert current_time.utcoffset() == timedelta(hours=offset_hours)
        assert before <= current_time <= after
    finally:
        dt_util.set_default_time_zone(previous_zone)
        await hass.async_stop()


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


@pytest.mark.parametrize("task_mode", [False, True])
async def test_real_live_context_filters_exposed_entities_without_promoting_data(
    tmp_path,
    task_mode,
    monkeypatch,
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
        from homeassistant.helpers import llm

        if hasattr(llm, "LLM_API_HOME_ASSISTANT"):
            hass.config_entries = SimpleNamespace(async_wait_initialized=AsyncMock())
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
        from homeassistant.helpers import llm

        from custom_components.github_copilot.backend import DataTaskRequest

        get_api = AsyncMock(return_value=api)
        if task_mode:
            monkeypatch.setattr(llm, "async_get_api", get_api)

        async def run():
            if task_mode:
                await backend.async_generate_data(
                    user.agent_id,
                    DataTaskRequest(
                        text=user.text, context=user.as_llm_context("github_copilot")
                    ),
                    chat,
                )
            else:
                await backend.async_handle_message(user.agent_id, user, chat)

        await run()
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
        await run()
        assert results[1].result_type == "failure"
        assert "PRIVATE_VALUE" not in results[1].text_result_for_llm
        if task_mode:
            assert get_api.await_args.args[1] == ["assist"]
            assert get_api.await_args.args[2].assistant == "conversation"
            assert not backend._sessions
    finally:
        if backend is not None:
            await backend.async_stop()
        await hass.async_stop()


@pytest.mark.parametrize("device_control", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("question", [False, True])
async def test_real_administrator_tools_read_logs_and_call_actions(
    tmp_path, monkeypatch, device_control, streamed, question
):
    from homeassistant.components import conversation
    from homeassistant.core import Context, HomeAssistant, callback
    from homeassistant.helpers import llm

    if not hasattr(llm, "LLM_API_HOME_ASSISTANT"):
        pytest.skip("The administrator API was introduced in Home Assistant 2026.11")

    from homeassistant.components import llm as llm_integration
    from homeassistant.components.config import llm as config_tools
    from homeassistant.components.history import llm as history_tools
    from homeassistant.components.homeassistant import llm as homeassistant_tools
    from homeassistant.components.llm import AssistAPI, HomeAssistantAPI
    from homeassistant.components.llm import llm as common_tools
    from homeassistant.components.system_log import llm as system_log_tools
    from homeassistant.const import KEY_DATA_LOGGING

    from custom_components.github_copilot.backend import ConversationBackend
    from custom_components.github_copilot.const import DOMAIN
    from custom_components.github_copilot.conversation import (
        GitHubCopilotConversationEntity,
    )
    from custom_components.github_copilot.llm_api import ADMIN_REQUIRED

    hass = HomeAssistant(str(tmp_path))
    user = SimpleNamespace(is_admin=True, name="Test administrator")
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    hass.data["system_log"] = SimpleNamespace(
        records=SimpleNamespace(
            to_list=Mock(
                return_value=[
                    {
                        "name": "example.integration",
                        "message": ["Recorded test warning"],
                        "level": "WARNING",
                        "source": ("example.py", 10),
                        "count": 1,
                        "timestamp": 1730000000,
                        "first_occurred": 1730000000,
                    }
                ]
            )
        )
    )
    hass.data[KEY_DATA_LOGGING] = {}
    hass.data[llm_integration.DATA_PLATFORMS] = SimpleNamespace(
        async_get_platforms=AsyncMock(
            return_value={
                "config": config_tools,
                "history": history_tools,
                "homeassistant": homeassistant_tools,
                "llm": common_tools,
                "system_log": system_log_tools,
            }
        )
    )
    calls = []

    @callback
    def handle_action(call):
        calls.append(call)

    hass.services.async_register("example", "admin_action", handle_action)

    monkeypatch.setattr(
        homeassistant_tools, "async_get_exposed_entities", Mock(return_value={})
    )
    monkeypatch.setattr(
        conversation.ChatLog,
        "_async_expand_prompt_template",
        AsyncMock(return_value=""),
    )
    llm.async_register_api(hass, AssistAPI(hass))
    llm.async_register_api(hass, HomeAssistantAPI(hass))
    session = create_autospec(CopilotSession, instance=True)
    session.session_id = "sdk-session"
    session.on.return_value = Mock()
    session.rpc = SimpleNamespace(options=SimpleNamespace(update=AsyncMock()))
    client = SimpleNamespace(
        create_session=AsyncMock(return_value=session), delete_session=AsyncMock()
    )
    entry = SimpleNamespace(
        entry_id="entry",
        data={"auth_method": "pat"},
        options={
            "model": "gpt-6-luna",
            "enable_device_control": device_control,
            "enable_home_assistant_api": True,
        },
    )
    backend = ConversationBackend(
        hass,
        entry,
        client,
        AsyncMock(),
        AsyncMock(return_value=[SimpleNamespace(id="gpt-6-luna")]),
        access_token_getter=AsyncMock(return_value="test-only"),
    )
    entity = GitHubCopilotConversationEntity(entry)
    entity.hass = hass
    entity.entity_id = "conversation.github_copilot"
    hass.data[DOMAIN] = {"entry": SimpleNamespace(backend=backend)}
    chat = conversation.ChatLog(hass, "conversation")
    deltas = []
    chat.delta_listener = lambda _chat, delta: deltas.append(delta)
    user_input = conversation.ConversationInput(
        text="Read the log and perform the requested action.",
        context=Context(user_id="admin"),
        conversation_id="conversation",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id=entity.entity_id,
    )
    chat.async_add_user_content(conversation.UserContent(content=user_input.text))
    tool_results = []

    async def send(*args, **kwargs):
        tools = {tool.name: tool for tool in session.set_tools.await_args.args[0]}
        reply = "Done."
        if user.is_admin:
            assert {
                "ha__config__reload_config_entry",
                "ha__history__get_history",
                "ha__homeassistant__call_action",
                "ha__homeassistant__render_template",
                "ha__system_log__get_entries",
                "ha__system_log__get_logs",
            } <= tools.keys()
            for name, arguments in (
                ("ha__system_log__get_entries", {"logger": "example"}),
                ("ha__homeassistant__call_action", {"action": "example.admin_action"}),
            ):
                tool_results.append(
                    await tools[name].handler(
                        ToolInvocation(
                            tool_name=name, tool_call_id=name, arguments=arguments
                        )
                    )
                )
        else:
            assert "ha__system_log__get_entries" not in tools
            assert "ha__homeassistant__call_action" not in tools
            if question:
                reply = "Which room?"
            if streamed:
                session.on.call_args.args[0](
                    SimpleNamespace(
                        agent_id=None,
                        data=AssistantMessageDeltaData(
                            delta_content=reply, message_id="reply"
                        ),
                    )
                )
                await asyncio.sleep(0)
        return SimpleNamespace(
            data=AssistantMessageData(content=reply, message_id="reply")
        )

    session.send_and_wait.side_effect = send
    try:
        result = await entity._async_handle_message(user_input, chat)
        assert result.response.speech["plain"]["speech"] == "Done."
        assert [item.result_type for item in tool_results] == ["success", "failure"]
        assert "Recorded test warning" in tool_results[0].text_result_for_llm
        assert "autonomous execution" in tool_results[1].text_result_for_llm
        assert not calls

        user.is_admin = False
        deltas.clear()
        chat.async_add_user_content(conversation.UserContent(content="Hello again."))
        result = await entity._async_handle_message(user_input, chat)
        expected = f"{'Which room?' if question else 'Done.'}\n\n{ADMIN_REQUIRED}"
        assert result.response.speech["plain"]["speech"] == expected
        assert result.continue_conversation is question
        assert "".join(delta.get("content") or "" for delta in deltas) == expected
        assert chat.content[-1].content == expected
        assert not calls
        assert client.create_session.await_count == 2
        assert (chat.llm_api is not None) is device_control
    finally:
        await backend.async_stop()
        await hass.async_stop()
