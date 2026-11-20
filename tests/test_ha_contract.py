"""Optional contracts against real HA, not the lightweight boundary doubles."""

import asyncio
import inspect
import json
import logging
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


def test_real_ha_satellite_and_recorder_history_contract():
    from homeassistant.components.conversation import ConversationInput
    from homeassistant.components.recorder.history import state_changes_during_period
    from homeassistant.core import Context

    user = ConversationInput(
        text="What was the temperature?",
        context=Context(),
        conversation_id="satellite-contract",
        device_id=None,
        satellite_id="assist_satellite.bedroom",
        language="en",
        agent_id="conversation.test",
    )
    assert user.context.user_id is None
    assert user.satellite_id == "assist_satellite.bedroom"

    parameters = inspect.signature(state_changes_during_period).parameters
    assert {
        "entity_id",
        "descending",
        "limit",
        "include_start_time_state",
    } <= parameters.keys()
    assert not inspect.iscoroutinefunction(state_changes_during_period)


async def test_real_history_uses_conversation_exposure_without_other_assistant_entries(
    tmp_path,
):
    from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
    from homeassistant.components.homeassistant.exposed_entities import (
        ExposedEntities,
        async_expose_entity,
    )
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.github_copilot.history import _eligible_states

    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = SimpleNamespace(async_wait_initialized=AsyncMock())
    try:
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        await ar.async_load(hass, load_empty=True)
        exposure = hass.data[DATA_EXPOSED_ENTITIES] = ExposedEntities(hass)
        await exposure.async_initialize()
        hass.states.async_set("sensor.allowed_temperature", "21")
        hass.states.async_set("sensor.hidden_temperature", "18")
        async_expose_entity(hass, "conversation", "sensor.allowed_temperature", True)
        async_expose_entity(hass, "conversation", "sensor.hidden_temperature", False)

        assert set(_eligible_states(hass)) == {"sensor.allowed_temperature"}
        allowed = exposure.entities["sensor.allowed_temperature"]
        hidden = exposure.entities["sensor.hidden_temperature"]
        assert "conversation" in allowed.assistants
        assert "conversation" in hidden.assistants
        assert "github_copilot" not in allowed.assistants
        assert "github_copilot" not in hidden.assistants
    finally:
        await hass.async_stop()


async def test_real_recorder_point_lookup_uses_one_bounded_query_for_dense_history(
    tmp_path,
):
    from homeassistant import loader
    from homeassistant.components import recorder
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.recorder import async_initialize_recorder
    from homeassistant.setup import async_setup_component

    from custom_components.github_copilot.const import HISTORY_MAX_ROWS_PER_TURN
    from custom_components.github_copilot.history import (
        HistoryTurnBudget,
        _async_find_state_at_or_before,
    )

    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = SimpleNamespace(
        async_wait_initialized=AsyncMock(),
        async_entries=Mock(return_value=[]),
    )
    hass.config.safe_mode = True
    loader.async_setup(hass)
    async_initialize_recorder(hass)
    try:
        await hass.async_start()
        assert await async_setup_component(
            hass,
            "recorder",
            {"recorder": {"db_url": f"sqlite:///{tmp_path / 'history.db'}"}},
        )
        entity_id = "sensor.dense_temperature"
        hass.states.async_set(entity_id, "0")
        first = hass.states.get(entity_id)
        assert first is not None
        for index in range(1, 150):
            await asyncio.sleep(0.001)
            hass.states.async_set(entity_id, str(index))
            if index == 75:
                middle = hass.states.get(entity_id)
        await recorder.get_instance(hass).async_block_till_done()
        current = hass.states.get(entity_id)
        assert current is not None
        slots = asyncio.Semaphore(2)

        for point, expected, max_queries in (
            (current.last_updated, current, 2),
            (middle.last_updated + timedelta(microseconds=500), middle, 1),
        ):
            budget = HistoryTurnBudget()
            status, state = await _async_find_state_at_or_before(
                hass, slots, entity_id, point, budget
            )
            assert status == "found"
            assert state.state == expected.state
            assert state.last_updated == expected.last_updated
            assert budget.queries <= max_queries
            assert budget.rows <= HISTORY_MAX_ROWS_PER_TURN

        budget = HistoryTurnBudget()
        status, state = await _async_find_state_at_or_before(
            hass, slots, entity_id, first.last_updated - timedelta(seconds=1), budget
        )
        assert (status, state) == ("absent", None)
    finally:
        await hass.async_stop()


