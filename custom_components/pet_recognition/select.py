"""Selects: correct who was in the latest run of a source."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PetRecognitionConfigEntry, PetRecognitionData
from .const import DOMAIN
from .core.corrections import correct, latest_run
from .core.matching import OTHER_ANIMAL
from .core.visits import Run
from .entity import JournalEntity, ShowsAPicture, subentry_device
from .examples import Origin
from .pets import pets
from .sensor import UNKNOWN_ANIMAL
from .sources import source_settings, sources

# Entities only read what the integration holds, and corrections are made
# one at a time by the integration itself.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetRecognitionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the correction of the latest run of each source, to its subentry."""
    every_pet = pets(entry)
    for source_id, subentry in sources(entry).items():
        expected = {key: every_pet[key].title for key in source_settings(entry, subentry).pets}
        async_add_entities(
            [CorrectLastVisit(entry.entry_id, subentry, entry.runtime_data, expected)],
            config_subentry_id=source_id,
        )


class CorrectLastVisit(ShowsAPicture, JournalEntity, SelectEntity):
    """Who was in the latest run of a source, with its picture: choosing corrects it.

    The options are the pets expected at the source, by name, then the
    unknown and the other animal. A run of several pets shows none.
    """

    _attr_translation_key = "correct_last_visit"

    def __init__(
        self,
        entry_id: str,
        subentry: ConfigSubentry,
        data: PetRecognitionData,
        expected: dict[str, str],
    ) -> None:
        super().__init__(data.journal)
        self._entry_id = entry_id
        self._source = subentry.subentry_id
        self._history = data.history
        self._learner = data.learner
        # By name, the first pet of each name.
        self._keys: dict[str, str] = {}
        for key, title in expected.items():
            self._keys.setdefault(title, key)
        self._names = {key: title for title, key in self._keys.items()}
        self._attr_options = [*self._keys, UNKNOWN_ANIMAL, OTHER_ANIMAL]
        self._attr_unique_id = f"{entry_id}_{subentry.subentry_id}_correct_last_visit"
        self._attr_device_info = subentry_device(entry_id, subentry)

    def _run(self) -> Run | None:
        return latest_run(self._history.visits(self._source))

    @property
    def entity_picture(self) -> str | None:
        """The picture that stands for the latest run."""
        return self._picture(None if (run := self._run()) is None else run.shown)

    @property
    def current_option(self) -> str | None:
        """Who the latest run shows, as an option."""
        if (run := self._run()) is None or len(run.pets) > 1:
            return None
        if not run.pets:
            return UNKNOWN_ANIMAL
        (pet,) = run.pets
        return OTHER_ANIMAL if pet == OTHER_ANIMAL else self._names.get(pet)

    async def async_select_option(self, option: str) -> None:
        """Correct the latest run to `option`, which teaches its animal."""
        if (run := self._run()) is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_visit")
        if option == UNKNOWN_ANIMAL:
            chosen: list[str] = []
        elif option == OTHER_ANIMAL:
            chosen = [OTHER_ANIMAL]
        else:
            chosen = [self._keys[option]]
        await self._learner.async_correct(correct(run, chosen), Origin.CORRECTION)
