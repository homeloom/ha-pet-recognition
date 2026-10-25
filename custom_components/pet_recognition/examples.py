"""The example pictures of the pets: their embeddings, and their crops on disk.

The embeddings are kept in a store of Home Assistant, as float16 in base64:
about one kilobyte each. The crop of each example is a JPEG file under
`<config>/pet_recognition/<entry_id>/examples/`, named by the example's
random ID, so that the examples can be shown, and embedded again when the
engine's model changes. A crop is written, and synced to disk, before the
store that names it, and deleted after, so the store never names a missing
file; files the store does not name, left by a crash, are deleted when the
store loads.

There is one store per entry, loaded once and kept across reloads: a flow
that started before a reload may still add examples, and two stores of the
same file would save over each other.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import re
import secrets
import shutil
import struct
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, EXAMPLE_LIMIT
from .core.matching import Added, Example, Gallery, normalise

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

DATA_STORES: HassKey[dict[str, ExampleStore]] = HassKey(f"{DOMAIN}_examples")

# The ID of an example, and the name of its crop.
EXAMPLE_ID = re.compile(r"[0-9a-f]{32}")


class Origin(StrEnum):
    """Where an example comes from."""

    # Given by the user, and never dropped to make room.
    UPLOAD = "upload"
    CAPTURE = "capture"
    # Learned from the user's answers and corrections.
    ANSWER = "answer"
    CORRECTION = "correction"


GIVEN = frozenset({Origin.UPLOAD, Origin.CAPTURE})


@dataclass(frozen=True)
class Candidate:
    """A crop and its embedding, not yet an example of a pet."""

    crop: bytes
    vector: tuple[float, ...]
    model: str


def new_id() -> str:
    """A random ID for an example."""
    return secrets.token_hex(16)


def data_directory(hass: HomeAssistant, entry_id: str) -> Path:
    """Where the files of an entry are kept."""
    return Path(hass.config.path(DOMAIN, entry_id))


def encode_vector(vector: Iterable[float]) -> str:
    """`vector` as float16 in base64."""
    values = tuple(vector)
    return base64.b64encode(struct.pack(f"<{len(values)}e", *values)).decode()


def decode_vector(text: str) -> tuple[float, ...]:
    """The unit vector encoded by `encode_vector`."""
    data = base64.b64decode(text, validate=True)
    if not data or len(data) % 2:
        raise ValueError("an embedding has an even, positive number of bytes")
    return normalise(struct.unpack(f"<{len(data) // 2}e", data))


def stored_vector(vector: Iterable[float]) -> tuple[float, ...]:
    """`vector` as the store keeps it, so that a restart changes no comparison."""
    return decode_vector(encode_vector(normalise(vector)))


async def async_get_store(hass: HomeAssistant, entry_id: str) -> ExampleStore:
    """The examples of an entry, loaded the first time."""
    stores = hass.data.setdefault(DATA_STORES, {})
    if (store := stores.get(entry_id)) is None:
        store = ExampleStore(hass, entry_id)
        await store.async_load()
        stores[entry_id] = store
    return store


class ExampleStore:
    """The examples of every pet of one entry.

    Pets are keyed by the unique ID of their subentry, which the flow that
    creates a pet chooses, so that its examples can be stored before the
    subentry exists. Changes are made one at a time.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.examples", private=True
        )
        self.directory = data_directory(hass, entry_id) / "examples"
        self.gallery = Gallery(limit=EXAMPLE_LIMIT)
        self._origins: dict[str, Origin] = {}
        self._listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        # The keys of pets being created, whose examples are kept.
        self._creating: set[str] = set()

    async def async_load(self) -> None:
        """Read the examples, and delete the crops of none."""
        stored = await self._store.async_load()
        records = stored.get("examples") if isinstance(stored, dict) else None
        if stored is not None and not isinstance(records, list):
            _LOGGER.warning("The stored examples cannot be read, and are ignored")
        examples: dict[str, Example] = {}
        for record in records or []:
            try:
                example, origin = _parse(record)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("A stored example cannot be read, and is ignored: %s", err)
                continue
            if example.id in examples:
                _LOGGER.warning("A stored example is there twice, and is read once")
                continue
            examples[example.id] = example
            self._origins[example.id] = origin
        # The gallery checked them when they were added.
        self.gallery.restore(examples.values())
        await self._hass.async_add_executor_job(self._delete_unknown_files)

    def origin(self, example_id: str) -> Origin:
        """Where the example comes from."""
        return self._origins[example_id]

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `listener` after each change."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def reserve(self, pet: str) -> CALLBACK_TYPE:
        """Keep the examples of `pet` while it is created, until the callback."""
        self._creating.add(pet)
        return lambda: self._creating.discard(pet)

    async def async_add(self, pet: str, candidate: Candidate, origin: Origin) -> Added:
        """Make `candidate` an example of `pet`, unless it adds nothing."""
        (outcome,) = await self.async_add_many(pet, [(candidate, origin)])
        return outcome

    async def async_add_many(
        self, pet: str, candidates: Sequence[tuple[Candidate, Origin]]
    ) -> list[Added]:
        """Make each candidate an example of `pet`, saving once."""
        added = dt_util.utcnow().timestamp()
        examples = [
            (
                Example(
                    id=new_id(),
                    pet=pet,
                    vector=stored_vector(candidate.vector),
                    model=candidate.model,
                    added=added,
                    uploaded=origin in GIVEN,
                ),
                candidate.crop,
                origin,
            )
            for candidate, origin in candidates
        ]
        async with self._lock:
            # The crops are on disk before the gallery, and so the store, names them.
            await self._hass.async_add_executor_job(
                self._write_crops, [(example.id, crop) for example, crop, _ in examples]
            )
            outcomes: list[Added] = []
            removed: list[Example] = []
            for example, _, origin in examples:
                outcome, made_room = self.gallery.add(example)
                outcomes.append(outcome)
                if outcome is Added.ADDED:
                    self._origins[example.id] = origin
                    removed.extend(made_room)
                else:
                    removed.append(example)
            if Added.ADDED in outcomes:
                await self._save_and_delete(removed)
            else:
                await self._hass.async_add_executor_job(
                    self._delete_crops, [example.id for example in removed]
                )
        return outcomes

    async def async_replace(self, example_id: str, vector: Iterable[float], model: str) -> None:
        """Give an example the embedding of another model, if it still exists."""
        async with self._lock:
            if (old := self.gallery.remove(example_id)) is None:
                return
            new = replace(old, vector=stored_vector(vector), model=model)
            removed: list[Example] = []
            if old.uploaded:
                # The user gave it: it stays, whatever the limit and the others.
                self.gallery.restore([new])
            else:
                outcome, removed = self.gallery.add(new)
                if outcome is not Added.ADDED:
                    # The same picture as another example, or no room left for it.
                    removed = [old]
            await self._save_and_delete(removed)

    async def async_remove(self, example_ids: Iterable[str]) -> None:
        """Remove examples by ID."""
        async with self._lock:
            removed = [
                example
                for example_id in example_ids
                if (example := self.gallery.remove(example_id))
            ]
            await self._save_and_delete(removed)

    async def async_keep_pets(self, pets: set[str]) -> None:
        """Remove the examples of every pet not in `pets`, nor being created."""
        async with self._lock:
            removed = [
                example
                for pet in {example.pet for example in self.gallery} - pets - self._creating
                for example in self.gallery.remove_pet(pet)
            ]
            if removed:
                await self._save_and_delete(removed)

    async def async_read_crop(self, example_id: str) -> bytes:
        """The crop of an example."""
        return await self._hass.async_add_executor_job(self.crop_path(example_id).read_bytes)

    def crop_path(self, example_id: str) -> Path:
        """The file of an example's crop, for an ID checked to be one."""
        if not EXAMPLE_ID.fullmatch(example_id):
            raise ValueError("not the ID of an example")
        return self.directory / f"{example_id}.jpg"

    async def _save_and_delete(self, removed: list[Example]) -> None:
        for example in removed:
            self._origins.pop(example.id, None)
        await self._store.async_save(self._data())
        for listener in list(self._listeners):
            listener()
        if removed:
            await self._hass.async_add_executor_job(
                self._delete_crops, [example.id for example in removed]
            )

    def _data(self) -> dict[str, Any]:
        return {
            "examples": [
                {
                    "id": example.id,
                    "pet": example.pet,
                    "origin": self._origins[example.id],
                    "model": example.model,
                    "added": example.added,
                    "vector": encode_vector(example.vector),
                }
                for example in self.gallery
            ]
        }

    def _write_crops(self, crops: list[tuple[str, bytes]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for example_id, crop in crops:
            path = self.crop_path(example_id)
            temporary = path.with_suffix(".tmp")
            with temporary.open("wb") as file:
                file.write(crop)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
        # The new names are on disk too.
        directory = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _delete_crops(self, example_ids: list[str]) -> None:
        for example_id in example_ids:
            self.crop_path(example_id).unlink(missing_ok=True)

    def _delete_unknown_files(self) -> None:
        if not self.directory.is_dir():
            return
        for path in self.directory.iterdir():
            if path.is_file() and (path.suffix != ".jpg" or self.gallery.get(path.stem) is None):
                path.unlink()


def _parse(record: Mapping[str, Any]) -> tuple[Example, Origin]:
    """A stored example, checked like any input."""
    example_id, pet, model = record["id"], record["pet"], record["model"]
    if not isinstance(example_id, str) or not EXAMPLE_ID.fullmatch(example_id):
        raise ValueError("not the ID of an example")
    if not isinstance(pet, str) or not pet or not isinstance(model, str) or not model:
        raise ValueError("an example needs a pet and a model")
    added = record["added"]
    if not isinstance(added, (int, float)) or not math.isfinite(added):
        raise ValueError("an example needs the time it was added")
    origin = Origin(record["origin"])
    example = Example(
        id=example_id,
        pet=pet,
        vector=decode_vector(record["vector"]),
        model=model,
        added=float(added),
        uploaded=origin in GIVEN,
    )
    return example, origin


async def async_remove_data(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the stored examples and files of a deleted entry."""
    hass.data.get(DATA_STORES, {}).pop(entry_id, None)
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.examples").async_remove()
    await hass.async_add_executor_job(shutil.rmtree, data_directory(hass, entry_id), True)
