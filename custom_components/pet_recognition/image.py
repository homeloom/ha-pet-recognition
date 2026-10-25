"""Images: the last picture of each source, and the last crop of each pet."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetRecognitionConfigEntry
from .entity import JournalEntity, subentry_device
from .journal import Journal, Record
from .pets import pets
from .sources import sources

_LOGGER = logging.getLogger(__name__)

# Entities only read what the integration holds.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetRecognitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the last picture of each pet and each source, to its subentry."""
    journal = entry.runtime_data.journal
    for key, subentry in pets(entry).items():
        async_add_entities(
            [PetPicture(hass, entry.entry_id, subentry, journal, key)],
            config_subentry_id=subentry.subentry_id,
        )
    for source_id, subentry in sources(entry).items():
        async_add_entities(
            [SourcePicture(hass, entry.entry_id, subentry, journal)], config_subentry_id=source_id
        )


def content_type(picture: bytes) -> str:
    """The type of a picture, told by its first bytes; JPEG when unsure."""
    if picture.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if picture[:4] == b"RIFF" and picture[8:12] == b"WEBP":
        return "image/webp"
    if picture.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


class LastPicture(JournalEntity, ImageEntity):
    """A picture of the journal, which changes when a newer one comes."""

    _attr_translation_key = "last_picture"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        subentry: ConfigSubentry,
        journal: Journal,
        record: Callable[[], Record | None],
    ) -> None:
        ImageEntity.__init__(self, hass)
        JournalEntity.__init__(self, journal)
        self._record = record
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_last_picture"
        self._attr_device_info = subentry_device(entry_id, subentry)
        self._update()

    def _update(self) -> None:
        record = self._record()
        self._attr_image_last_updated = (
            None if record is None else datetime.fromtimestamp(record.taken, UTC)
        )

    @callback
    def _async_journal_changed(self) -> None:
        self._update()
        super()._async_journal_changed()


class SourcePicture(LastPicture):
    """The latest picture of a source where an animal was found."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, subentry: ConfigSubentry, journal: Journal
    ) -> None:
        source = subentry.subentry_id
        super().__init__(hass, entry_id, subentry, journal, lambda: journal.last_picture(source))

    async def async_image(self) -> bytes | None:
        """The picture, or nothing when there is none yet."""
        if (record := self._record()) is None:
            return None
        try:
            picture = await self._journal.async_read_frame(record.id)
        except OSError as err:
            _LOGGER.debug("The last picture of a source cannot be read: %s", err)
            return None
        self._attr_content_type = content_type(picture)
        return picture


class PetPicture(LastPicture):
    """The crop of a pet in the latest picture where it was recognized."""

    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        subentry: ConfigSubentry,
        journal: Journal,
        key: str,
    ) -> None:
        self._key = key
        super().__init__(hass, entry_id, subentry, journal, lambda: journal.last_of_pet(key))

    async def async_image(self) -> bytes | None:
        """The crop that resembles the pet most, or nothing when there is none yet."""
        if (record := self._record()) is None or not record.detections:
            return None
        best = max(record.detections, key=lambda detection: detection.scores.get(self._key, -1))
        try:
            return await self._journal.async_read_crop(best.crop)
        except OSError as err:
            _LOGGER.debug("The last crop of a pet cannot be read: %s", err)
            return None
