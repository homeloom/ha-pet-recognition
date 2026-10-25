"""Sensors: what each pet and each source last saw, and the engine's queue."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetRecognitionConfigEntry, PetRecognitionData
from .core.matching import OTHER_ANIMAL
from .core.visits import Visit
from .entity import JournalEntity, hub_device, subentry_device
from .examples import ExampleStore
from .journal import Record
from .pets import pets
from .sources import sources

# Entities only read what the integration holds.
PARALLEL_UPDATES = 0

# The last visitor when it is not one pet. "unknown" would be read as the
# state of a sensor that knows nothing.
SEVERAL = "several"
UNKNOWN_ANIMAL = "unknown_animal"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetRecognitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the hub's sensors, and those of each pet and each source to its subentry."""
    data = entry.runtime_data
    async_add_entities([PendingPictures(entry.entry_id, data)])
    titles = {key: subentry.title for key, subentry in pets(entry).items()}
    places = {source_id: subentry.title for source_id, subentry in sources(entry).items()}
    for key, subentry in pets(entry).items():
        async_add_entities(
            [
                PetExamples(entry.entry_id, subentry, key, data.examples),
                LastSeen(entry.entry_id, subentry, key, data, places),
            ],
            config_subentry_id=subentry.subentry_id,
        )
    for source_id, subentry in sources(entry).items():
        async_add_entities(
            [LastVisitor(entry.entry_id, subentry, data, titles)], config_subentry_id=source_id
        )


class PetExamples(SensorEntity):
    """How many examples a pet has."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "examples"

    def __init__(
        self, entry_id: str, subentry: ConfigSubentry, key: str, store: ExampleStore
    ) -> None:
        self._key = key
        self._store = store
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_examples"
        self._attr_device_info = subentry_device(entry_id, subentry)

    async def async_added_to_hass(self) -> None:
        """Follow the changes of the examples."""
        self.async_on_remove(self._store.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        """The number of examples, of every model."""
        return len(self._store.gallery.of(self._key))


class PendingPictures(JournalEntity, SensorEntity):
    """How many pictures wait for the engine."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "pending_pictures"

    def __init__(self, entry_id: str, data: PetRecognitionData) -> None:
        super().__init__(data.journal)
        self._attr_unique_id = f"{entry_id}_pending_pictures"
        self._attr_device_info = hub_device(entry_id)

    @property
    def native_value(self) -> int:
        """The length of the queue."""
        return len(self._journal.queue)


class LastSeen(JournalEntity, SensorEntity):
    """When a pet was last recognized: the start of that visit."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_seen"

    def __init__(
        self,
        entry_id: str,
        subentry: ConfigSubentry,
        key: str,
        data: PetRecognitionData,
        places: dict[str, str],
    ) -> None:
        super().__init__(data.journal)
        self._key = key
        self._history = data.history
        self._places = places
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_last_seen"
        self._attr_device_info = subentry_device(entry_id, subentry)

    @property
    def native_value(self) -> datetime | None:
        """The start of the latest visit where the pet was recognized."""
        if (record := self._journal.last_of_pet(self._key)) is None:
            return None
        return datetime.fromtimestamp(self._history.start_of(record), UTC)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Where the pet was, and how sure the recognition was."""
        if (record := self._journal.last_of_pet(self._key)) is None:
            return None
        return {
            "source": self._places.get(record.source),
            "confidence": confidence(record, self._key),
        }


def confidence(record: Record, pet: str) -> float | None:
    """The best score of a pet among the animals of a picture."""
    scores = [detection.scores[pet] for detection in record.detections if pet in detection.scores]
    return round(max(scores), 2) if scores else None


class LastVisitor(JournalEntity, SensorEntity):
    """Who came in the latest visit of a source."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "last_visitor"

    def __init__(
        self,
        entry_id: str,
        subentry: ConfigSubentry,
        data: PetRecognitionData,
        titles: dict[str, str],
    ) -> None:
        super().__init__(data.journal)
        self._source = subentry.subentry_id
        self._history = data.history
        self._titles = titles
        self._attr_options = [
            *dict.fromkeys(titles.values()),
            SEVERAL,
            UNKNOWN_ANIMAL,
            OTHER_ANIMAL,
        ]
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_last_visitor"
        self._attr_device_info = subentry_device(entry_id, subentry)

    def _visit(self) -> Visit | None:
        visits = self._history.visits(self._source)
        return visits[-1] if visits else None

    def _pets(self, visit: Visit) -> list[str]:
        return sorted(self._titles[pet] for pet in visit.pets if pet in self._titles)

    @property
    def native_value(self) -> str | None:
        """The pet of the latest visit, or what it was when it was not one pet."""
        if (visit := self._visit()) is None:
            return None
        names = self._pets(visit)
        other = OTHER_ANIMAL in visit.pets
        if len(names) + other > 1:
            return SEVERAL
        if names:
            return names[0]
        return OTHER_ANIMAL if other else UNKNOWN_ANIMAL

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The pets of the latest visit."""
        if (visit := self._visit()) is None:
            return None
        return {"pets": self._pets(visit)}
