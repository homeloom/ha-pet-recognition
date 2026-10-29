"""Learning: what the answers and corrections teach, and what it changes.

A correction relabels pictures of the journal (see `core.corrections`).
When it names a single pet, the animal it chose becomes a learned example
of that pet: its embedding, if the journal still keeps it, else its crop
embedded again by the engine. An example learned before from the same
pictures goes first, since their label changed, so that correcting a
picture again, to unknown or to several pets, takes its example back. A
visit announced as ended is announced again once corrected.

Each time the examples change, the unknown pictures are told again, so that
an example learned from one visit can tell who was at the others, and
settle their questions. The comparisons run in the executor, never in the
event loop, on a copy of the examples. They are made again when the
examples changed meanwhile, and a picture that is no longer unknown by
then keeps what it is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import EXAMPLE_LIMIT
from .coordinator import EngineCoordinator
from .core.corrections import Correction
from .core.matching import Example, Gallery, MatchSettings, Told, Vector, tell
from .counts import VisitCounter
from .engine import EngineError
from .examples import Candidate, ExampleStore, Learned, Origin
from .journal import Journal
from .sources import SourceSettings

_LOGGER = logging.getLogger(__name__)


class Learner:
    """Applies the corrections of one entry, and learns from them."""

    def __init__(
        self,
        coordinator: EngineCoordinator,
        journal: Journal,
        examples: ExampleStore,
        counter: VisitCounter,
    ) -> None:
        self._coordinator = coordinator
        self._journal = journal
        self._examples = examples
        self._counter = counter
        # One correction at a time, so that each learns from what the last left.
        self._lock = asyncio.Lock()

    async def async_correct(self, correction: Correction, origin: Origin) -> None:
        """Relabel the pictures of `correction`, and learn its example, if any."""
        async with self._lock:
            # Before the journal drops the embeddings of the pictures it relabels.
            learned = await self._learned(correction, origin)
            sources = {
                record.source
                for record in self._journal.records
                if record.id in correction.pictures
            }
            await self._journal.async_correct(correction)
            for source in sources:
                self._counter.async_corrected(source, correction.pictures)
            await self._examples.async_relearn(correction.pictures, learned)

    async def _learned(self, correction: Correction, origin: Origin) -> Learned | None:
        if correction.example is None or len(correction.pets) != 1:
            return None
        record = next(
            (record for record in self._journal.records if record.id == correction.example),
            None,
        )
        if record is None or len(record.detections) != 1:
            return None
        (detection,) = record.detections
        try:
            crop = await self._journal.async_read_crop(detection.crop)
        except OSError as err:
            _LOGGER.warning("A correction teaches nothing, as its crop is missing: %s", err)
            return None
        vector, model = detection.vector, detection.model
        if vector is None:
            try:
                embedding = await self._coordinator.client.embed(crop)
            except EngineError as err:
                _LOGGER.warning(
                    "A correction teaches nothing, as the engine cannot embed its crop: %s", err
                )
                return None
            vector, model = embedding.vector, embedding.model
        (pet,) = correction.pets
        return Learned(pet, Candidate(crop, vector, model), origin, record.id)


class Rematcher:
    """Tells the unknown pictures of one entry again when the examples change."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        journal: Journal,
        examples: ExampleStore,
        settings: Mapping[str, SourceSettings],
        match: MatchSettings,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._journal = journal
        self._examples = examples
        self._expected = {source: settings.pets for source, settings in settings.items()}
        self._match = match
        self._running = False
        self._again = False

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Tell the unknown pictures now, as the settings may have changed, and
        each time the examples change."""
        stop = self._examples.async_add_listener(self.async_rematch)
        self.async_rematch()
        return stop

    @callback
    def async_rematch(self) -> None:
        """Tell the unknown pictures again, once the comparisons going on are done."""
        self._again = True
        if self._running:
            return
        self._running = True
        self._entry.async_create_background_task(
            self._hass, self._async_run(), "pet_recognition tell unknown pictures again"
        )

    async def _async_run(self) -> None:
        try:
            while self._again:
                self._again = False
                await self._async_rematch_now()
        finally:
            self._running = False

    async def _async_rematch_now(self) -> None:
        pictures = [
            (record.id, animals, self._expected[record.source])
            for record, animals in self._journal.unknown()
            if record.source in self._expected
        ]
        if not pictures:
            return
        told = await self._hass.async_add_executor_job(
            rematch, pictures, list(self._examples.gallery), self._match
        )
        await self._journal.async_told(told)


def rematch(
    pictures: Iterable[tuple[str, Sequence[tuple[Vector, str]], Sequence[str]]],
    examples: Iterable[Example],
    settings: MatchSettings,
) -> dict[str, Told]:
    """What `examples` tell of each picture, given by ID, with the embeddings
    and models of its animals and the pets expected at its source."""
    gallery = Gallery(limit=EXAMPLE_LIMIT)
    gallery.restore(examples)
    return {
        picture_id: tell(animals, gallery, expected, settings)
        for picture_id, animals, expected in pictures
    }
