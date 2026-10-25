"""The devices of the integration, and the entities that show the journal."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, TITLE
from .journal import Journal


def hub_device(entry_id: str) -> DeviceInfo:
    """The device of the engine and of what concerns every source."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)}, name=TITLE, entry_type=DeviceEntryType.SERVICE
    )


def subentry_device(entry_id: str, subentry: ConfigSubentry) -> DeviceInfo:
    """The device of a pet or a source."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{subentry.subentry_id}")}, name=subentry.title
    )


class JournalEntity(Entity):
    """An entity whose state comes from the journal, written again when it changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    async def async_added_to_hass(self) -> None:
        """Follow the changes of the journal."""
        self.async_on_remove(self._journal.async_add_listener(self._async_journal_changed))

    @callback
    def _async_journal_changed(self) -> None:
        self.async_write_ha_state()
