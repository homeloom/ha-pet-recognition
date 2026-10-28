"""Sources: take each new picture of their image entities, and analyse it.

A source is a set of image or camera entities, such as a feeder's snapshot.
Cameras take pictures when their trigger fires (see `cameras`). Each time
an image entity shows a new picture, the picture is queued for the engine. A
picture is new when the entity's state, the time of its last picture,
differs from the last one taken, so that a restart or a change of
attributes takes nothing twice. Its SHA-256 is the last guard: a picture
the source already has, waiting or recorded, such as the same picture shown
again after a restart of the entity's own integration, adds nothing. The
pictures of an entity are taken one at a time, in order.

One picture is analysed at a time, oldest first. While the engine is busy or
cannot be reached, the queue waits, longer after each failure; the engine
answering its health check again wakes it up. A picture the engine cannot
read, or that fails again and again while the engine is otherwise healthy,
is recorded as such, so that it never blocks the queue.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.camera.const import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.image import DOMAIN as IMAGE_DOMAIN
from homeassistant.components.image import async_get_image
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTITIES,
    CONF_EVERY_PICTURE,
    CONF_FRAMES,
    CONF_GAP,
    CONF_INTERVAL,
    CONF_LATE,
    CONF_PETS,
    CONF_SPECIES,
    CONF_TRIGGER,
    DETECTION_LIMIT,
    MIN_AREA,
    SUBENTRY_SOURCE,
)
from .coordinator import EngineCoordinator
from .core.matching import MatchSettings, Nearest, decide, resembles
from .core.visits import VisitSettings
from .engine import (
    Analysis,
    EngineAuthError,
    EngineBusyError,
    EngineConnectionError,
    EngineError,
    EngineRejectedError,
)
from .examples import ExampleStore, stored_vector
from .files import new_id
from .journal import Detection, Journal, Pending, Record, Seen, Status
from .pets import UNUSABLE_CROP, pets

_LOGGER = logging.getLogger(__name__)

# Seconds before trying the engine again, doubled after each failure.
FIRST_RETRY = 5
LAST_RETRY = 300
# Failures of the engine on one picture before it is given up.
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class SourceSettings:
    """What a source watches, which pets may appear there, and how its visits form."""

    entities: tuple[str, ...]
    # The keys of the pets expected at the source.
    pets: tuple[str, ...]
    visits: VisitSettings
    # What makes the cameras take pictures: how many each, and how far apart.
    trigger: str | None
    frames: int
    interval: float

    @property
    def images(self) -> tuple[str, ...]:
        """The image entities, whose new pictures are taken."""
        return tuple(entity for entity in self.entities if _domain(entity) == IMAGE_DOMAIN)

    @property
    def cameras(self) -> tuple[str, ...]:
        """The camera entities, which take pictures when the trigger fires."""
        return tuple(cameras(self.entities))


def cameras(entities: Iterable[str]) -> list[str]:
    """The camera entities among `entities`, which take pictures on a trigger."""
    return [entity for entity in entities if _domain(entity) == CAMERA_DOMAIN]


def _domain(entity_id: str) -> str:
    return split_entity_id(entity_id)[0]


def sources(entry: ConfigEntry) -> dict[str, ConfigSubentry]:
    """The source subentries of `entry`, by subentry ID."""
    return {
        subentry_id: subentry
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_SOURCE
    }


def source_settings(entry: ConfigEntry, subentry: ConfigSubentry) -> SourceSettings:
    """The settings of a source.

    It expects the pets it was given that still exist, or every pet when it
    was given none, or when they are all gone.
    """
    data = subentry.data
    every_pet = list(pets(entry))
    chosen = [key for key in data[CONF_PETS] if key in every_pet]
    return SourceSettings(
        entities=tuple(data[CONF_ENTITIES]),
        pets=tuple(chosen or every_pet),
        visits=VisitSettings(
            gap=data[CONF_GAP] * 60,
            late=data[CONF_LATE] * 60,
            every_picture_shows_a_pet=data[CONF_EVERY_PICTURE],
        ),
        trigger=data[CONF_TRIGGER],
        frames=data[CONF_FRAMES],
        interval=data[CONF_INTERVAL],
    )


class SourceWatcher:
    """Queues the new pictures of the image entities of every source."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        journal: Journal,
        on_new: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._journal = journal
        self._on_new = on_new
        # The source of each entity; an entity belongs to one source.
        self._sources: dict[str, str] = {}
        # The latest state of each entity not taken yet, and the entities
        # being taken.
        self._latest: dict[str, State] = {}
        self._taking: set[str] = set()
        for source_id, subentry in sources(entry).items():
            for entity_id in source_settings(entry, subentry).images:
                self._sources.setdefault(entity_id, source_id)

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Follow the entities, and take what they show since they were last seen."""
        unsubscribe = async_track_state_change_event(
            self._hass, list(self._sources), self._async_changed
        )
        for entity_id in self._sources:
            self._take(entity_id, self._hass.states.get(entity_id))
        return unsubscribe

    @callback
    def _async_changed(self, event: Event[EventStateChangedData]) -> None:
        self._take(event.data["entity_id"], event.data["new_state"])

    @callback
    def _take(self, entity_id: str, state: State | None) -> None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        self._latest[entity_id] = state
        if entity_id in self._taking:
            # Taken next, once the picture being taken is.
            return
        self._taking.add(entity_id)
        self._entry.async_create_background_task(
            self._hass, self._async_take(entity_id), f"pet_recognition take {entity_id}"
        )

    async def _async_take(self, entity_id: str) -> None:
        """Take the pictures of an entity, until none is left to take."""
        try:
            while (state := self._latest.pop(entity_id, None)) is not None:
                seen = self._journal.seen.get(entity_id)
                if seen is not None and seen.state == state.state:
                    # Only its attributes changed, or it was taken before a restart.
                    continue
                await self._async_take_one(entity_id, state, seen)
        finally:
            self._taking.discard(entity_id)

    async def _async_take_one(self, entity_id: str, state: State, seen: Seen | None) -> None:
        try:
            image = await async_get_image(self._hass, entity_id)
        except (HomeAssistantError, TimeoutError) as err:
            _LOGGER.debug("Cannot take the picture of %s: %s", entity_id, err)
            return
        source_id = self._sources[entity_id]
        sha256 = hashlib.sha256(image.content).hexdigest()
        now = Seen(entity_id, state.state, sha256)
        if seen is None or self._journal.known(source_id, sha256) or seen.sha256 == sha256:
            # The picture an entity showed when its source was added is not
            # new, and neither is a picture the source already has.
            self._journal.async_see(now)
            return
        pending = Pending(new_id(), source_id, entity_id, _taken(state), sha256)
        await self._journal.async_add_pending(pending, image.content, now)
        self._on_new()


def _taken(state: State) -> float:
    """When the picture was taken, as far as Home Assistant knows."""
    taken: datetime = dt_util.parse_datetime(state.state) or state.last_changed
    return taken.timestamp()


class Analyzer:
    """Sends the waiting pictures to the engine, one at a time, and records them."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: EngineCoordinator,
        journal: Journal,
        examples: ExampleStore,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._journal = journal
        self._examples = examples
        self._running = False
        self._retry: CALLBACK_TYPE | None = None
        self._delay = FIRST_RETRY
        # Whether the engine answered its last health check.
        self._answered = True

    @callback
    def async_wake(self) -> None:
        """Analyse the waiting pictures, unless already doing so or waiting to retry."""
        if self._running or self._retry is not None or not self._journal.queue:
            return
        self._running = True
        self._entry.async_create_background_task(
            self._hass, self._async_run(), "pet_recognition analyze pictures"
        )

    @callback
    def async_engine_updated(self) -> None:
        """Try again at once when the engine answers its health check again."""
        answers = self._coordinator.last_update_success
        if answers and not self._answered and self._retry is not None:
            self._retry()
            self._retry = None
        self._answered = answers
        self.async_wake()

    @callback
    def async_stop(self, _event: Event | None = None) -> None:
        """Wait no more to try again, such as when Home Assistant stops."""
        if self._retry is not None:
            self._retry()
            self._retry = None

    async def _async_run(self) -> None:
        try:
            while self._journal.queue and await self._async_analyze(self._journal.queue[0]):
                pass
        finally:
            self._running = False

    async def _async_analyze(self, pending: Pending) -> bool:
        """Analyse one picture, and say whether to go on with the next one."""
        subentry = self._entry.subentries.get(pending.source)
        if subentry is None or subentry.subentry_type != SUBENTRY_SOURCE:
            await self._journal.async_drop(pending)
            return True
        info = self._coordinator.data
        if info is None or not self._coordinator.last_update_success:
            # The next successful update wakes the queue up.
            return False
        try:
            picture = await self._journal.async_read_frame(pending.id)
        except OSError as err:
            _LOGGER.warning("A waiting picture is missing, and is dropped: %s", err)
            await self._journal.async_drop(pending)
            return True
        if len(picture) > info.max_bytes:
            await self._journal.async_record(pending, _record(pending, Status.UNREADABLE))
            return True
        settings = source_settings(self._entry, subentry)
        wanted = {
            pet.data[CONF_SPECIES] for key, pet in pets(self._entry).items() if key in settings.pets
        }
        try:
            analysis = await self._coordinator.client.analyze(
                picture,
                min_area=MIN_AREA,
                limit=DETECTION_LIMIT,
                species=frozenset(wanted & info.species or info.species),
            )
        except EngineRejectedError as err:
            if err.code in UNUSABLE_CROP:
                await self._journal.async_record(pending, _record(pending, Status.UNREADABLE))
                return True
            return await self._async_failed(pending, err)
        except EngineAuthError:
            # The health check raises the repair; its next success wakes the queue.
            await self._coordinator.async_request_refresh()
            return False
        except EngineBusyError as err:
            self._later(err.retry_after)
            return False
        except EngineConnectionError as err:
            # The engine is down, or fails on this picture: its health tells.
            await self._coordinator.async_refresh()
            if self._coordinator.last_update_success:
                return await self._async_failed(pending, err)
            self._later()
            return False
        except EngineError as err:
            return await self._async_failed(pending, err)
        record, crops = self._match(pending, analysis, settings)
        await self._journal.async_record(pending, record, crops)
        self._delay = FIRST_RETRY
        return True

    async def _async_failed(self, pending: Pending, err: EngineError) -> bool:
        self._journal.async_retry(pending)
        if pending.attempts < MAX_ATTEMPTS:
            _LOGGER.debug("The engine failed on a picture, which waits: %s", err)
            self._later()
            return False
        _LOGGER.warning(
            "The engine failed on a picture %s times, and it is given up: %s", MAX_ATTEMPTS, err
        )
        await self._journal.async_record(pending, _record(pending, Status.FAILED))
        return True

    @callback
    def _later(self, retry_after: float | None = None) -> None:
        """Try again later: when the engine asks, or longer after each failure."""
        if retry_after is not None:
            delay = max(retry_after, 1)
        else:
            delay = self._delay
            self._delay = min(self._delay * 2, LAST_RETRY)
        self._retry = async_call_later(self._hass, delay, self._async_retry)

    @callback
    def _async_retry(self, _now: datetime) -> None:
        self._retry = None
        self.async_wake()

    def _match(
        self, pending: Pending, analysis: Analysis, settings: SourceSettings
    ) -> tuple[Record, list[tuple[str, bytes]]]:
        """Which pets the animals of a picture are, as far as their examples tell."""
        expected = list(settings.pets)
        record = _record(pending, Status.ANALYZED)
        crops: list[tuple[str, bytes]] = []
        decided: list[str | None] = []
        for animal in analysis.animals:
            vector = stored_vector(animal.embedding)
            scores = Nearest.compute(vector, analysis.model, self._examples.gallery).scores()
            decided.append(decide(scores, expected, MatchSettings()))
            crop = new_id()
            crops.append((crop, animal.crop))
            record.detections.append(
                Detection(
                    species=animal.species,
                    confidence=animal.score,
                    area=animal.area,
                    crop=crop,
                    model=analysis.model,
                    scores=scores,
                    vector=vector,
                )
            )
        if decided and all(decided):
            record.pets = tuple(sorted({pet for pet in decided if pet}))
            # A picture decided needs its embeddings no more.
            for detection in record.detections:
                detection.vector = None
        if len(record.detections) == 1:
            record.resembles = resembles(record.detections[0].scores, expected)
        return record, crops


def _record(pending: Pending, status: Status) -> Record:
    return Record(
        id=pending.id,
        source=pending.source,
        entity=pending.entity,
        taken=pending.taken,
        sha256=pending.sha256,
        status=status,
    )
