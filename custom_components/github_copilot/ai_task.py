"""One-shot Home Assistant AI Tasks using the entry's Copilot runtime."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.chat_session import ChatSession, current_session
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import const
from .backend import DataTaskRequest, ImageTaskRequest
from .const import (
    CONF_ENABLE_DEVICE_CONTROL,
    CONF_ENABLE_HOME_ASSISTANT_API,
    DOMAIN,
    MODELS_UPDATED_SIGNAL,
    NAME,
)
from .images import verified_image_runtime
from .runtime import CopilotRuntime

PARALLEL_UPDATES = 0


@dataclass(frozen=True)
class _CapturedContext:
    entity: object
    session: ChatSession
    task: asyncio.Task[Any] | None
    context: Context


_request_context: ContextVar[_CapturedContext | None] = ContextVar(
    "github_copilot_ai_task_context", default=None
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one AI Task entity alongside the conversation entity."""
    async_add_entities([GitHubCopilotAITaskEntity(entry)])


class GitHubCopilotAITaskEntity(ai_task.AITaskEntity):
    """Generate data without retaining another task's SDK history."""

    _attr_has_entity_name = True
    _attr_name = NAME
    _attr_supported_features = ai_task.AITaskEntityFeature.GENERATE_DATA

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ai_task"
        if (
            const.IMAGE_INPUT_VERIFIED
            and const.IMAGE_HOST_BUDGET_VERIFIED
            and verified_image_runtime()
        ):
            self._attr_supported_features |= (
                ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
            )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{MODELS_UPDATED_SIGNAL}_{self.entry.entry_id}",
                self._refresh_features,
            )
        )
        self._refresh_features()

    @callback
    def _refresh_features(self) -> None:
        runtime: CopilotRuntime = self.hass.data[DOMAIN][self.entry.entry_id]
        features = ai_task.AITaskEntityFeature.GENERATE_DATA
        if (
            const.IMAGE_INPUT_VERIFIED
            and const.IMAGE_HOST_BUDGET_VERIFIED
            and verified_image_runtime()
        ):
            features |= ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        if (
            const.IMAGE_GENERATION_VERIFIED
            and const.IMAGE_HOST_BUDGET_VERIFIED
            and runtime.image_model() is not None
        ):
            features |= ai_task.AITaskEntityFeature.GENERATE_IMAGE
        self._attr_supported_features = features
        self.async_write_ha_state()

    @callback
    def async_set_context(self, context: Context) -> None:
        """Capture the caller before HA's chat preparation can yield."""
        super().async_set_context(context)
        session = current_session.get()
        _request_context.set(
            _CapturedContext(self, session, asyncio.current_task(), context)
            if session is not None
            else None
        )

    def _consume_context(self, chat_log: conversation.ChatLog) -> Context | None:
        captured = _request_context.get()
        session = current_session.get()
        if (
            self.entry.options.get(CONF_ENABLE_DEVICE_CONTROL)
            or self.entry.options.get(CONF_ENABLE_HOME_ASSISTANT_API)
        ) and (
            session is None
            or session.conversation_id != chat_log.conversation_id
            or captured is not None
            and captured.entity is self
            and captured.session is session
            and captured.task is not asyncio.current_task()
        ):
            raise HomeAssistantError(
                "AI Tasks with Home Assistant tools must use Home Assistant's task "
                "service or public task helper so caller context can be preserved."
            )
        _request_context.set(None)
        if (
            captured is not None
            and captured.entity is self
            and captured.session is session
            and captured.task is asyncio.current_task()
            and session is not None
            and session.conversation_id == chat_log.conversation_id
        ):
            return captured.context
        return None

    async def _async_generate_data(
        self, task: ai_task.GenDataTask, chat_log: conversation.ChatLog
    ) -> ai_task.GenDataTaskResult:
        context = self._consume_context(chat_log)
        if task.llm_api is not None:
            raise HomeAssistantError(
                "Caller-supplied LLM APIs are not supported. Use the integration's "
                "settings to enable Home Assistant tools."
            )
        runtime: CopilotRuntime = self.hass.data[DOMAIN][self.entry.entry_id]
        if runtime.backend is None:
            raise HomeAssistantError("GitHub Copilot is not ready.")
        data = await runtime.backend.async_generate_data(
            self.entity_id,
            DataTaskRequest(
                text=task.instructions,
                context=llm.LLMContext(
                    platform=DOMAIN,
                    context=context,
                    language=None,
                    assistant=conversation.DOMAIN,
                    device_id=None,
                ),
                structure=task.structure,
                attachments=task.attachments or [],
            ),
            chat_log,
        )
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=data
        )

    async def _async_generate_image(
        self, task: ai_task.GenImageTask, chat_log: conversation.ChatLog
    ) -> ai_task.GenImageTaskResult:
        context = self._consume_context(chat_log)
        runtime: CopilotRuntime = self.hass.data[DOMAIN][self.entry.entry_id]
        if runtime.backend is None:
            raise HomeAssistantError("GitHub Copilot is not ready.")
        image = await runtime.backend.async_generate_image(
            self.entity_id,
            ImageTaskRequest(
                text=task.instructions,
                context=llm.LLMContext(
                    platform=DOMAIN,
                    context=context,
                    language=None,
                    assistant=conversation.DOMAIN,
                    device_id=None,
                ),
                attachments=task.attachments or [],
            ),
            chat_log,
        )
        return ai_task.GenImageTaskResult(
            image_data=image.data,
            mime_type=image.mime_type,
            conversation_id=chat_log.conversation_id,
            width=image.width,
            height=image.height,
            model=image.model,
        )
