"""Whether the engine answers."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PetRecognitionConfigEntry
from .const import DOMAIN, TITLE
from .coordinator import EngineCoordinator

# Entities only read the coordinator.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetRecognitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the engine's connectivity sensor."""
    async_add_entities([EngineConnectivity(entry.runtime_data.coordinator)])


class EngineConnectivity(CoordinatorEntity[EngineCoordinator], BinarySensorEntity):
    """On while the engine answers as an engine of this integration's API."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "engine"

    def __init__(self, coordinator: EngineCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_engine"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=TITLE,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Always: being off is what the sensor tells."""
        return True

    @property
    def is_on(self) -> bool:
        """Whether the last update reached the engine."""
        return self.coordinator.last_update_success
