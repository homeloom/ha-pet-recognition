"""Sensors: what each pet and each source last saw, the visits of the day and
those to review, and the engine's queue."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import PetRecognitionConfigEntry, PetRecognitionData
from .const import CONF_KIND
from .core.matching import OTHER_ANIMAL
from .core.visits import DayCount, Picture, Run, Visit
from .counts import VisitCounter
from .entity import JournalEntity, ShowsAPicture, hub_device, subentry_device
from .examples import ExampleStore
from .journal import Record
from .pets import pets
from .sources import source_settings, sources

# Entities only read what the integration holds.
PARALLEL_UPDATES = 0

# The last visitor when it is not one pet. "unknown" would be read as the
# state of a sensor that knows nothing.
SEVERAL = "several"
UNKNOWN_ANIMAL = "unknown_animal"

# The key of the count of a pet's visits at a source, which ends its unique ID.
PET_VISITS = "visits_today"


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
    registry = er.async_get(hass)
    for source_id, subentry in sources(entry).items():
        expected = {
            pets(entry)[key].subentry_id: key for key in source_settings(entry, subentry).pets
        }
        _remove_orphans(registry, entry.entry_id, source_id, expected)
        async_add_entities(
            [
                LastVisitor(entry.entry_id, subentry, data, titles),
                VisitsToday(entry.entry_id, subentry, data.counter),
                VisitsToReview(entry.entry_id, subentry, data),
                *(
                    PetVisitsToday(entry.entry_id, subentry, data.counter, pets(entry)[key])
                    for key in expected.values()
                ),
            ],
            config_subentry_id=source_id,
        )


def _remove_orphans(
    registry: er.EntityRegistry, entry_id: str, source_id: str, expected: dict[str, str]
) -> None:
    """Remove the counts of a source for pets that are gone or no longer expected there.

    They belong to the source's subentry, so removing the pet leaves them.
    """
    prefix = f"{entry_id}_{source_id}_"
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if not entity.unique_id.startswith(prefix):
            continue
        pet, _, key = entity.unique_id.removeprefix(prefix).partition("_")
        if key == PET_VISITS and pet not in expected:
            registry.async_remove(entity.entity_id)


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


class LastVisitor(ShowsAPicture, JournalEntity, SensorEntity):
    """Who came in the latest visit of a source, with its picture."""

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
        self._entry_id = entry_id
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
    def entity_picture(self) -> str | None:
        """The picture that stands for the latest visit."""
        return self._picture(_shown(self._visit()))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The pets of the latest visit."""
        if (visit := self._visit()) is None:
            return None
        return {"pets": self._pets(visit)}


class VisitsToReview(ShowsAPicture, SensorEntity):
    """The visits of a source that ended unsure, among the pictures kept,
    with the picture of the latest."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "visits_to_review"

    def __init__(self, entry_id: str, subentry: ConfigSubentry, data: PetRecognitionData) -> None:
        self._entry_id = entry_id
        self._source = subentry.subentry_id
        self._journal = data.journal
        self._history = data.history
        self._counter = data.counter
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_visits_to_review"
        self._attr_device_info = subentry_device(entry_id, subentry)

    async def async_added_to_hass(self) -> None:
        """Follow the visits that end, and the corrections."""
        await super().async_added_to_hass()
        self.async_on_remove(self._counter.async_add_listener(self.async_write_ha_state))

    def _visits(self) -> list[Visit]:
        ended = self._history.ended(self._source, dt_util.utcnow().timestamp())
        return [visit for visit in ended if visit.unsure]

    @property
    def native_value(self) -> int:
        """How many visits are left unsure."""
        return len(self._visits())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The latest of them, to correct with the `assign_visit` action."""
        visits = self._visits()
        return {"latest_visit_id": visits[-1].id if visits else None}

    @property
    def entity_picture(self) -> str | None:
        """The picture that stands for the latest of them."""
        visits = self._visits()
        return self._picture(_shown(visits[-1] if visits else None))


def _shown(visit: Visit | None) -> Picture | None:
    """The picture that stands for a visit, as a question shows it."""
    return None if visit is None else Run(list(visit.pictures), visit.pets).shown


class DayCountEntity(SensorEntity):
    """A count of the visits of a source that started today, reset at midnight."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self, source: str, counter: VisitCounter, value: Callable[[DayCount], int]
    ) -> None:
        self._source = source
        self._counter = counter
        self._value = value

    async def async_added_to_hass(self) -> None:
        """Follow the visits that end, and the days."""
        self.async_on_remove(self._counter.async_add_listener(self._async_counted))

    @callback
    def _async_counted(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        """The visits of today that ended."""
        return self._value(self._counter.today(self._source))

    @property
    def last_reset(self) -> datetime:
        """The start of today."""
        return dt_util.start_of_local_day()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The count of yesterday."""
        return {"yesterday": self._value(self._counter.yesterday(self._source))}


class VisitsToday(DayCountEntity):
    """The visits of a source today, unknown ones included."""

    _attr_translation_key = "visits_today"

    def __init__(self, entry_id: str, subentry: ConfigSubentry, counter: VisitCounter) -> None:
        super().__init__(subentry.subentry_id, counter, lambda counted: counted.visits)
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_visits_today"
        self._attr_device_info = subentry_device(entry_id, subentry)


class PetVisitsToday(DayCountEntity):
    """The visits of one pet to a source today: its meals, at a feeder."""

    def __init__(
        self,
        entry_id: str,
        subentry: ConfigSubentry,
        counter: VisitCounter,
        pet: ConfigSubentry,
    ) -> None:
        key = pet.unique_id or ""
        super().__init__(subentry.subentry_id, counter, lambda counted: counted.pets.get(key, 0))
        self._attr_translation_key = (
            "pet_meals_today" if subentry.data[CONF_KIND] == "feeder" else "pet_visits_today"
        )
        self._attr_translation_placeholders = {"pet": pet.title}
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_{pet.subentry_id}_{PET_VISITS}"
        self._attr_device_info = subentry_device(entry_id, subentry)
