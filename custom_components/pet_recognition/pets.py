"""Pets: turning a picture into an example, and keeping the examples usable.

A picture becomes an example when the engine finds exactly one animal of the
pet's species in it. When the engine's model changes, the examples of the old
model are embedded again from their crops, one at a time, so that matching
never compares embeddings of two models. The examples the user gave go
first, so that a learned example that turns out to be the same picture is
the one dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.camera import async_get_image as async_get_camera_image
from homeassistant.components.image import async_get_image as async_get_entity_image
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DETECTION_LIMIT, DOMAIN, MIN_AREA, SUBENTRY_PET
from .coordinator import EngineCoordinator
from .engine import EngineBusyError, EngineConnectionError, EngineError, EngineRejectedError
from .examples import Candidate, ExampleStore

_LOGGER = logging.getLogger(__name__)

ISSUE_PET_WITHOUT_EXAMPLES = "pet_without_examples"

# What the engine says of a crop it cannot embed.
UNUSABLE_CROP = frozenset({"too_large", "unreadable_image"})


class PictureError(Exception):
    """A picture that cannot become an example, with the form error to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def pets(entry: ConfigEntry) -> dict[str, ConfigSubentry]:
    """The pet subentries of `entry`, by the key of their examples."""
    return {
        subentry.unique_id: subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_PET and subentry.unique_id
    }


async def async_candidate(
    coordinator: EngineCoordinator, picture: bytes, species: str
) -> Candidate:
    """The one animal of `species` in `picture`, as an example to add."""
    info = coordinator.data
    if info is None:
        raise PictureError("cannot_connect")
    if len(picture) > info.max_bytes:
        raise PictureError("picture_too_large")
    try:
        analysis = await coordinator.client.analyze(
            picture, min_area=MIN_AREA, limit=DETECTION_LIMIT, species=frozenset({species})
        )
    except EngineRejectedError as err:
        if err.code == "too_large":
            raise PictureError("picture_too_large") from err
        if err.code == "unreadable_image":
            raise PictureError("unreadable_picture") from err
        raise PictureError("cannot_analyze") from err
    except EngineConnectionError as err:
        raise PictureError("cannot_connect") from err
    except EngineBusyError as err:
        raise PictureError("engine_busy") from err
    except EngineError as err:
        raise PictureError("cannot_analyze") from err
    animals = [animal for animal in analysis.animals if animal.species == species]
    if not animals:
        raise PictureError("no_animal")
    if len(animals) > 1:
        raise PictureError("several_animals")
    (animal,) = animals
    return Candidate(crop=animal.crop, vector=animal.embedding, model=analysis.model)


async def async_capture(hass: HomeAssistant, entity_id: str) -> bytes:
    """The picture a camera or image entity shows now."""
    try:
        if entity_id.startswith("camera."):
            image = await async_get_camera_image(hass, entity_id)
        else:
            image = await async_get_entity_image(hass, entity_id)
    except (HomeAssistantError, TimeoutError) as err:
        _LOGGER.debug("Cannot take a picture from %s: %s", entity_id, err)
        raise PictureError("cannot_capture") from err
    return image.content


@callback
def async_check_examples(hass: HomeAssistant, entry: ConfigEntry, store: ExampleStore) -> None:
    """Raise a repair for each pet without examples, and clear the others."""
    with_examples = store.gallery.pets()
    for key, subentry in pets(entry).items():
        issue_id = f"{ISSUE_PET_WITHOUT_EXAMPLES}_{subentry.subentry_id}"
        if key in with_examples:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PET_WITHOUT_EXAMPLES,
            translation_placeholders={"pet": subentry.title},
        )


@callback
def async_delete_pet_issues(hass: HomeAssistant) -> None:
    """Remove the repairs about pets."""
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(f"{ISSUE_PET_WITHOUT_EXAMPLES}_"):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@dataclass
class Reembedder:
    """Embeds again, with the engine's model, the examples of another model."""

    coordinator: EngineCoordinator
    store: ExampleStore
    running: bool = False

    def needed(self) -> str | None:
        """The engine's model, if some examples are of another one."""
        info = self.coordinator.data
        if info is None or all(example.model == info.model for example in self.store.gallery):
            return None
        return info.model

    async def async_run(self, model: str) -> None:
        """Embed the examples of other models again with `model`, until the engine fails.

        The next update of the coordinator starts again what is left.
        """
        self.running = True
        try:
            while example := min(
                (example for example in self.store.gallery if example.model != model),
                key=lambda example: (not example.uploaded, example.added, example.id),
                default=None,
            ):
                try:
                    crop = await self.store.async_read_crop(example.id)
                    embedding = await self.coordinator.client.embed(crop)
                except (OSError, EngineRejectedError) as err:
                    if isinstance(err, EngineRejectedError) and err.code not in UNUSABLE_CROP:
                        _LOGGER.debug("Cannot embed the examples again yet: %s", err)
                        return
                    if self.store.gallery.get(example.id) is not None:
                        # Without a usable crop, the example can never match again.
                        _LOGGER.warning("Removing an example whose crop cannot be used: %s", err)
                        await self.store.async_remove([example.id])
                    continue
                except EngineError as err:
                    _LOGGER.debug("Cannot embed the examples again yet: %s", err)
                    return
                if embedding.model != model:
                    # The engine changed again meanwhile: wait for its next update.
                    return
                await self.store.async_replace(example.id, embedding.vector, embedding.model)
        finally:
            self.running = False