async def test_real_discovery_uses_device_area_for_exposed_entities(tmp_path):
    from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
    from homeassistant.components.homeassistant.exposed_entities import (
        ExposedEntities,
        async_expose_entity,
    )
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.github_copilot.history import (
        HistoryTurnBudget,
        async_find_entities,
    )

    hass = HomeAssistant(str(tmp_path))
    entry = Mock(
        spec=ConfigEntry,
        entry_id="device-entry",
        domain="test",
        title="Test",
        disabled_by=None,
        subentries={},
    )
    hass.config_entries = SimpleNamespace(
        async_wait_initialized=AsyncMock(),
        async_get_entry=Mock(return_value=entry),
    )
    try:
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        await ar.async_load(hass, load_empty=True)
        exposure = hass.data[DATA_EXPOSED_ENTITIES] = ExposedEntities(hass)
        await exposure.async_initialize()
        area = ar.async_get(hass).async_create("Guest Bedroom")
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("test", "thermostat")},
        )
        dr.async_get(hass).async_update_device(device.id, area_id=area.id)
        registry_entry = er.async_get(hass).async_get_or_create(
            "sensor", "test", "thermostat_temperature", device_id=device.id
        )
        assert registry_entry.area_id is None
        hidden = er.async_get(hass).async_get_or_create(
            "sensor", "test", "thermostat_hidden", device_id=device.id
        )
        hass.states.async_set(registry_entry.entity_id, "21")
        hass.states.async_set(hidden.entity_id, "18")
        async_expose_entity(hass, "conversation", registry_entry.entity_id, True)
        async_expose_entity(hass, "conversation", hidden.entity_id, False)

        result = async_find_entities(
            hass, {"query": "guest bedroom", "domain": "sensor"}, HistoryTurnBudget()
        )

        assert result["truncated"] is False
        assert [
            (entity["entity_id"], entity["area"]) for entity in result["entities"]
        ] == [(registry_entry.entity_id, "Guest Bedroom")]
    finally:
        await hass.async_stop()


async def test_real_chat_memory_keeps_user_input_and_provenance_unchanged(tmp_path):
    from homeassistant.components.conversation import ConversationInput
    from homeassistant.components.conversation.chat_log import (
        ChatLog,
        ToolResultContent,
        UserContent,
    )
    from homeassistant.core import Context, HomeAssistant

    from custom_components.github_copilot.backend import ConversationBackend
    from custom_components.github_copilot.memory import MemoryManager

    hass = HomeAssistant(str(tmp_path))
    manager = MemoryManager(hass, "entry")
    session = create_autospec(CopilotSession, instance=True)
    session.session_id = "sdk-memory"
    session.on.return_value = Mock()
    session.rpc = SimpleNamespace(options=SimpleNamespace(update=AsyncMock()))
    backend = ConversationBackend(
        hass,
        SimpleNamespace(
            entry_id="entry",
            data={},
            options={
                "model": "gpt-4.1",
                "enable_shared_memory": True,
            },
        ),
        SimpleNamespace(
            create_session=AsyncMock(return_value=session), delete_session=AsyncMock()
        ),
        AsyncMock(),
        AsyncMock(return_value=[SimpleNamespace(id="gpt-4.1")]),
        access_token_getter=AsyncMock(return_value="not-used"),
        memory=manager,
    )
    text = "We prefer warm reading lights."
    chat = ChatLog(hass, "memory-chat")
    chat.async_add_user_content(UserContent(content=text))
    user = ConversationInput(
        text=text,
        context=Context(),
        conversation_id="memory-chat",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="conversation.test",
    )

    async def send(prompt, **kwargs):
        assert prompt.endswith("Actual user request:\n" + text)
        assert kwargs["display_prompt"] == text
        tools = session.set_tools.await_args.args[0]
        assert not any(tool.name.startswith("ha__") for tool in tools)
        tool = next(t for t in tools if t.name == "ha_memory_save")
        result = await tool.handler(
            ToolInvocation(
                tool_name=tool.name,
                tool_call_id="save-fact",
                arguments={
                    "text": "Warm reading lights",
                    "source": "warm reading lights",
                    "expected_revision": 0,
                },
            )
        )
        assert result.result_type == "success"
        return SimpleNamespace(
            data=AssistantMessageData(content="Saved.", message_id="m")
        )

    session.send_and_wait.side_effect = send
    try:
        await backend.async_handle_message(user.agent_id, user, chat)
        assert user.text == text
        assert [c.content for c in chat.content if isinstance(c, UserContent)] == [text]
        results = [c for c in chat.content if isinstance(c, ToolResultContent)]
        assert len(results) == 1
        assert results[0].result.data["snapshot"]["count"] == 1
        from pathlib import Path

        stored = json.loads(Path(manager.store.path).read_text())
        assert stored["data"]["facts"][0]["source"] == "warm reading lights"
    finally:
        await backend.async_stop()
        await manager.async_close()
        await hass.async_stop()


