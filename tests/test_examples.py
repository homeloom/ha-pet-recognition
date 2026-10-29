"""Tests of the store of the examples and of their crops."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.pet_recognition.core.matching import Added
from custom_components.pet_recognition.examples import (
    Candidate,
    ExampleStore,
    Origin,
    async_get_store,
    async_remove_data,
    decode_vector,
    encode_vector,
    stored_vector,
)

from .answers import CROP, MODEL, unit

ENTRY = "entry1"
KEY = f"pet_recognition.{ENTRY}.examples"


def candidate(*values: float, model: str = MODEL, crop: bytes = CROP) -> Candidate:
    return Candidate(crop=crop, vector=tuple(unit(*values)), model=model)


async def loaded(hass: HomeAssistant) -> ExampleStore:
    store = ExampleStore(hass, ENTRY)
    await store.async_load()
    return store


def files(store: ExampleStore) -> set[str]:
    return {path.name for path in store.directory.iterdir()} if store.directory.is_dir() else set()


def test_vectors_are_stored_as_float16() -> None:
    vector = tuple(unit(1, 2, 2))
    text = encode_vector(vector)
    assert len(base64.b64decode(text)) == 6
    assert decode_vector(text) == pytest.approx(vector, abs=1e-3)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "even, positive"),
        (base64.b64encode(b"abc").decode(), "even, positive"),
        ("not base64!", "base64|Non-base64|Invalid"),
    ],
)
def test_a_damaged_vector_is_refused(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        decode_vector(text)


def test_a_zero_vector_is_refused() -> None:
    with pytest.raises(ValueError, match="zero vector"):
        decode_vector(base64.b64encode(struct.pack("<2e", 0, 0)).decode())


async def test_examples_and_their_crops_are_kept_across_restarts(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    assert await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD) is Added.ADDED
    assert await store.async_add("pet", candidate(0, 1, 0), Origin.ANSWER) is Added.ADDED
    first, second = store.gallery.of("pet")
    assert first.uploaded
    assert not second.uploaded
    assert files(store) == {f"{first.id}.jpg", f"{second.id}.jpg"}
    assert await store.async_read_crop(first.id) == CROP

    again = await loaded(hass)
    assert [example.id for example in again.gallery.of("pet")] == [first.id, second.id]
    # As precise in memory as on disk, so that a restart changes no comparison.
    assert again.gallery.get(first.id).vector == first.vector
    assert first.vector == stored_vector(unit(1, 0, 0))
    assert again.origin(first.id) is Origin.UPLOAD
    assert again.origin(second.id) is Origin.ANSWER
    assert again.gallery.get(second.id).model == MODEL


async def test_a_duplicate_writes_nothing(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD)
    assert await store.async_add("pet", candidate(1, 0.01, 0), Origin.UPLOAD) is Added.DUPLICATE
    assert len(store.gallery) == 1
    assert len(files(store)) == 1


async def test_a_full_pet_refuses_more_given_examples(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await loaded(hass)
    monkeypatch.setattr(store.gallery, "limit", 1)
    await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD)
    assert await store.async_add("pet", candidate(0, 1, 0), Origin.CAPTURE) is Added.FULL
    assert len(files(store)) == 1


async def test_a_given_photo_takes_the_place_of_a_learned_duplicate(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    await store.async_add("pet", candidate(1, 0, 0), Origin.ANSWER)
    (learned,) = store.gallery.of("pet")
    assert await store.async_add("pet", candidate(1, 0, 0), Origin.CAPTURE) is Added.ADDED
    (given,) = store.gallery.of("pet")
    assert given.uploaded
    assert store.origin(given.id) is Origin.CAPTURE
    assert files(store) == {f"{given.id}.jpg"}
    with pytest.raises(KeyError):
        store.origin(learned.id)


async def test_a_learned_example_makes_room_and_its_crop_goes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await loaded(hass)
    monkeypatch.setattr(store.gallery, "limit", 1)
    await store.async_add("pet", candidate(1, 0, 0), Origin.CORRECTION)
    (old,) = store.gallery.of("pet")
    assert await store.async_add("pet", candidate(0, 1, 0), Origin.ANSWER) is Added.ADDED
    (new,) = store.gallery.of("pet")
    assert files(store) == {f"{new.id}.jpg"}
    with pytest.raises(KeyError):
        store.origin(old.id)


async def test_listeners_follow_the_changes(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    calls: list[None] = []
    remove = store.async_add_listener(lambda: calls.append(None))
    await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD)
    await store.async_remove([example.id for example in store.gallery])
    assert len(calls) == 2
    remove()
    await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD)
    assert len(calls) == 2


async def test_removed_examples_lose_their_crops(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD)
    await store.async_add("pet", candidate(0, 1, 0), Origin.UPLOAD)
    first, second = store.gallery.of("pet")
    await store.async_remove([first.id, "0" * 32])
    assert files(store) == {f"{second.id}.jpg"}
    assert [example.id for example in (await loaded(hass)).gallery] == [second.id]


async def test_only_the_examples_of_known_pets_are_kept(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    await store.async_add("kept", candidate(1, 0, 0), Origin.UPLOAD)
    await store.async_add("gone", candidate(0, 1, 0), Origin.UPLOAD)
    calls: list[None] = []
    store.async_add_listener(lambda: calls.append(None))
    await store.async_keep_pets({"kept", "never had examples"})
    assert store.gallery.pets() == {"kept"}
    assert len(files(store)) == 1
    await store.async_keep_pets({"kept"})
    # Nothing to remove: nothing saved.
    assert len(calls) == 1


async def test_the_examples_of_a_pet_being_created_are_kept(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    release = store.reserve("new")
    await store.async_add("new", candidate(1, 0, 0), Origin.UPLOAD)
    await store.async_keep_pets(set())
    assert store.gallery.pets() == {"new"}
    release()
    await store.async_keep_pets(set())
    assert not store.gallery.pets()


async def test_an_example_can_get_the_embedding_of_another_model(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    await store.async_add("pet", candidate(1, 0, 0, model="old@1"), Origin.UPLOAD)
    (example,) = store.gallery
    await store.async_replace(example.id, unit(1, 1, 0), "new@1")
    replaced = store.gallery.get(example.id)
    assert (replaced.model, replaced.added, replaced.uploaded) == ("new@1", example.added, True)
    assert replaced.vector == stored_vector(unit(1, 1, 0))
    assert (await loaded(hass)).gallery.get(example.id) == replaced
    # Removed meanwhile.
    await store.async_remove([example.id])
    await store.async_replace(example.id, unit(0, 1, 0), "new@1")
    assert store.gallery.get(example.id) is None


async def test_a_given_example_stays_when_embedded_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await loaded(hass)
    # A learned example of the new model comes first.
    await store.async_add("pet", candidate(0, 0, 1, model="new@1"), Origin.ANSWER)
    await store.async_add("pet", candidate(1, 0, 0, model="old@1"), Origin.UPLOAD)
    await store.async_add("pet", candidate(0, 1, 0, model="old@1"), Origin.CAPTURE)
    learned, first, second = store.gallery.of("pet")
    monkeypatch.setattr(store.gallery, "limit", 1)
    # The same picture as the learned example, and no room left: it stays.
    await store.async_replace(first.id, unit(0, 0, 1), "new@1")
    await store.async_replace(second.id, unit(0, 1, 0), "new@1")
    assert {example.id for example in store.gallery} == {learned.id, first.id, second.id}
    assert {example.model for example in store.gallery} == {"new@1"}
    assert len(files(store)) == 3


async def test_a_learned_example_goes_when_embedded_again_as_a_copy(
    hass: HomeAssistant,
) -> None:
    store = await loaded(hass)
    await store.async_add("pet", candidate(0, 0, 1, model="new@1"), Origin.UPLOAD)
    await store.async_add("pet", candidate(1, 0, 0, model="old@1"), Origin.ANSWER)
    await store.async_add("pet", candidate(0, 1, 0, model="old@1"), Origin.ANSWER)
    given, learned, other = store.gallery.of("pet")
    await store.async_replace(learned.id, unit(0, 0, 1), "new@1")
    await store.async_replace(other.id, unit(0, 1, 0), "new@1")
    assert [example.id for example in store.gallery] == [given.id, other.id]
    assert files(store) == {f"{given.id}.jpg", f"{other.id}.jpg"}


async def test_a_learned_example_goes_when_embedded_again_into_a_full_pet(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await loaded(hass)
    monkeypatch.setattr(store.gallery, "limit", 1)
    await store.async_add("pet", candidate(0, 0, 1, model="new@1"), Origin.UPLOAD)
    await store.async_add("pet", candidate(1, 0, 0, model="old@1"), Origin.CORRECTION)
    given, learned = store.gallery.of("pet")
    await store.async_replace(learned.id, unit(1, 0, 0), "new@1")
    assert [example.id for example in store.gallery] == [given.id]


async def test_examples_are_added_together_with_one_save(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    calls: list[None] = []
    store.async_add_listener(lambda: calls.append(None))
    outcomes = await store.async_add_many(
        "pet",
        [
            (candidate(1, 0, 0), Origin.UPLOAD),
            (candidate(1, 0.01, 0), Origin.UPLOAD),
            (candidate(0, 1, 0), Origin.CAPTURE),
        ],
    )
    assert outcomes == [Added.ADDED, Added.DUPLICATE, Added.ADDED]
    assert len(calls) == 1
    assert {f"{example.id}.jpg" for example in store.gallery} == files(store)
    assert len(files(store)) == 2
    # Nothing added: nothing saved, and no crop left.
    assert await store.async_add_many("pet", [(candidate(1, 0, 0), Origin.UPLOAD)]) == [
        Added.DUPLICATE
    ]
    assert len(calls) == 1
    assert len(files(store)) == 2


async def test_crops_named_by_no_example_are_deleted_on_load(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD)
    (example,) = store.gallery
    (store.directory / ("0" * 32 + ".jpg")).write_bytes(CROP)
    (store.directory / "left.tmp").write_bytes(CROP)
    (store.directory / f"{example.id}.tmp").write_bytes(CROP)
    (store.directory / "folder").mkdir()
    await loaded(hass)
    assert files(store) == {f"{example.id}.jpg", "folder"}


@pytest.mark.parametrize("example_id", ["../../secrets", "A" * 32, "0" * 31, "0" * 32 + "/x"])
def test_crop_paths_are_only_for_example_ids(hass: HomeAssistant, example_id: str) -> None:
    with pytest.raises(ValueError, match="not the ID of a file"):
        ExampleStore(hass, ENTRY).crop_path(example_id)


async def test_the_data_of_a_deleted_entry_is_removed(hass: HomeAssistant) -> None:
    store = await loaded(hass)
    await store.async_add("pet", candidate(1, 0, 0), Origin.UPLOAD)
    assert await async_get_store(hass, ENTRY) is await async_get_store(hass, ENTRY)
    await async_remove_data(hass, ENTRY)
    assert not Path(hass.config.path("pet_recognition", ENTRY)).exists()
    assert not (await loaded(hass)).gallery.pets()
    # Nothing to remove is fine too.
    await async_remove_data(hass, "never-set-up")


def stored(examples: object) -> dict[str, object]:
    return {"version": 1, "minor_version": 1, "key": KEY, "data": examples}


def record(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "a" * 32,
        "pet": "pet",
        "origin": "upload",
        "model": MODEL,
        "added": 1.5,
        "vector": encode_vector(unit(1, 0, 0)),
    }
    value.update(changes)
    return {key: item for key, item in value.items() if item is not None}


async def test_unreadable_stored_examples_are_ignored(
    hass: HomeAssistant, hass_storage: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    hass_storage[KEY] = stored(
        {
            "examples": [
                record(),
                record(),
                record(id="b" * 32, vector="not base64!"),
                record(id="../../x"),
                record(id=None),
                record(id="c" * 32, pet=""),
                record(id="d" * 32, model=7),
                record(id="e" * 32, added="yesterday"),
                record(id="f" * 32, added=float("nan")),
                record(id="0" * 32, origin="unknown"),
                # Only learned examples come from a picture, named by its ID.
                record(id="1" * 32, picture="2" * 32),
                record(id="3" * 32, origin="answer", picture="../x"),
                record(id="4" * 32, origin="correction", picture="5" * 32),
                "not a record",
            ]
        }
    )
    store = await loaded(hass)
    assert [example.id for example in store.gallery] == ["a" * 32, "4" * 32]
    assert store.picture("4" * 32) == "5" * 32
    assert store.picture("a" * 32) is None
    assert caplog.text.count("cannot be read") == 11
    assert "there twice" in caplog.text


@pytest.mark.parametrize("data", [["a list"], {"examples": "not a list"}])
async def test_an_unreadable_store_is_ignored(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    caplog: pytest.LogCaptureFixture,
    data: object,
) -> None:
    hass_storage[KEY] = stored(data)
    store = await loaded(hass)
    assert len(store.gallery) == 0
    assert "stored examples cannot be read" in caplog.text
