"""Repairs of the sources: entities renamed, or gone.

When the user renames an entity of a source, or its trigger, the source
follows it. When one is gone, removed from Home Assistant, a repair says so
once Home Assistant has started, and its fix chooses the entities the
source uses now. An entity counts as gone when the entity registry does not
know it and it has no state, so that an entity whose integration is still
loading, or one without a unique ID, is not.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started

from .const import CONF_ENTITIES, CONF_MEAL_PICTURES, CONF_TRIGGER, DOMAIN
from .source_flow import ENTITY_SELECTOR, TRIGGER_SELECTOR, entity_errors, trigger_errors
from .sources import cameras, sources

ISSUE_SOURCE_ENTITY_MISSING = "source_entity_missing"


def missing(hass: HomeAssistant, subentry: ConfigSubentry) -> list[str]:
    """The entities of a source, and the trigger of its cameras, that are gone."""
    registry = er.async_get(hass)
    watched = list(subentry.data[CONF_ENTITIES])
    if cameras(watched) and subentry.data[CONF_TRIGGER]:
        watched.append(subentry.data[CONF_TRIGGER])
    return [
        entity_id
        for entity_id in watched
        if registry.async_get(entity_id) is None and hass.states.get(entity_id) is None
    ]


@callback
def async_follow_renames(hass: HomeAssistant) -> None:
    """Make the sources of every entry follow the renames of their entities.

    The integration listens, not each entry: saving a source reloads its
    entry at once, and the entities of a device renamed together must all
    be followed, even while the entry loads again.
    """

    @callback
    def _async_registry_updated(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] != "update" or "old_entity_id" not in event.data:
            return
        for entry in hass.config_entries.async_entries(DOMAIN):
            _async_rename(hass, entry, event.data["old_entity_id"], event.data["entity_id"])

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _async_registry_updated)


@callback
def _async_rename(hass: HomeAssistant, entry: ConfigEntry, old: str, new: str) -> None:
    for subentry in sources(entry).values():
        data = subentry.data
        if old not in data[CONF_ENTITIES] and data[CONF_TRIGGER] != old:
            continue
        entities = [new if entity_id == old else entity_id for entity_id in data[CONF_ENTITIES]]
        trigger = new if data[CONF_TRIGGER] == old else data[CONF_TRIGGER]
        meals = [
            new if entity_id == old else entity_id for entity_id in data.get(CONF_MEAL_PICTURES, [])
        ]
        # Saving the source reloads the entry, which follows the new name.
        hass.config_entries.async_update_subentry(
            entry,
            subentry,
            data={
                **data,
                CONF_ENTITIES: list(dict.fromkeys(entities)),
                CONF_TRIGGER: trigger,
                CONF_MEAL_PICTURES: list(dict.fromkeys(meals)),
            },
        )


@callback
def async_watch_entities(hass: HomeAssistant, entry: ConfigEntry) -> CALLBACK_TYPE:
    """Tell the entities of the sources that are gone, once Home Assistant has started."""

    @callback
    def _async_registry_updated(_event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if hass.state is CoreState.running:
            async_check_sources(hass, entry)

    @callback
    def _async_started(_hass: HomeAssistant) -> None:
        async_check_sources(hass, entry)

    unsubscribe = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _async_registry_updated)
    cancel = async_at_started(hass, _async_started)

    @callback
    def _async_stop() -> None:
        unsubscribe()
        cancel()

    return _async_stop


@callback
def async_check_sources(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise a repair for each source with an entity gone, and clear the others."""
    for source_id, subentry in sources(entry).items():
        issue_id = f"{ISSUE_SOURCE_ENTITY_MISSING}_{source_id}"
        if not (gone := missing(hass, subentry)):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            data={"entry_id": entry.entry_id, "subentry_id": source_id},
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SOURCE_ENTITY_MISSING,
            translation_placeholders={"source": subentry.title, "entities": ", ".join(gone)},
        )


@callback
def async_delete_source_issues(hass: HomeAssistant) -> None:
    """Remove the repairs about sources."""
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(f"{ISSUE_SOURCE_ENTITY_MISSING}_"):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """The fix of a source with an entity gone."""
    if data is None:
        raise ValueError(f"the repair {issue_id} does not say which source it is about")
    return SourceEntitiesFlow(str(data["entry_id"]), str(data["subentry_id"]))


class SourceEntitiesFlow(RepairsFlow):
    """Choose the entities a source uses now."""

    def __init__(self, entry_id: str, subentry_id: str) -> None:
        self._entry_id = entry_id
        self._subentry_id = subentry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start with the form: what the first step receives is the repair's own data."""
        return await self.async_step_entities()

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Show what is gone, and take the new entities."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        subentry = None if entry is None else sources(entry).get(self._subentry_id)
        if entry is None or subentry is None:
            return self.async_abort(reason="source_removed")
        gone = missing(self.hass, subentry)
        data = subentry.data
        errors: dict[str, str] = {}
        if user_input is not None:
            entities = list(dict.fromkeys(user_input.get(CONF_ENTITIES) or []))
            trigger = user_input.get(CONF_TRIGGER) or None
            errors = entity_errors(entry, entities, self._subentry_id) or trigger_errors(
                entities, trigger
            )
            if not errors:
                self.hass.config_entries.async_update_subentry(
                    entry,
                    subentry,
                    data={
                        **data,
                        CONF_ENTITIES: entities,
                        # Kept when the cameras go and none is chosen, as the
                        # source flow does.
                        CONF_TRIGGER: trigger or data[CONF_TRIGGER],
                        # Only among the entities left.
                        CONF_MEAL_PICTURES: [
                            entity_id
                            for entity_id in data.get(CONF_MEAL_PICTURES, [])
                            if entity_id in entities
                        ],
                    },
                )
                return self.async_create_entry(data={})
        current = {
            CONF_ENTITIES: [
                entity_id for entity_id in data[CONF_ENTITIES] if entity_id not in gone
            ],
            CONF_TRIGGER: None if data[CONF_TRIGGER] in gone else data[CONF_TRIGGER],
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITIES): ENTITY_SELECTOR,
                vol.Optional(CONF_TRIGGER): TRIGGER_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="entities",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or current),
            description_placeholders={"source": subentry.title, "entities": ", ".join(gone)},
            errors=errors,
        )