@pytest.mark.parametrize(
    ("failure", "has_storage"), [("unlink", True), ("stop", True), ("stop", False)]
)
async def test_real_entry_removal_reports_orphaned_memory(
    tmp_path, monkeypatch, caplog, failure, has_storage
):
    from pathlib import Path
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigEntries, ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.storage import Store

    from custom_components import github_copilot
    from custom_components.github_copilot import runtime as runtime_module
    from custom_components.github_copilot.const import DOMAIN
    from custom_components.github_copilot.memory import MemoryManager

    hass = HomeAssistant(str(tmp_path))
    try:
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        if setup_devices := getattr(dr, "async_setup", None):
            setup_devices(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        await ir.async_load(hass)
        entry = ConfigEntry(
            domain=DOMAIN,
            entry_id="entry",
            title="GitHub Copilot",
            data={},
            options={},
            version=1,
            minor_version=2,
            source="user",
            unique_id=None,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        entry._integration_for_domain = SimpleNamespace(
            domain=DOMAIN,
            logger=logging.getLogger("homeassistant.components.github_copilot"),
            async_get_component=AsyncMock(return_value=github_copilot),
        )
        hass.config_entries._entries[entry.entry_id] = entry
        memory = MemoryManager(hass, entry.entry_id)
        original = None
        if has_storage:
            saved = await memory.async_tool(
                "ha_memory_save",
                {
                    "text": "PRIVATE_HOUSEHOLD_FACT",
                    "source": "PRIVATE_HOUSEHOLD_FACT",
                    "expected_revision": 0,
                },
                "PRIVATE_HOUSEHOLD_FACT",
            )
            assert saved["status"] == "saved"
            original = Path(memory.store.path).read_bytes()
        await memory.async_close()
        runtime = SimpleNamespace(
            async_stop=AsyncMock(side_effect=RuntimeError("Runtime is still running")),
            memory=memory,
        )
        if failure == "stop":
            hass.data[DOMAIN] = {entry.entry_id: runtime}
        session_state = Path(hass.config.path(".storage", DOMAIN, entry.entry_id))
        session_state.mkdir(parents=True)
        (session_state / "state").write_text("Live SDK state")
        remove_directory = Mock(wraps=runtime_module.remove_entry_directory)
        monkeypatch.setattr(runtime_module, "remove_entry_directory", remove_directory)
        original_remove = Store.async_remove

        async def fail_memory_remove(store):
            if store.key == memory.key:
                raise PermissionError("PRIVATE_FILESYSTEM_DETAIL")
            await original_remove(store)

        remove_memory = create_autospec(original_remove, side_effect=fail_memory_remove)
        monkeypatch.setattr(Store, "async_remove", remove_memory)
        result = await hass.config_entries.async_remove(entry.entry_id)
        assert result == {"require_restart": False}
        assert hass.config_entries.async_get_entry(entry.entry_id) is None
        if has_storage:
            assert Path(memory.store.path).read_bytes() == original
        else:
            assert not Path(memory.store.path).exists()
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, "memory_cleanup_failed_entry"
        )
        if has_storage:
            assert issue is not None
            assert issue.is_persistent
            assert issue.translation_key == "memory_cleanup_failed"
            assert issue.translation_placeholders == {"filename": memory.key}
        else:
            assert issue is None
        if failure == "stop":
            assert hass.data[DOMAIN][entry.entry_id] is runtime
            assert (session_state / "state").read_text() == "Live SDK state"
            remove_memory.assert_not_awaited()
            remove_directory.assert_not_called()
        else:
            assert not session_state.exists()
        assert "PRIVATE_HOUSEHOLD_FACT" not in caplog.text
        assert "PRIVATE_FILESYSTEM_DETAIL" not in caplog.text
        if failure == "unlink":
            Path(memory.store.path).unlink()
            ir.async_ignore_issue(hass, DOMAIN, "memory_cleanup_failed_entry", True)
            assert (
                ir.async_get(hass)
                .async_get_issue(DOMAIN, "memory_cleanup_failed_entry")
                .dismissed_version
                is not None
            )
    finally:
        await hass.async_stop()


@pytest.mark.parametrize(
    ("time_zone", "offset_hours"),
    [("Asia/Tokyo", 9), ("Pacific/Honolulu", -10), ("America/New_York", None)],
)
async def test_request_time_uses_real_home_assistant_configured_zone(
    tmp_path, time_zone, offset_hours
):
    from zoneinfo import ZoneInfo

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
        zone = ZoneInfo(result["time_zone"])
        assert result["time_zone"] == time_zone
        if offset_hours is None:
            assert current_time.utcoffset() == current_time.astimezone(zone).utcoffset()
            # A historical date can use a different offset than the current one.
            assert (
                datetime(2026, 1, 15, 3, tzinfo=zone).utcoffset()
                != datetime(2026, 7, 15, 3, tzinfo=zone).utcoffset()
            )
        else:
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
    from homeassistant.exceptions import HomeAssistantError
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
    from custom_components.github_copilot.llm_api import (
        ADMIN_REQUIRED,
        ASSIST_API,
        api_includes,
    )

    hass = HomeAssistant(str(tmp_path))
    user = SimpleNamespace(is_admin=True, name="Test administrator")
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    write_calls = []

    class ExampleWriteTool(llm.Tool):
        integration = "example"

        def __init__(self, kind):
            self.name = f"example__{kind}_write"
            self.kind = kind

        async def async_call(self, hass, tool_input, llm_context):
            write_calls.append(self.kind)
            return llm.ToolResult(data={"success": True})

    def example_tools(hass, context, api_id):
        kind = {
            llm.LLM_API_HOME_ASSISTANT: "admin",
            llm.LLM_API_ASSIST: "assist",
        }[api_id]
        return llm_integration.LLMTools(tools=[ExampleWriteTool(kind)])

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
                "example": SimpleNamespace(async_get_tools=example_tools),
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
                "ha__example__admin_write",
            } <= tools.keys()
            assert ("ha__example__assist_write" in tools) is device_control
            calls_to_make = [
                ("ha__system_log__get_entries", {"logger": "example"}),
                ("ha__homeassistant__call_action", {"action": "example.admin_action"}),
                ("ha__example__admin_write", {}),
            ]
            if device_control:
                calls_to_make.append(("ha__example__assist_write", {}))
            for name, arguments in calls_to_make:
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
        assert [item.result_type for item in tool_results] == ["success"] * (
            4 if device_control else 3
        )
        assert "Recorded test warning" in tool_results[0].text_result_for_llm
        assert len(calls) == 1
        assert write_calls == (["admin", "assist"] if device_control else ["admin"])

        user.is_admin = False
        deltas.clear()
        chat.async_add_user_content(conversation.UserContent(content="Hello again."))
        result = await entity._async_handle_message(user_input, chat)
        expected = "Which room?" if question else "Done."
        assert result.response.speech["plain"]["speech"] == expected
        assert result.continue_conversation is question
        assert "".join(delta.get("content") or "" for delta in deltas) == expected
        assert chat.content[-1].content == expected
        assert len(calls) == 1
        assert write_calls == (["admin", "assist"] if device_control else ["admin"])
        assert client.create_session.await_count == 2
        assert (chat.llm_api is not None) is device_control

        user.is_admin = True
        prepared_api = await llm.async_get_api(
            hass,
            [llm.LLM_API_ASSIST, llm.LLM_API_HOME_ASSISTANT],
            user_input.as_llm_context(DOMAIN),
        )
        assert prepared_api.api.requires_admin
        assert api_includes(prepared_api, ASSIST_API)
        assert api_includes(prepared_api, llm.LLM_API_HOME_ASSISTANT)
        entry.options["enable_device_control"] = True
        entry.options["enable_home_assistant_api"] = False
        user.is_admin = False
        before_send = session.send_and_wait.await_count
        with pytest.raises(HomeAssistantError, match="administrator tools.*disabled"):
            await backend.async_handle_message(
                entity.entity_id,
                user_input,
                conversation.ChatLog(hass, "stale-api", llm_api=prepared_api),
            )
        assert session.send_and_wait.await_count == before_send
        assert len(calls) == 1

        entry.options["enable_home_assistant_api"] = True
        assist_api = await llm.async_get_api(
            hass, llm.LLM_API_ASSIST, user_input.as_llm_context(DOMAIN)
        )
        entry.options["enable_device_control"] = False
        before_send = session.send_and_wait.await_count
        before_writes = list(write_calls)
        with pytest.raises(HomeAssistantError, match="Device control is disabled"):
            await backend.async_handle_message(
                entity.entity_id,
                user_input,
                conversation.ChatLog(hass, "stale-assist-api", llm_api=assist_api),
                admin_notice=ADMIN_REQUIRED,
            )
        assert session.send_and_wait.await_count == before_send
        assert write_calls == before_writes
    finally:
        await backend.async_stop()
        await hass.async_stop()


