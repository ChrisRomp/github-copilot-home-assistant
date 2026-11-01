"""Tests for conversation entity naming and stable identity."""

from types import SimpleNamespace

from custom_components.github_copilot.const import CONF_ENABLE_DEVICE_CONTROL, NAME


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
