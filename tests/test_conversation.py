"""Tests for conversation entity naming and stable identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.github_copilot.const import (
    CONF_ENABLE_DEVICE_CONTROL,
    CONF_ENABLE_HOME_ASSISTANT_API,
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


def test_administrator_tools_also_advertise_control(ha_boundary):
    module = ha_boundary.load("conversation")
    entity = module.GitHubCopilotConversationEntity(
        SimpleNamespace(
            entry_id="entry", options={CONF_ENABLE_HOME_ASSISTANT_API: True}
        )
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
    llm_context = SimpleNamespace(context=None)
    user = SimpleNamespace(
        as_llm_context=Mock(return_value=llm_context),
        extra_system_prompt="request data",
    )
    chat = SimpleNamespace(async_provide_llm_data=AsyncMock())
    get_result = Mock(return_value="result")
    monkeypatch.setattr(
        module.conversation, "async_get_result_from_chat_log", get_result, raising=False
    )
    assert await entity._async_handle_message(user, chat) == "result"
    chat.async_provide_llm_data.assert_awaited_once_with(
        llm_context, None, None, "request data"
    )
    backend.async_handle_message.assert_awaited_once_with(
        entity.entity_id, user, chat, admin_notice=None
    )
    assert entry.options[CONF_PROMPT] == "Describe {{ states('sensor.device_text') }}"


@pytest.mark.parametrize(
    ("device_control", "administrator_tools", "expected_apis"),
    [
        (False, False, None),
        (True, False, ["assist"]),
        (False, True, ["homeassistant"]),
        (True, True, ["assist", "homeassistant"]),
    ],
)
async def test_administrator_receives_only_selected_apis(
    ha_boundary, monkeypatch, device_control, administrator_tools, expected_apis
):
    monkeypatch.setattr(
        ha_boundary.llm, "LLM_API_HOME_ASSISTANT", "homeassistant", raising=False
    )
    module = ha_boundary.load("conversation")
    entity = module.GitHubCopilotConversationEntity(
        SimpleNamespace(
            entry_id="entry",
            options={
                CONF_ENABLE_DEVICE_CONTROL: device_control,
                CONF_ENABLE_HOME_ASSISTANT_API: administrator_tools,
            },
        )
    )
    backend = SimpleNamespace(async_handle_message=AsyncMock())
    auth = SimpleNamespace(
        async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True))
    )
    entity.hass = SimpleNamespace(
        auth=auth, data={DOMAIN: {"entry": SimpleNamespace(backend=backend)}}
    )
    entity.entity_id = "conversation.github_copilot"
    context = SimpleNamespace(context=SimpleNamespace(user_id="admin"))
    user_input = SimpleNamespace(
        as_llm_context=Mock(return_value=context), extra_system_prompt=None
    )
    chat = SimpleNamespace(async_provide_llm_data=AsyncMock())
    result = object()
    monkeypatch.setattr(
        module.conversation,
        "async_get_result_from_chat_log",
        Mock(return_value=result),
        raising=False,
    )

    assert await entity._async_handle_message(user_input, chat) is result
    chat.async_provide_llm_data.assert_awaited_once_with(
        context, expected_apis, None, None
    )
    backend.async_handle_message.assert_awaited_once_with(
        entity.entity_id, user_input, chat, admin_notice=None
    )
    assert auth.async_get_user.await_count == int(administrator_tools)


@pytest.mark.parametrize(
    ("supported", "user_id", "user", "reason"),
    [
        (True, "member", SimpleNamespace(is_admin=False), "administrator"),
        (True, "deleted", None, "administrator"),
        (True, None, None, "administrator"),
        (False, "admin", SimpleNamespace(is_admin=True), "2026.11"),
    ],
)
@pytest.mark.parametrize("device_control", [False, True])
async def test_unavailable_admin_tools_preserve_chat_and_explain(
    ha_boundary, monkeypatch, supported, user_id, user, reason, device_control
):
    if supported:
        monkeypatch.setattr(
            ha_boundary.llm, "LLM_API_HOME_ASSISTANT", "homeassistant", raising=False
        )
    module = ha_boundary.load("conversation")
    entity = module.GitHubCopilotConversationEntity(
        SimpleNamespace(
            entry_id="entry",
            options={
                CONF_ENABLE_DEVICE_CONTROL: device_control,
                CONF_ENABLE_HOME_ASSISTANT_API: True,
            },
        )
    )
    backend = SimpleNamespace(async_handle_message=AsyncMock())
    auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    entity.hass = SimpleNamespace(
        auth=auth, data={DOMAIN: {"entry": SimpleNamespace(backend=backend)}}
    )
    entity.entity_id = "conversation.github_copilot"
    context = SimpleNamespace(context=SimpleNamespace(user_id=user_id))
    user_input = SimpleNamespace(
        as_llm_context=Mock(return_value=context), extra_system_prompt=None
    )
    chat = SimpleNamespace(async_provide_llm_data=AsyncMock())
    result = SimpleNamespace(response=SimpleNamespace(speech={}))
    monkeypatch.setattr(
        module.conversation,
        "async_get_result_from_chat_log",
        Mock(return_value=result),
        raising=False,
    )

    assert await entity._async_handle_message(user_input, chat) is result
    chat.async_provide_llm_data.assert_awaited_once_with(
        context, ["assist"] if device_control else None, None, None
    )
    notice = backend.async_handle_message.await_args.kwargs["admin_notice"]
    assert reason in notice
    assert auth.async_get_user.await_count == int(supported and user_id is not None)


async def test_revoked_admin_during_api_preparation_falls_back_to_assist(
    ha_boundary, monkeypatch
):
    monkeypatch.setattr(
        ha_boundary.llm, "LLM_API_HOME_ASSISTANT", "homeassistant", raising=False
    )
    module = ha_boundary.load("conversation")

    class ConverseError(Exception):
        pass

    monkeypatch.setattr(
        module.conversation, "ConverseError", ConverseError, raising=False
    )
    entity = module.GitHubCopilotConversationEntity(
        SimpleNamespace(
            entry_id="entry",
            options={
                CONF_ENABLE_DEVICE_CONTROL: True,
                CONF_ENABLE_HOME_ASSISTANT_API: True,
            },
        )
    )
    user = SimpleNamespace(is_admin=True)
    backend = SimpleNamespace(async_handle_message=AsyncMock())
    entity.hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user)),
        data={DOMAIN: {"entry": SimpleNamespace(backend=backend)}},
    )
    entity.entity_id = "conversation.github_copilot"
    context = SimpleNamespace(context=SimpleNamespace(user_id="admin"))
    user_input = SimpleNamespace(
        as_llm_context=Mock(return_value=context), extra_system_prompt=None
    )

    async def provide(_context, api_ids, *_args):
        if "homeassistant" in api_ids:
            user.is_admin = False
            raise ConverseError("denied") from ha_boundary.Unauthorized()

    chat = SimpleNamespace(async_provide_llm_data=AsyncMock(side_effect=provide))
    response = SimpleNamespace(
        speech={"plain": {"speech": "Regular answer"}}, async_set_speech=Mock()
    )
    monkeypatch.setattr(
        module.conversation,
        "async_get_result_from_chat_log",
        Mock(return_value=SimpleNamespace(response=response)),
        raising=False,
    )

    await entity._async_handle_message(user_input, chat)
    assert [call.args[1] for call in chat.async_provide_llm_data.await_args_list] == [
        ["assist", "homeassistant"],
        ["assist"],
    ]
    assert (
        "administrator"
        in backend.async_handle_message.await_args.kwargs["admin_notice"]
    )


async def test_admin_api_denial_for_current_admin_is_reported(ha_boundary, monkeypatch):
    monkeypatch.setattr(
        ha_boundary.llm, "LLM_API_HOME_ASSISTANT", "homeassistant", raising=False
    )
    module = ha_boundary.load("conversation")

    class ConverseError(Exception):
        pass

    monkeypatch.setattr(
        module.conversation, "ConverseError", ConverseError, raising=False
    )
    user = SimpleNamespace(is_admin=True)
    backend = SimpleNamespace(async_handle_message=AsyncMock())
    entity = module.GitHubCopilotConversationEntity(
        SimpleNamespace(
            entry_id="entry",
            options={
                CONF_ENABLE_DEVICE_CONTROL: True,
                CONF_ENABLE_HOME_ASSISTANT_API: True,
            },
        )
    )
    entity.hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user)),
        data={DOMAIN: {"entry": SimpleNamespace(backend=backend)}},
    )
    entity.entity_id = "conversation.github_copilot"
    user_input = SimpleNamespace(
        as_llm_context=Mock(
            return_value=SimpleNamespace(context=SimpleNamespace(user_id="admin"))
        ),
        extra_system_prompt=None,
    )

    async def deny(*_args):
        raise ConverseError("denied") from ha_boundary.Unauthorized()

    chat = SimpleNamespace(async_provide_llm_data=AsyncMock(side_effect=deny))

    with pytest.raises(
        ha_boundary.HomeAssistantError,
        match="denied access to administrator tools",
    ):
        await entity._async_handle_message(user_input, chat)

    assert chat.async_provide_llm_data.await_count == 1
    backend.async_handle_message.assert_not_awaited()