async def test_real_mcp_client_tools_are_discovered_gated_and_called(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from homeassistant.components.mcp import ModelContextProtocolAPI
    from homeassistant.components.mcp import coordinator as mcp_coordinator
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.helpers import llm
    from mcp.types import CallToolResult
    from probatio import from_openapi

    from custom_components.github_copilot.mcp_tools import (
        async_call_mcp_tool,
        async_mcp_bindings,
        registered_mcp_apis,
    )
    from custom_components.github_copilot.schema import convert_tool_schema

    def tool(name, *, read_only):
        return mcp_coordinator.ModelContextProtocolTool(
            name,
            None,
            f"{name} notes",
            from_openapi(
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                }
            ),
            "http://mcp.invalid/mcp",
            SimpleNamespace(),
            annotations=llm.ToolAnnotations(read_only=read_only),
        )

    calls = []
    responses = [{"content": [{"type": "text", "text": "hit"}], "isError": False}]

    @asynccontextmanager
    async def fake_client(hass, url, token_manager):
        async def call_tool(name, arguments):
            calls.append((name, arguments))
            return CallToolResult.model_validate(responses[-1])

        yield SimpleNamespace(call_tool=call_tool), None

    monkeypatch.setattr(mcp_coordinator, "mcp_client", fake_client)
    hass = HomeAssistant(str(tmp_path))
    try:
        api = ModelContextProtocolAPI(
            hass=hass,
            id="mcp-notes",
            name="Notes",
            coordinator=SimpleNamespace(
                data=[tool("search", read_only=True), tool("delete", read_only=False)]
            ),
        )
        llm.async_register_api(hass, api)
        assert registered_mcp_apis(hass) == {"mcp-notes": api}

        options = {"mcp_servers": ["mcp-notes"]}
        context = llm.LLMContext(
            platform="github_copilot",
            context=Context(),
            language="en",
            assistant="conversation",
            device_id=None,
        )
        bindings = await async_mcp_bindings(hass, options, context)
        assert [binding.tool.name for binding in bindings] == ["search"]
        schema = convert_tool_schema(
            bindings[0].tool.parameters,
            custom_serializer=bindings[0].instance.custom_serializer,
        )
        assert schema["type"] == "object"
        assert schema["required"] == ["query"]

        result = await async_call_mcp_tool(
            hass, lambda: options, bindings[0], {"query": "milk"}, "call"
        )
        assert calls == [("search", {"query": "milk"})]
        assert result["content"][0]["text"] == "hit"
        assert "error" not in result

        # HA 2026.10 keeps unknown result fields; 2026.11 drops them.
        responses.append({"content": [], "isError": True, "error": "e" * 20_000})
        result = await asyncio.wait_for(
            async_call_mcp_tool(
                hass, lambda: options, bindings[0], {"query": "milk"}, "call"
            ),
            5,
        )
        assert result["error"] == "mcp_tool_error"
        assert len(json.dumps(result).encode()) <= 16_000

        options["mcp_write_servers"] = ["mcp-notes"]
        bindings = await async_mcp_bindings(hass, options, context)
        assert {binding.tool.name for binding in bindings} == {"search", "delete"}
    finally:
        await hass.async_stop()
