"""Events: each visit of a source, once it ended, and once corrected."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetRecognitionConfigEntry
from .core.visits import Visit
from .counts import VISIT_CORRECTED, VISIT_ENDED, VisitCounter, visit_data
from .entity import subentry_device
from .pets import pets
from .sources import sources

# Entities only read what the integration holds.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetRecognitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the visit event of each source, to its subentry."""
    titles = {key: subentry.title for key, subentry in pets(entry).items()}
    for source_id, subentry in sources(entry).items():
        async_add_entities(
            [VisitEvent(entry.entry_id, subentry, entry.runtime_data.counter, titles)],
            config_subentry_id=source_id,
        )


class VisitEvent(EventEntity):
    """The visits of a source, each announced once when it ended, and again
    each time a correction changes it."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "visit"

    def __init__(
        self,
        entry_id: str,
        subentry: ConfigSubentry,
        counter: VisitCounter,
        titles: dict[str, str],
    ) -> None:
        self._attr_event_types = [VISIT_ENDED, VISIT_CORRECTED]
        self._source = subentry.subentry_id
        self._counter = counter
        self._titles = titles
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_visit"
        self._attr_device_info = subentry_device(entry_id, subentry)

    async def async_added_to_hass(self) -> None:
        """Listen to the visits of the source that end."""
        await super().async_added_to_hass()
        self.async_on_remove(self._counter.async_add_announcer(self._source, self._async_announced))

    @callback
    def _async_announced(self, kind: str, visit: Visit) -> None:
        meals = self._counter.counts_meals(self._source)
        self._trigger_event(kind, visit_data(visit, self._titles, meals))
        self.async_write_ha_state()
