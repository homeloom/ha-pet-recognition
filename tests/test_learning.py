"""Tests of what the answers and corrections teach, and of telling unknown pictures again."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition import learning
from custom_components.pet_recognition.core.corrections import Correction
from custom_components.pet_recognition.core.doubts import State
from custom_components.pet_recognition.core.matching import (
    Example,
    MatchSettings,
    Told,
    normalise,
    tell,
)
from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics
from custom_components.pet_recognition.examples import (
    Candidate,
    ExampleStore,
    Origin,
    stored_vector,
)
from custom_components.pet_recognition.journal import Record

from .answers import CROP, MODEL, URL, embedding, error, unit
from .asking import PET_C, UNSURE, Phones, asking, visit
from .test_answers import MEALS_B, press, unsure
from .watching import PET_A, PET_B, Shown, journal, settle, source_id

OTHER_MODEL = "other-model@2"


def store(entry: MockConfigEntry) -> ExampleStore:
    return entry.runtime_data.examples


def learned(entry: MockConfigEntry) -> list[tuple[str, Origin, str | None]]:
    """The examples learned, by pet, origin and picture."""
    examples = store(entry)
    return [
        (example.pet, examples.origin(example.id), examples.picture(example.id))
        for example in examples.gallery
        if not example.uploaded
    ]


def records(entry: MockConfigEntry) -> list[Record]:
    return journal(entry).of(source_id(entry))


def stored(hass_storage: dict[str, Any], entry: MockConfigEntry) -> list[dict[str, Any]]:
    return hass_storage[f"pet_recognition.{entry.entry_id}.examples"]["data"]["examples"]


async def correct(entry: MockConfigEntry, record: Record, *pets: str) -> None:
    """Correct one picture by hand, which teaches its animal for a single pet."""
    correction = Correction((record.id,), pets, record.id, record.id if len(pets) == 1 else None)
    await entry.runtime_data.learner.async_correct(correction, Origin.CORRECTION)


async def test_an_answer_teaches_an_example(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_storage: dict[str, Any],
) -> None:
    entry = await unsure(hass, aioclient_mock, shown, freezer, phones)
    (record,) = records(entry)
    await press(hass, entry, PET_B)

    assert learned(entry) == [(PET_B, Origin.ANSWER, record.id)]
    (example,) = (example for example in store(entry).gallery if not example.uploaded)
    # The embedding the journal kept, as the engine gave it, and the crop of the animal.
    assert example.vector == stored_vector(UNSURE)
    assert example.model == MODEL
    assert await store(entry).async_read_crop(example.id) == CROP
    # Relabelled, the picture needs its embedding no more.
    assert record.detections[0].vector is None
    (row,) = (row for row in stored(hass_storage, entry) if row["id"] == example.id)
    assert (row["origin"], row["picture"]) == ("answer", record.id)
    # What was given has no picture.
    given = [row for row in stored(hass_storage, entry) if row["origin"] == "upload"]
    assert len(given) == 3
    assert all("picture" not in row for row in given)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert [pet["learned"] for pet in diagnostics["pets"]] == [0, 1, 0]

    # Read again after a restart.
    again = ExampleStore(hass, entry.entry_id)
    await again.async_load()
    assert again.origin(example.id) is Origin.ANSWER
    assert again.picture(example.id) == record.id

    # The next visit of that animal is told, and not asked about.
    await visit(hass, aioclient_mock, shown, freezer, 20)
    assert records(entry)[-1].pets == (PET_B,)
    assert len(journal(entry).asked) == 1
    assert hass.states.get(MEALS_B).state == "2"


async def test_an_answer_tells_the_other_visits(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    await visit(hass, aioclient_mock, shown, freezer, 1)
    await visit(hass, aioclient_mock, shown, freezer, 20)
    first, second = journal(entry).asked
    assert hass.states.get(MEALS_B).state == "0"

    await press(hass, entry, PET_B, question=first)
    # The example of the first visit tells the second one, which settles its question.
    assert [record.pets for record in records(entry)] == [(PET_B,), (PET_B,)]
    assert (first.doubt.state, second.doubt.state) == (State.SETTLED, State.SETTLED)
    tags = [f"pet_recognition_{asked.doubt.id}" for asked in (first, second)]
    assert sorted(tag for _name, tag in phones.cleared()) == sorted(tags * 2)
    assert hass.states.get(MEALS_B).state == "2"
    # Told by the examples, not corrected.
    assert not records(entry)[1].corrected
    assert records(entry)[1].detections[0].vector is None


async def test_a_correction_without_an_embedding_embeds_the_crop(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    # Told Pet B, so the journal keeps no embedding.
    await visit(hass, aioclient_mock, shown, freezer, 1, unit(0, 1, 0))
    (record,) = records(entry)
    assert record.detections[0].vector is None
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(1, 0, 1, model=OTHER_MODEL))

    await correct(entry, record, PET_C)
    (call,) = [call for call in aioclient_mock.mock_calls if call[1].path == "/v1/embed"]
    assert call[2] == CROP
    (example,) = (example for example in store(entry).gallery if not example.uploaded)
    assert (example.pet, example.model) == (PET_C, OTHER_MODEL)
    assert example.vector == stored_vector(unit(1, 0, 1))
    assert record.pets == (PET_C,)


async def test_a_correction_teaches_nothing_when_the_engine_cannot_embed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1, unit(0, 1, 0))
    (record,) = records(entry)
    aioclient_mock.post(f"{URL}/v1/embed", status=503, json=error("busy"))

    await correct(entry, record, PET_C)
    assert learned(entry) == []
    assert "cannot embed its crop" in caplog.text
    # The correction stands.
    assert (record.pets, record.corrected) == ((PET_C,), True)


async def test_a_correction_teaches_nothing_without_its_crop(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await unsure(hass, aioclient_mock, shown, freezer, phones)
    (record,) = records(entry)
    await hass.async_add_executor_job(journal(entry).crop_path(record.detections[0].crop).unlink)

    await press(hass, entry, PET_B)
    assert learned(entry) == []
    assert "its crop is missing" in caplog.text
    assert record.pets == (PET_B,)


async def test_correcting_again_takes_the_example_back(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await unsure(hass, aioclient_mock, shown, freezer, phones)
    (record,) = records(entry)
    await press(hass, entry, PET_B)
    (first,) = (example for example in store(entry).gallery if not example.uploaded)
    crop = store(entry).crop_path(first.id)
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*UNSURE))

    # To another pet: the example moves.
    await correct(entry, record, PET_C)
    assert learned(entry) == [(PET_C, Origin.CORRECTION, record.id)]
    assert not await hass.async_add_executor_job(crop.exists)

    # To several pets, or to unknown: it goes.
    await correct(entry, record, PET_B, PET_C)
    assert learned(entry) == []
    assert record.pets == (PET_B, PET_C)
    await correct(entry, record)
    assert learned(entry) == []
    assert (record.pets, record.corrected) == ((), True)


async def test_what_a_correction_cannot_teach(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_storage: dict[str, Any],
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1, unit(0, 1, 0))
    (record,) = records(entry)
    saved = list(stored(hass_storage, entry))
    learner = entry.runtime_data.learner

    # A picture that is gone, and one with no example to learn.
    gone = Correction(("f" * 32,), (PET_C,), "f" * 32, "f" * 32)
    await learner.async_correct(gone, Origin.CORRECTION)
    await learner.async_correct(Correction((record.id,), (PET_C,), record.id, None), Origin.ANSWER)
    assert learned(entry) == []
    assert record.pets == (PET_C,)
    # Nothing changed, so nothing was saved.
    assert stored(hass_storage, entry) == saved

    # The same animal as an example the pet has already.
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(0, 0, 1))
    await correct(entry, record, PET_C)
    assert learned(entry) == []
    assert len(await hass.async_add_executor_job(list, store(entry).directory.iterdir())) == 3


async def test_new_examples_tell_the_unknown_pictures(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    (record,) = records(entry)
    before = dict(record.detections[0].scores)

    # Another example of Pet C changes the scores, without telling.
    await store(entry).async_add(
        PET_C, Candidate(CROP, tuple(unit(-0.2, 0.2, 1)), MODEL), Origin.UPLOAD
    )
    await settle(hass)
    assert record.pets == ()
    assert record.detections[0].scores[PET_C] != before[PET_C]
    assert record.detections[0].vector is not None

    # Removing it scores as before.
    (_given, added) = store(entry).gallery.of(PET_C)
    await store(entry).async_remove([added.id])
    await settle(hass)
    assert record.detections[0].scores == before

    # A picture with nothing new to tell is left as it is.
    await journal(entry).async_told({})
    assert record.detections[0].scores == before


async def test_examples_added_while_telling_are_told_next(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    (record,) = records(entry)
    real = learning.rematch
    told: list[int] = []

    def rematch(
        pictures: list[Any], examples: list[Example], settings: MatchSettings
    ) -> dict[str, Told]:
        if not told:
            # An example of Pet B comes while the first one is being compared.
            asyncio.run_coroutine_threadsafe(
                store(entry).async_add(PET_B, Candidate(CROP, tuple(UNSURE), MODEL), Origin.UPLOAD),
                hass.loop,
            ).result(timeout=10)
        told.append(len(examples))
        return real(pictures, examples, settings)

    monkeypatch.setattr(learning, "rematch", rematch)
    await store(entry).async_add(
        PET_A, Candidate(CROP, tuple(unit(1, -1, 0)), MODEL), Origin.UPLOAD
    )
    await settle(hass)
    # Told once without it, then once more with it.
    assert told == [4, 5]
    assert record.pets == (PET_B,)
    assert record.resembles == PET_B
    assert record.detections[0].vector is None
    assert hass.states.get(MEALS_B).state == "1"


async def test_the_examples_tell_the_pictures_again_when_the_entry_loads(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    (record,) = records(entry)
    # A lower threshold and margin tell Pet B.
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "threshold": 0.5, "margin": 0.01},
    )
    await settle(hass)
    (record,) = records(entry)
    assert record.pets == (PET_B,)


async def test_telling_the_pictures_again_leaves_the_event_loop_free(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    loop_ran = threading.Event()
    threads: list[threading.Thread] = []
    real = learning.rematch

    def rematch(
        pictures: list[Any], examples: list[Example], settings: MatchSettings
    ) -> dict[str, Told]:
        threads.append(threading.current_thread())
        # Only set by the event loop, which must go on meanwhile.
        assert loop_ran.wait(timeout=10)
        return real(pictures, examples, settings)

    monkeypatch.setattr(learning, "rematch", rematch)
    hass.loop.call_soon(loop_ran.set)
    await store(entry).async_add(PET_B, Candidate(CROP, tuple(UNSURE), MODEL), Origin.UPLOAD)
    await settle(hass)
    assert threads
    assert threading.main_thread() not in threads
    assert records(entry)[0].pets == (PET_B,)


def test_telling_a_large_batch_again() -> None:
    """Two hundred unknown pictures, three pets of two hundred examples each,
    with embeddings of the engine's size."""
    vectors = [
        normalise((1 + index % 7, *(((index * 31 + value) % 17) - 8 for value in range(383))))
        for index in range(800)
    ]
    examples = [
        Example(id=f"{index:032x}", pet=f"pet {index % 3}", vector=vector, model=MODEL, added=index)
        for index, vector in enumerate(vectors[:600])
    ]
    pictures = [
        (f"{index:032x}", [(vector, MODEL)], ["pet 0", "pet 1", "pet 2"])
        for index, vector in enumerate(vectors[600:])
    ]
    told = learning.rematch(pictures, examples, MatchSettings())
    assert len(told) == len(pictures)
    gallery = learning.Gallery(limit=200)
    gallery.restore(examples)
    picture_id, animals, expected = pictures[0]
    assert told[picture_id] == tell(animals, gallery, expected, MatchSettings())
