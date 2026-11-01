"""Tests for conversation entity naming and stable identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.github_copilot.const import (
    CONF_ENABLE_DEVICE_CONTROL,
    CONF_PROMPT,
    DOMAIN,
    NAME,
)


def test_default_entity_name_is_friendly_and_unique_id_is_stable(ha_boundary):
    module = ha_boundary.load("conversation")
    entity = module.GitHubCopilotConversationEntity(
        SimpleNamespace(entry_id="01m6ch71ebwbxmzat4855j23m9", options={})
    )
    assert entity._attr_name == NAME
    assert entity._attr_unique_id == "01m6ch71ebwbxmzat4855j23m9"
    assert entity._attr_has_entity_name is True
    assert "entity_id" not in entity.__dict__


def test_multiple_agents_have_distinct_identities_with_same_default_name(ha_boundary):
    module = ha_boundary.load("conversation")
    entities = [
        module.GitHubCopilotConversationEntity(
            SimpleNamespace(entry_id=entry_id, options={})
        )
        for entry_id in ("first-entry", "second-entry")
    ]
    assert entities[0]._attr_name == entities[1]._attr_name == NAME
    assert entities[0]._attr_unique_id != entities[1]._attr_unique_id


def test_device_control_remains_opt_in(ha_boundary):
    module = ha_boundary.load("conversation")
    entity = module.GitHubCopilotConversationEntity(
        SimpleNamespace(entry_id="entry", options={CONF_ENABLE_DEVICE_CONTROL: True})
    )
    assert entity._attr_supported_features == (
        module.conversation.ConversationEntityFeature.CONTROL
    )


async def test_owner_prompt_is_not_rendered_into_mixed_ha_system_content(
    ha_boundary, monkeypatch
):
    module = ha_boundary.load("conversation")
    entry = SimpleNamespace(
        entry_id="entry",
        options={CONF_PROMPT: "Describe {{ states('sensor.device_text') }}"},
    )
    entity = module.GitHubCopilotConversationEntity(entry)
    backend = SimpleNamespace(async_handle_message=AsyncMock())
    entity.hass = SimpleNamespace(
        data={DOMAIN: {"entry": SimpleNamespace(backend=backend)}}
    )
    entity.entity_id = "conversation.github_copilot"
    user = SimpleNamespace(
        as_llm_context=Mock(return_value="context"), extra_system_prompt="request data"
    )
    chat = SimpleNamespace(async_provide_llm_data=AsyncMock())
    get_result = Mock(return_value="result")
    monkeypatch.setattr(
        module.conversation, "async_get_result_from_chat_log", get_result, raising=False
    )
    assert await entity._async_handle_message(user, chat) == "result"
    chat.async_provide_llm_data.assert_awaited_once_with(
        "context", None, None, "request data"
    )
    backend.async_handle_message.assert_awaited_once_with(entity.entity_id, user, chat)
    assert entry.options[CONF_PROMPT] == "Describe {{ states('sensor.device_text') }}"
