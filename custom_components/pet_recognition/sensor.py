"""Sensors of the pets."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetRecognitionConfigEntry
from .const import DOMAIN
from .examples import ExampleStore
from .pets import pets

# Entities only read what the integration holds.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetRecognitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of each pet, to the pet's subentry."""
    store = entry.runtime_data.examples
    for key, subentry in pets(entry).items():
        async_add_entities(
            [PetExamples(entry.entry_id, subentry, key, store)],
            config_subentry_id=subentry.subentry_id,
        )


def pet_device(entry_id: str, subentry: ConfigSubentry) -> DeviceInfo:
    """The device of a pet."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{subentry.subentry_id}")},
        name=subentry.title,
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
        self._attr_device_info = pet_device(entry_id, subentry)

    async def async_added_to_hass(self) -> None:
        """Follow the changes of the examples."""
        self.async_on_remove(self._store.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        """The number of examples, of every model."""
        return len(self._store.gallery.of(self._key))
