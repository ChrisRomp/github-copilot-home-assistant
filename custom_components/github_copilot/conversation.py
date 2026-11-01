"""Assist conversation entity backed by GitHub Copilot."""

from __future__ import annotations

from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_DEVICE_CONTROL, CONF_PROMPT, DOMAIN, NAME
from .runtime import CopilotRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the conversation agent."""
    async_add_entities([GitHubCopilotConversationEntity(entry)])


class GitHubCopilotConversationEntity(conversation.ConversationEntity):
    """A streaming conversation agent for Home Assistant Assist."""

    _attr_has_entity_name = True
    _attr_name = NAME
    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        if entry.options.get(CONF_ENABLE_DEVICE_CONTROL):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return that Copilot can handle any language."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Run one Copilot turn and append streamed messages to the HA log."""
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                ["assist"]
                if self.entry.options.get(CONF_ENABLE_DEVICE_CONTROL)
                else None,
                self.entry.options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        runtime: CopilotRuntime = self.hass.data[DOMAIN][self.entry.entry_id]
        if runtime.backend is None:
            raise HomeAssistantError("GitHub Copilot is not ready.")
        await runtime.backend.async_handle_message(self.entity_id, user_input, chat_log)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
