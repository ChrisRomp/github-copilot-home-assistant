"""AI Task contracts against the minimum supported Home Assistant."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HA_COPILOT_HA_TESTS") != "1",
    reason="Requires the pinned Home Assistant component requirements",
)


@pytest.fixture
async def ai_setup(tmp_path, monkeypatch):
    from homeassistant.components import ai_task
    from homeassistant.core import HomeAssistant

    from custom_components.github_copilot import const
    from custom_components.github_copilot.ai_task import GitHubCopilotAITaskEntity
    from custom_components.github_copilot.const import DOMAIN

    monkeypatch.setattr(const, "IMAGE_HOST_BUDGET_VERIFIED", False)

    hass = HomeAssistant(str(tmp_path))
    backend = SimpleNamespace(async_generate_data=AsyncMock(return_value="Task result"))
    entry = SimpleNamespace(
        entry_id="test", options={"model": "chat", "enable_device_control": True}
    )
    entity = GitHubCopilotAITaskEntity(entry)
    entity.hass = hass
    entity.entity_id = "ai_task.github_copilot"
    entity.platform = SimpleNamespace(domain=DOMAIN)
    entity.async_write_ha_state = Mock()
    hass.data[DOMAIN] = {"test": SimpleNamespace(backend=backend)}
    hass.data[ai_task.DOMAIN] = SimpleNamespace(get_entity=Mock(return_value=entity))
    yield SimpleNamespace(
        hass=hass, entry=entry, entity=entity, backend=backend, ai_task=ai_task
    )
    await hass.async_stop()


async def test_real_service_helper_returns_result_and_records_last_used(ai_setup):
    result = await ai_setup.ai_task.async_generate_data(
        ai_setup.hass,
        entity_id=ai_setup.entity.entity_id,
        task_name="notification",
        instructions="Summarize.",
    )
    assert result.data == "Task result"
    assert result.conversation_id
    assert ai_setup.entity.state is not None
    request = ai_setup.backend.async_generate_data.await_args.args[1]
    assert request.text == "Summarize."
    assert request.context.assistant == "conversation"
    assert request.context.context is None
    assert ai_setup.entity.unique_id == "test_ai_task"


async def test_interleaved_callers_keep_their_own_context(ai_setup, monkeypatch):
    from homeassistant.components.conversation import ChatLog
    from homeassistant.core import Context

    entered = 0
    both_entered = asyncio.Event()

    async def provide(chat, context, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()

    monkeypatch.setattr(ChatLog, "async_provide_llm_data", provide)
    await asyncio.gather(
        *(
            ai_setup.ai_task.async_generate_data(
                ai_setup.hass,
                entity_id=ai_setup.entity.entity_id,
                task_name=user,
                instructions=user,
                context=Context(user_id=user),
            )
            for user in ("alice", "bob")
        )
    )
    requests = [
        call.args[1] for call in ai_setup.backend.async_generate_data.await_args_list
    ]
    assert {request.text: request.context.context.user_id for request in requests} == {
        "alice": "alice",
        "bob": "bob",
    }


async def test_failed_preparation_does_not_leak_context_into_next_call(
    ai_setup, monkeypatch
):
    from homeassistant.components.conversation import ChatLog
    from homeassistant.core import Context
    from homeassistant.exceptions import HomeAssistantError

    monkeypatch.setattr(
        ChatLog,
        "async_provide_llm_data",
        AsyncMock(side_effect=[HomeAssistantError("Preparation failed"), None]),
    )
    with pytest.raises(HomeAssistantError, match="Preparation failed"):
        await ai_setup.ai_task.async_generate_data(
            ai_setup.hass,
            entity_id=ai_setup.entity.entity_id,
            task_name="first",
            instructions="first",
            context=Context(user_id="alice"),
        )
    await ai_setup.ai_task.async_generate_data(
        ai_setup.hass,
        entity_id=ai_setup.entity.entity_id,
        task_name="second",
        instructions="second",
        context=None,
    )
    request = ai_setup.backend.async_generate_data.await_args.args[1]
    assert request.context.context is None


async def test_direct_control_call_without_session_is_rejected(ai_setup):
    from homeassistant.components.conversation import ChatLog
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.chat_session import current_session

    token = current_session.set(None)
    try:
        with pytest.raises(HomeAssistantError, match="public task helper"):
            await ai_setup.entity._async_generate_data(
                ai_setup.ai_task.GenDataTask(name="test", instructions="test"),
                ChatLog(ai_setup.hass, "unsupported-direct-call"),
            )
        ai_setup.backend.async_generate_data.assert_not_awaited()
    finally:
        current_session.reset(token)


async def test_caller_supplied_api_is_not_executed(ai_setup):
    from homeassistant.components.conversation import ChatLog
    from homeassistant.exceptions import HomeAssistantError

    ai_setup.entry.options["enable_device_control"] = False
    with pytest.raises(HomeAssistantError, match="Caller-supplied"):
        await ai_setup.entity._async_generate_data(
            ai_setup.ai_task.GenDataTask(
                name="test", instructions="test", llm_api=Mock()
            ),
            ChatLog(ai_setup.hass, "test"),
        )
    ai_setup.backend.async_generate_data.assert_not_awaited()


async def test_inherited_context_cannot_authorize_direct_child_call(ai_setup):
    from homeassistant.components.conversation import ChatLog
    from homeassistant.core import Context
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.chat_session import async_get_chat_session

    with async_get_chat_session(ai_setup.hass) as session:
        ai_setup.entity.async_set_context(Context(user_id="parent"))

        async def child():
            for _ in range(2):
                with pytest.raises(HomeAssistantError, match="public task helper"):
                    await ai_setup.entity._async_generate_data(
                        ai_setup.ai_task.GenDataTask(
                            name="child", instructions="child"
                        ),
                        ChatLog(ai_setup.hass, session.conversation_id),
                    )

        await asyncio.create_task(child())
    ai_setup.backend.async_generate_data.assert_not_awaited()


async def test_unverified_images_are_not_advertised_or_dispatched(ai_setup):
    from homeassistant.exceptions import HomeAssistantError

    features = ai_setup.entity.supported_features
    assert features == ai_setup.ai_task.AITaskEntityFeature.GENERATE_DATA
    with pytest.raises(HomeAssistantError, match="does not support attachments"):
        await ai_setup.ai_task.async_generate_data(
            ai_setup.hass,
            entity_id=ai_setup.entity.entity_id,
            task_name="camera",
            instructions="Describe",
            attachments=[{"media_content_id": "media-source://camera/camera.test"}],
        )
    with pytest.raises(HomeAssistantError, match="does not support generating images"):
        await ai_setup.ai_task.async_generate_image(
            ai_setup.hass,
            entity_id=ai_setup.entity.entity_id,
            task_name="image",
            instructions="Draw",
        )
    ai_setup.backend.async_generate_data.assert_not_awaited()


async def test_ha_invalidates_supported_features_cache(ai_setup):
    feature = ai_setup.ai_task.AITaskEntityFeature
    assert ai_setup.entity.supported_features == feature.GENERATE_DATA
    ai_setup.entity._attr_supported_features |= feature.SUPPORT_ATTACHMENTS
    assert feature.SUPPORT_ATTACHMENTS in ai_setup.entity.supported_features


async def test_generation_capability_tracks_discovery_and_host_gate(
    ai_setup, monkeypatch
):
    from custom_components.github_copilot import const

    runtime = ai_setup.hass.data[const.DOMAIN]["test"]
    runtime.image_model = Mock(return_value=SimpleNamespace(id="image"))
    ai_setup.entity._refresh_features()
    assert not (
        ai_setup.entity.supported_features
        & ai_setup.ai_task.AITaskEntityFeature.GENERATE_IMAGE
    )
    monkeypatch.setattr(const, "IMAGE_HOST_BUDGET_VERIFIED", True)
    ai_setup.entity._refresh_features()
    assert (
        ai_setup.entity.supported_features
        & ai_setup.ai_task.AITaskEntityFeature.GENERATE_IMAGE
    )
    runtime.image_model.return_value = None
    ai_setup.entity._refresh_features()
    assert not (
        ai_setup.entity.supported_features
        & ai_setup.ai_task.AITaskEntityFeature.GENERATE_IMAGE
    )


async def test_migration_seeds_task_model_once_without_changing_conversation():
    from custom_components.github_copilot import async_migrate_entry

    entry = SimpleNamespace(
        version=1,
        minor_version=1,
        options={"model": "original", "prompt": "keep"},
        data={"model_billing": {"model": "original", "multiplier": 1.0}},
    )

    def update(entry, **kwargs):
        for key, value in kwargs.items():
            setattr(entry, key, value)

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=Mock(side_effect=update))
    )
    assert await async_migrate_entry(hass, entry)
    assert entry.options["ai_task_model"] == "original"
    assert entry.options["ai_task_timeout"] == 180
    assert entry.options["prompt"] == "keep"
    assert entry.data["ai_task_model_billing"] == entry.data["model_billing"]
    entry.options["model"] = "new-conversation"
    assert await async_migrate_entry(hass, entry)
    assert entry.options["ai_task_model"] == "original"
    hass.config_entries.async_update_entry.assert_called_once()


async def test_real_image_helper_publishes_bytes_through_ha_media(
    ai_setup, monkeypatch
):
    from homeassistant.components.ai_task import task as task_module
    from homeassistant.components.ai_task.const import DATA_MEDIA_SOURCE

    from custom_components.github_copilot.images import GeneratedImage

    ai_setup.entry.options["enable_device_control"] = False
    ai_setup.entity._attr_supported_features |= (
        ai_setup.ai_task.AITaskEntityFeature.GENERATE_IMAGE
    )
    ai_setup.backend.async_generate_image = AsyncMock(
        return_value=GeneratedImage(
            b"validated-image-bytes",
            "image/png",
            32,
            32,
            "native-image-model",
        )
    )
    media = SimpleNamespace(
        async_upload_media=AsyncMock(
            return_value="media-source://ai_task/image/test.png"
        ),
        async_resolve_media=AsyncMock(
            return_value=SimpleNamespace(url="/media/test.png")
        ),
    )
    ai_setup.hass.data[DATA_MEDIA_SOURCE] = media
    monkeypatch.setattr(
        task_module, "async_sign_path", lambda *args: "/signed/test.png"
    )
    result = await ai_setup.ai_task.async_generate_image(
        ai_setup.hass,
        entity_id=ai_setup.entity.entity_id,
        task_name="image",
        instructions="Draw a circle",
    )
    assert "image_data" not in result
    assert result["url"] == "/signed/test.png"
    assert result["media_source_id"] == "media-source://ai_task/image/test.png"
    assert result["width"] == result["height"] == 32
    assert result["model"] == "native-image-model"
    media.async_upload_media.assert_awaited_once()
    assert (
        media.async_upload_media.await_args.args[1].file.read()
        == b"validated-image-bytes"
    )
