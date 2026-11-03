"""The picture that shows each pet, such as on a dashboard.

It is the picture of an entity, such as a sensor of the pet that another
integration shows with the pet's photo, or a photo of the pet kept with the
entry's files: the crop of the pet in an uploaded photo, or of one of its
examples. A photo kept is written before its pet is saved with it, and is
deleted at a later setup once no pet names it.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import ATTR_ENTITY_PICTURE
from homeassistant.core import HomeAssistant, callback

from .const import CONF_PORTRAIT_ENTITY, CONF_PORTRAIT_FILE, DOMAIN
from .examples import data_directory
from .files import delete_unknown_files, file_path, new_id, write_files
from .pets import pets

PORTRAIT_URL = "/api/pet_recognition/portrait/{entry_id}/{file_id}"
# The crops of the engine are JPEG.
SUFFIX = ".jpg"
# The photos being saved, which no pet names yet.
DATA_SAVING = f"{DOMAIN}_portraits_saving"


def portrait_directory(hass: HomeAssistant, entry_id: str) -> Path:
    """Where the photos of the pets of an entry are kept."""
    return data_directory(hass, entry_id) / "portraits"


def portrait_kind(subentry: ConfigSubentry) -> str | None:
    """What shows a pet: "photo", "entity", or nothing."""
    if subentry.data.get(CONF_PORTRAIT_FILE):
        return "photo"
    if subentry.data.get(CONF_PORTRAIT_ENTITY):
        return "entity"
    return None


def portrait_path(entry_id: str, subentry: ConfigSubentry) -> str | None:
    """The path of a pet's photo kept, if it has one."""
    if file_id := subentry.data.get(CONF_PORTRAIT_FILE):
        return PORTRAIT_URL.format(entry_id=entry_id, file_id=file_id)
    return None


def entity_picture(hass: HomeAssistant, entity_id: str) -> str | None:
    """The picture that an entity shows now, if it has one."""
    if (state := hass.states.get(entity_id)) is None:
        return None
    picture = state.attributes.get(ATTR_ENTITY_PICTURE)
    return picture if isinstance(picture, str) and picture else None


def entity_portrait(hass: HomeAssistant, subentry: ConfigSubentry) -> str | None:
    """The picture that a pet's entity shows now, if it has one."""
    if not (entity_id := subentry.data.get(CONF_PORTRAIT_ENTITY)):
        return None
    return entity_picture(hass, entity_id)


def portrait_files(entry: ConfigEntry) -> set[str]:
    """The IDs of the photos that the pets of `entry` name."""
    return {
        file_id
        for subentry in pets(entry).values()
        if (file_id := subentry.data.get(CONF_PORTRAIT_FILE))
    }


async def async_save_portrait(hass: HomeAssistant, entry_id: str, photo: bytes) -> str:
    """Keep a photo of a pet, and give its ID, which stays kept until
    `async_portrait_saved` even if no pet names it."""
    file_id = new_id()
    hass.data.setdefault(DATA_SAVING, set()).add(file_id)
    directory = portrait_directory(hass, entry_id)
    await hass.async_add_executor_job(
        write_files, directory, [(file_path(directory, file_id, SUFFIX), photo)]
    )
    return file_id


@callback
def async_portrait_saved(hass: HomeAssistant, file_id: str) -> None:
    """A photo kept is named by its pet now, or not needed."""
    hass.data.get(DATA_SAVING, set()).discard(file_id)


async def async_keep_portraits(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the photos that no pet of `entry` names, such as those of
    pets removed, or of pets shown otherwise since."""
    known = portrait_files(entry) | hass.data.get(DATA_SAVING, set())
    await hass.async_add_executor_job(
        delete_unknown_files, portrait_directory(hass, entry.entry_id), SUFFIX, known.__contains__
    )


async def async_read_portrait(hass: HomeAssistant, entry_id: str, file_id: str) -> bytes:
    """The photo of a pet, for an ID checked to be one."""
    path = file_path(portrait_directory(hass, entry_id), file_id, SUFFIX)
    return await hass.async_add_executor_job(path.read_bytes)
