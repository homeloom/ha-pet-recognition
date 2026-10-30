"""The journal: what each source saw, and the pictures waiting for the engine.

Each picture of a source becomes a record: when it was taken, its SHA-256,
what the engine found, and which pets it shows. The pictures waiting for the
engine form a queue that survives restarts. Both are kept in a store of
Home Assistant, saved a few seconds after a picture is taken, at once when a
change deletes files, and when Home Assistant stops.

A waiting picture is a file under `frames/`, and each animal found is a
JPEG crop under `crops/`, both in `<config>/pet_recognition/<entry_id>/`.
A file is written before the store names it, and deleted only once the
store no longer names it, so that a crash leaves at worst a file that is
named nowhere. Those are deleted each time the entry loads.

A picture is deleted once analyzed, except the latest picture of each
source where an animal was found, which its last picture entity shows until
a newer one comes. A picture where no animal is found, such as a garden
camera's picture of people passing by, is not kept, except at a source that
only takes pictures of a pet, such as a feeder: there the picture is all
there is to show of a visit where no animal was found, so it stays as long
as its record, and the latest picture of such a source is its latest
picture, with an animal or not.

A correction relabels pictures with the pets the user named, or with none
to say they are unknown, and keeps them as a group of their own. They keep
their embeddings no more: the examples cannot change them any more.

Embeddings are kept only while a picture is unknown, which is when they can
still change its pets, and only for the latest unknown pictures of each
source. The pictures of a source are limited in number and age, so the
journal stays small. The latest picture of each source, and the latest
picture where each pet was recognized, stay past both limits, so that
what was last seen is still known. So do the pictures the entry's history
protects: the whole visits its entities show, and the visits that have not
ended before the age limit, so that a visit goes as a whole.

The journal also remembers, for each source, the start of the latest visit
announced as ended, so that a visit is announced once, even across a
restart, and the questions asked on the phones, with the phones each was
sent to. A question is saved at once when it is asked, before it is sent,
so that a restart never asks it twice, and forgotten once it is closed and
none of its pictures is kept.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, PICTURE_LIMIT, QUEUE_LIMIT
from .core.corrections import Correction
from .core.doubts import Doubt, Kind, State
from .core.matching import OTHER_ANIMAL, Told
from .examples import data_directory, decode_vector, encode_vector
from .files import FILE_ID, delete_files, delete_unknown_files, file_path, write_files

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Seconds between a change and its save, so that a burst of pictures is
# saved once.
SAVE_DELAY = 2
# Unknown pictures of a source that keep their embeddings, latest first.
VECTOR_LIMIT = 200

FRAME = ".frame"
CROP = ".jpg"

DATA_JOURNALS: HassKey[dict[str, Journal]] = HassKey(f"{DOMAIN}_journal")


class Status(StrEnum):
    """What became of a picture."""

    # The engine analysed it.
    ANALYZED = "analyzed"
    # The engine cannot read it, or it is too large for the engine.
    UNREADABLE = "unreadable"
    # The engine failed on it, again and again.
    FAILED = "failed"


@dataclass
class Seen:
    """The last picture taken from an entity, to tell a new one."""

    entity: str
    state: str
    sha256: str


@dataclass
class Pending:
    """A picture waiting for the engine."""

    id: str
    source: str
    entity: str
    taken: float
    sha256: str
    attempts: int = 0


@dataclass
class Detection:
    """An animal found in a picture, and how much it resembles each pet."""

    species: str
    confidence: float
    area: float
    crop: str
    model: str
    scores: dict[str, float] = field(default_factory=dict)
    # Kept while the picture is unknown.
    vector: tuple[float, ...] | None = None


@dataclass
class Record:
    """A picture of a source, and what was found in it."""

    id: str
    source: str
    entity: str
    taken: float
    sha256: str
    status: Status
    detections: list[Detection] = field(default_factory=list)
    pets: tuple[str, ...] = ()
    resembles: str | None = None
    # Corrected to `pets`, or to unknown when it has none, with the pictures
    # of its group.
    corrected: bool = False
    group: str = ""


@dataclass
class Asked:
    """A question asked on the phones, and where it stands.

    `taken` is when the picture shown was taken, and `crop` the crop the
    notification shows, if that picture has an animal; `choices` the pets
    of its buttons; `phones` the devices to notify, and `sent` those it was
    sent to.
    """

    doubt: Doubt
    taken: float
    crop: str | None
    choices: tuple[str, ...]
    phones: tuple[str, ...]
    sent: tuple[str, ...] = ()

    @property
    def open(self) -> bool:
        """The question waits for an answer."""
        return self.doubt.state is State.OPEN


async def async_get_journal(
    hass: HomeAssistant, entry_id: str, every_picture: Collection[str] = ()
) -> Journal:
    """The journal of an entry, loaded the first time and kept across reloads.

    `every_picture` names the sources that only take pictures of a pet,
    whose pictures without an animal are kept. Each time, the files the
    journal does not name are deleted, such as a picture whose taking an
    unload interrupted.
    """
    journals = hass.data.setdefault(DATA_JOURNALS, {})
    if (journal := journals.get(entry_id)) is None:
        journal = Journal(hass, entry_id)
        await journal.async_load()
        journals[entry_id] = journal
    journal.every_picture = frozenset(every_picture)
    await journal.async_delete_unknown_files()
    return journal


async def async_remove_journal(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the journal of a deleted entry, and any save still to come."""
    journal = hass.data.get(DATA_JOURNALS, {}).pop(entry_id, None) or Journal(hass, entry_id)
    await journal.async_remove()


