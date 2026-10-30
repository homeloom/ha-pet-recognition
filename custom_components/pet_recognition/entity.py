"""The devices of the integration, and the entities that show the journal."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import ATTR_ENTITY_PICTURE
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, TITLE
from .core.visits import Picture
from .journal import Journal
from .views import picture_paths, signed_entity_picture

# How often the picture of an entity is signed again, before it expires.
SIGN_AGAIN = timedelta(hours=1)


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


class ShowsAPicture(Entity):
    """An entity of a source whose picture is that of the visit it shows:
    the crop of its animal, or the picture itself when it has none, if kept.

    Its URL is signed, so that any dashboard can show it, and signed again
    each hour, before it expires. It is left out of the recorder.
    """

    _unrecorded_attributes = frozenset({ATTR_ENTITY_PICTURE})
    _entry_id: str
    _source: str
    _journal: Journal

    async def async_added_to_hass(self) -> None:
        """Sign the picture again each hour."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_sign_again, SIGN_AGAIN, cancel_on_shutdown=True
            )
        )

    @callback
    def _async_sign_again(self, _now: datetime) -> None:
        self.async_write_ha_state()

    def _picture(self, shown: Picture | None) -> str | None:
        """The URL of what shows the picture `shown`, if any."""
        if shown is None:
            return None
        path = picture_paths(self._entry_id, self._journal, self._source).get(shown.id)
        return None if path is None else signed_entity_picture(self.hass, path)