class Journal:
    """The pictures of the sources of one entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.journal", private=True
        )
        directory = data_directory(hass, entry_id)
        self.frames = directory / "frames"
        self.crops = directory / "crops"
        self.seen: dict[str, Seen] = {}
        self.queue: list[Pending] = []
        self.records: list[Record] = []
        # By source, the start of the latest visit announced as ended.
        self.announced: dict[str, float] = {}
        self.asked: list[Asked] = []
        # The sources that only take pictures of a pet.
        self.every_picture: frozenset[str] = frozenset()
        # The stored form of each record, by ID, made again only when it changes.
        self._rows: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._protect: Callable[[float | None], set[str]] = _nothing

    async def async_load(self) -> None:
        """Read the journal."""
        stored = await self._store.async_load()
        if stored is not None and not isinstance(stored, dict):
            _LOGGER.warning("The journal cannot be read, and is ignored")
            stored = None
        stored = stored or {}
        self.seen = {seen.entity: seen for seen in _read_list(stored, "seen", _seen)}
        self.queue = _read_list(stored, "queue", _pending)
        self.records = _read_list(stored, "pictures", _record)
        self.announced = _read_announced(stored)
        self.asked = _read_list(stored, "questions", _asked)

    async def async_delete_unknown_files(self) -> None:
        """Delete the files the journal does not name."""
        frames = {pending.id for pending in self.queue} | self.kept_frames()
        crops = {detection.crop for record in self.records for detection in record.detections}
        await self._hass.async_add_executor_job(self._delete_unknown_files, frames, crops)

    async def async_remove(self) -> None:
        """Remove the stored journal; the caller deletes the files."""
        await self._store.async_remove()

    def frame_path(self, pending_id: str) -> Path:
        """The file of a waiting picture."""
        return file_path(self.frames, pending_id, FRAME)

    def crop_path(self, crop_id: str) -> Path:
        """The file of the crop of an animal found."""
        return file_path(self.crops, crop_id, CROP)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `listener` after each change of the queue or the records."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_protect(self, protect: Callable[[float | None], set[str]]) -> CALLBACK_TYPE:
        """Keep past the limits the records `protect` names, given the age limit, if any."""
        self._protect = protect

        @callback
        def _async_stop() -> None:
            self._protect = _nothing

        return _async_stop

    @callback
    def async_see(self, seen: Seen) -> None:
        """Remember the last picture taken from an entity."""
        self.seen[seen.entity] = seen
        self._save()

    async def async_add_pending(
        self, pending: Pending, picture: bytes, seen: Seen | None = None
    ) -> None:
        """Queue a picture for the engine, taken as `seen` for an image entity.

        The picture is written first, so that an interruption leaves the
        entity as not taken. The oldest pictures go when the queue is full.
        """
        await self._hass.async_add_executor_job(
            write_files, self.frames, [(self.frame_path(pending.id), picture)]
        )
        if seen is not None:
            self.seen[seen.entity] = seen
        self.queue.append(pending)
        dropped = self.queue[:-QUEUE_LIMIT]
        if not dropped:
            self._save()
            self._notify()
            return
        _LOGGER.warning(
            "The queue of pictures for the engine is full, and its %s oldest are dropped",
            len(dropped),
        )
        del self.queue[:-QUEUE_LIMIT]
        await self._changed(frames=[pending.id for pending in dropped])

    async def async_read_frame(self, pending_id: str) -> bytes:
        """A waiting picture, or the last picture of a source."""
        return await self._hass.async_add_executor_job(self.frame_path(pending_id).read_bytes)

    async def async_read_crop(self, crop_id: str) -> bytes:
        """The crop of an animal found."""
        return await self._hass.async_add_executor_job(self.crop_path(crop_id).read_bytes)

    def last_picture(self, source: str) -> Record | None:
        """The latest picture of a source whose file is kept: where an animal
        was found, or any at a source that only takes pictures of a pet."""
        return self._last_pictures().get(source)

    def kept_frames(self) -> set[str]:
        """The records whose picture is kept: the latest picture of each
        source, and the pictures without an animal of the sources that only
        take pictures of a pet."""
        return {record.id for record in self._last_pictures().values()} | {
            record.id
            for record in self.records
            if not record.detections and record.source in self.every_picture
        }

    def last_of_pet(self, pet: str) -> Record | None:
        """The latest picture where `pet` was recognized."""
        return max(
            (record for record in self.records if pet in record.pets),
            key=_order,
            default=None,
        )

    def known(self, source: str, sha256: str) -> bool:
        """Whether a source already has a picture, waiting or recorded."""
        return any(
            item.source == source and item.sha256 == sha256 for item in (*self.queue, *self.records)
        )

    @callback
    def async_announce(self, source: str, start: float) -> None:
        """Remember the start of the latest visit of a source announced as ended."""
        self.announced[source] = start
        self._save()

    def doubts(self, source: str) -> list[Doubt]:
        """The questions asked about a source."""
        return [asked.doubt for asked in self.asked if asked.doubt.source == source]

    def question(self, doubt: Doubt) -> Asked:
        """The question of a doubt."""
        return next(asked for asked in self.asked if asked.doubt is doubt)

    async def async_ask(self, asked: Asked) -> None:
        """Remember a question about to be sent, saved at once."""
        self.asked.append(asked)
        await self._store.async_save(self._data())

    @callback
    def async_sent(self, asked: Asked, phone: str) -> None:
        """Remember that a question was sent to a phone."""
        asked.sent = (*asked.sent, phone)
        self._save()

    @callback
    def async_closed(self) -> None:
        """Save the questions that were closed, and forget those without pictures."""
        self._forget_questions()
        self._save()

    async def async_correct(self, correction: Correction) -> None:
        """Relabel the pictures of a correction, which need their embeddings no more."""
        for record in self.records:
            if record.id in correction.pictures:
                record.pets = correction.pets
                record.corrected = True
                record.group = correction.group
                for detection in record.detections:
                    detection.vector = None
                self._rows.pop(record.id, None)
        await self._changed()

    def unknown(self) -> list[tuple[Record, list[tuple[tuple[float, ...], str]]]]:
        """The pictures that the examples may still tell, with the embedding
        and model of each animal: unknown, not corrected, and that keep their
        embeddings."""
        return [
            (
                record,
                [
                    (detection.vector, detection.model)
                    for detection in record.detections
                    if detection.vector is not None
                ],
            )
            for record in self.records
            if not record.pets and not record.corrected and _vectors(record)
        ]

    async def async_told(self, told: Mapping[str, Told]) -> None:
        """Give unknown pictures, by ID, what the examples tell of them now.

        A picture that is no longer unknown, or not as it was, is left as it is.
        """
        changed = False
        for record, _animals in self.unknown():
            if (now := told.get(record.id)) is None:
                continue
            if (
                now.pets == record.pets
                and now.resembles == record.resembles
                and all(
                    detection.scores == scores
                    for detection, scores in zip(record.detections, now.scores, strict=True)
                )
            ):
                continue
            for detection, scores in zip(record.detections, now.scores, strict=True):
                detection.scores = dict(scores)
                if now.pets:
                    detection.vector = None
            record.pets = now.pets
            record.resembles = now.resembles
            self._rows.pop(record.id, None)
            changed = True
        if changed:
            await self._changed()

    @callback
    def async_retry(self, pending: Pending) -> None:
        """Count a failed attempt at a waiting picture."""
        pending.attempts += 1
        self._save()

    async def async_drop(self, pending: Pending) -> None:
        """Forget a waiting picture."""
        if pending in self.queue:
            self.queue.remove(pending)
        await self._changed(frames=[pending.id])

    async def async_record(
        self, pending: Pending, record: Record, crops: Iterable[tuple[str, bytes]] = ()
    ) -> None:
        """Turn a waiting picture into a record, with the crops of its animals."""
        crops = list(crops)
        if crops:
            await self._hass.async_add_executor_job(
                write_files,
                self.crops,
                [(self.crop_path(crop_id), crop) for crop_id, crop in crops],
            )
        if pending not in self.queue:
            # Dropped from a full queue meanwhile, with its frame.
            await self._hass.async_add_executor_job(
                delete_files, [self.crop_path(crop_id) for crop_id, _crop in crops]
            )
            return
        self.queue.remove(pending)
        before = self.kept_frames()
        self.records.append(record)
        after = self.kept_frames()
        frames = [frame for frame in (pending.id, *before) if frame not in after]
        mine = [other for other in self.records if other.source == record.source]
        unknown = [other for other in mine if _vectors(other)]
        for other in unknown[:-VECTOR_LIMIT]:
            for detection in other.detections:
                detection.vector = None
            self._rows.pop(other.id, None)
        protected = self._protected()
        old = [other for other in mine if other.id not in protected]
        await self._changed(frames=frames, records=old[: max(len(mine) - PICTURE_LIMIT, 0)])

    async def async_purge(self, before: float) -> None:
        """Remove the records of pictures taken before `before`."""
        protected = self._protected(before)
        old = [
            record
            for record in self.records
            if record.taken < before and record.id not in protected
        ]
        if old:
            await self._changed(records=old)

    async def async_keep(self, sources: Mapping[str, Collection[str]], pets: set[str]) -> None:
        """Forget what concerns sources, entities and pets that are gone.

        `sources` gives the entities of each source; `pets` the keys of the
        pets. The other animal is no subentry, and is kept.
        """
        entities = {entity for watched in sources.values() for entity in watched}
        changed = False
        for entity in set(self.seen) - entities:
            del self.seen[entity]
            changed = True
        gone = [pending for pending in self.queue if pending.source not in sources]
        for pending in gone:
            self.queue.remove(pending)
        known = pets | {OTHER_ANIMAL}
        for record in self.records:
            if _keep_pets(record, known):
                self._rows.pop(record.id, None)
                changed = True
        old = [record for record in self.records if record.source not in sources]
        for source in set(self.announced) - set(sources):
            del self.announced[source]
            changed = True
        if changed or gone or old:
            await self._changed(frames=[pending.id for pending in gone], records=old)

    def pending(self, source: str | None = None) -> list[Pending]:
        """The pictures waiting for the engine, of a source or of all."""
        return [pending for pending in self.queue if source in (None, pending.source)]

    def of(self, source: str) -> list[Record]:
        """The records of a source, oldest first."""
        return sorted((record for record in self.records if record.source == source), key=_order)

    async def _changed(self, frames: Iterable[str] = (), records: Iterable[Record] = ()) -> None:
        """Save, then delete the files no longer needed.

        The save is at once when there are files to delete, so that the
        store never names a deleted file.
        """
        records = list(records)
        for record in records:
            self.records.remove(record)
            self._rows.pop(record.id, None)
        self._forget_questions()
        # A record removed takes its frame, if it was the last picture of its source.
        paths = [self.frame_path(frame) for frame in (*frames, *(record.id for record in records))]
        paths += [
            self.crop_path(detection.crop) for record in records for detection in record.detections
        ]
        if not paths:
            self._save()
            self._notify()
            return
        await self._store.async_save(self._data())
        self._notify()
        await self._hass.async_add_executor_job(delete_files, paths)

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _forget_questions(self) -> None:
        """Forget the questions that are closed and none of whose pictures is kept."""
        kept = {record.id for record in self.records}
        self.asked = [
            asked
            for asked in self.asked
            if asked.open or any(picture in kept for picture in asked.doubt.pictures)
        ]

    def _last_pictures(self) -> dict[str, Record]:
        """The latest picture of each source where an animal was found, or
        the latest of all at a source that only takes pictures of a pet."""
        latest: dict[str, Record] = {}
        for record in self.records:
            if (record.detections or record.source in self.every_picture) and (
                record.source not in latest or _order(record) > _order(latest[record.source])
            ):
                latest[record.source] = record
        return latest

    def _protected(self, before: float | None = None) -> set[str]:
        """The records that stay past the limits: what each source and pet last showed."""
        latest: dict[str, Record] = {}
        for record in self.records:
            for pet in record.pets:
                if pet not in latest or _order(record) > _order(latest[pet]):
                    latest[pet] = record
        return {
            record.id for record in (*self._last_pictures().values(), *latest.values())
        } | self._protect(before)

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)

    def _row(self, record: Record) -> dict[str, Any]:
        if (row := self._rows.get(record.id)) is None:
            row = self._rows[record.id] = _record_row(record)
        return row

    def _data(self) -> dict[str, Any]:
        return {
            "seen": [
                {"entity": seen.entity, "state": seen.state, "sha256": seen.sha256}
                for seen in self.seen.values()
            ],
            "queue": [
                {
                    "id": pending.id,
                    "source": pending.source,
                    "entity": pending.entity,
                    "taken": pending.taken,
                    "sha256": pending.sha256,
                    "attempts": pending.attempts,
                }
                for pending in self.queue
            ],
            "pictures": [self._row(record) for record in self.records],
            "announced": dict(self.announced),
            "questions": [_asked_row(asked) for asked in self.asked],
        }

    def _delete_unknown_files(self, frames: set[str], crops: set[str]) -> None:
        delete_unknown_files(self.frames, FRAME, frames.__contains__)
        delete_unknown_files(self.crops, CROP, crops.__contains__)


def _nothing(_before: float | None) -> set[str]:
    """No more records to keep."""
    return set()


def _order(record: Record) -> tuple[float, str]:
    """The order of the records of a source: by time taken, then ID."""
    return (record.taken, record.id)


def _record_row(record: Record) -> dict[str, Any]:
    """The stored form of a record."""
    return {
        "id": record.id,
        "source": record.source,
        "entity": record.entity,
        "taken": record.taken,
        "sha256": record.sha256,
        "status": record.status,
        "pets": list(record.pets),
        "resembles": record.resembles,
        "corrected": record.corrected,
        "group": record.group,
        "detections": [
            {
                "species": detection.species,
                "confidence": detection.confidence,
                "area": detection.area,
                "crop": detection.crop,
                "model": detection.model,
                "scores": detection.scores,
                "vector": None if detection.vector is None else encode_vector(detection.vector),
            }
            for detection in record.detections
        ],
    }


def _asked_row(asked: Asked) -> dict[str, Any]:
    """The stored form of a question."""
    doubt = asked.doubt
    return {
        "id": doubt.id,
        "source": doubt.source,
        "pictures": list(doubt.pictures),
        "shown": doubt.shown,
        "kind": doubt.kind,
        "asked": doubt.asked,
        "state": doubt.state,
        "taken": asked.taken,
        "crop": asked.crop,
        "choices": list(asked.choices),
        "phones": list(asked.phones),
        "sent": list(asked.sent),
    }


def _vectors(record: Record) -> bool:
    """Whether a record keeps embeddings."""
    return any(detection.vector is not None for detection in record.detections)


def _keep_pets(record: Record, known: set[str]) -> bool:
    """Forget the pets of a record that are gone, and say if any were."""
    changed = False
    if any(pet not in known for pet in record.pets):
        record.pets = tuple(pet for pet in record.pets if pet in known)
        changed = True
    if record.resembles is not None and record.resembles not in known:
        record.resembles = None
        changed = True
    for detection in record.detections:
        if any(pet not in known for pet in detection.scores):
            detection.scores = {
                pet: score for pet, score in detection.scores.items() if pet in known
            }
            changed = True
    return changed


def _read_list[T](stored: Mapping[str, Any], key: str, parse: Callable[[Any], T]) -> list[T]:
    """The valid items of the list under `key`; the others are logged and left out."""
    value = stored.get(key, [])
    if not isinstance(value, list):
        _LOGGER.warning("The %s of the journal cannot be read, and are ignored", key)
        return []
    valid: list[T] = []
    for item in value:
        try:
            valid.append(parse(item))
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("A record of the journal cannot be read, and is ignored: %s", err)
    return valid


def _read_announced(stored: Mapping[str, Any]) -> dict[str, float]:
    """The start of the latest visit announced of each source; unreadable ones are left out."""
    value = stored.get("announced", {})
    if not isinstance(value, dict):
        _LOGGER.warning("The announced visits of the journal cannot be read, and are ignored")
        return {}
    announced: dict[str, float] = {}
    for source, start in value.items():
        try:
            announced[_text(source)] = _number(start)
        except ValueError as err:
            _LOGGER.warning(
                "An announced visit of the journal cannot be read, and is ignored: %s", err
            )
    return announced


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("a text is missing")
    return value


def _id(value: object) -> str:
    if not isinstance(value, str) or not FILE_ID.fullmatch(value):
        raise ValueError("not the ID of a file")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("a number is missing")
    return float(value)


def _seen(value: Mapping[str, Any]) -> Seen:
    return Seen(
        entity=_text(value["entity"]), state=_text(value["state"]), sha256=_text(value["sha256"])
    )


def _pending(value: Mapping[str, Any]) -> Pending:
    attempts = value["attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("a count of attempts is missing")
    return Pending(
        id=_id(value["id"]),
        source=_text(value["source"]),
        entity=_text(value["entity"]),
        taken=_number(value["taken"]),
        sha256=_text(value["sha256"]),
        attempts=attempts,
    )


def _record(value: Mapping[str, Any]) -> Record:
    pets, resembles, detections = value["pets"], value["resembles"], value["detections"]
    if not isinstance(pets, list) or not isinstance(detections, list):
        raise ValueError("the pets and animals of a picture are lists")
    # Journals from before corrections have neither.
    corrected, group = value.get("corrected", False), value.get("group", "")
    if not isinstance(corrected, bool) or not isinstance(group, str):
        raise ValueError("a correction cannot be read")
    return Record(
        id=_id(value["id"]),
        source=_text(value["source"]),
        entity=_text(value["entity"]),
        taken=_number(value["taken"]),
        sha256=_text(value["sha256"]),
        status=Status(value["status"]),
        detections=[_detection(item) for item in detections],
        pets=tuple(_text(pet) for pet in pets),
        resembles=None if resembles is None else _text(resembles),
        corrected=corrected,
        group=group,
    )


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("a list of texts is missing")
    return tuple(_text(item) for item in value)


def _asked(value: Mapping[str, Any]) -> Asked:
    crop = value["crop"]
    doubt = Doubt(
        id=_id(value["id"]),
        source=_text(value["source"]),
        pictures=tuple(_id(picture) for picture in _texts(value["pictures"])),
        shown=_id(value["shown"]),
        kind=Kind(value["kind"]),
        asked=_number(value["asked"]),
        state=State(value["state"]),
    )
    return Asked(
        doubt=doubt,
        taken=_number(value["taken"]),
        crop=None if crop is None else _id(crop),
        choices=_texts(value["choices"]),
        phones=_texts(value["phones"]),
        sent=_texts(value["sent"]),
    )


def _detection(value: Mapping[str, Any]) -> Detection:
    scores = value["scores"]
    if not isinstance(scores, dict):
        raise ValueError("the scores of an animal are missing")
    vector = value["vector"]
    return Detection(
        species=_text(value["species"]),
        confidence=_number(value["confidence"]),
        area=_number(value["area"]),
        crop=_id(value["crop"]),
        model=_text(value["model"]),
        scores={_text(pet): _number(score) for pet, score in scores.items()},
        vector=None if vector is None else decode_vector(vector),
    )
