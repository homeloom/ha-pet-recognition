"""Tests of the entities that show what the sources saw."""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from homeassistant.components.image import async_get_image
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.examples import Candidate, Origin
from custom_components.pet_recognition.files import new_id
from custom_components.pet_recognition.image import content_type
from custom_components.pet_recognition.journal import Pending, Record, Status

from .answers import CROP, MODEL, analysis, animal, unit
from .watching import (
    FEEDER,
    PET_A,
    PET_B,
    START,
    Shown,
    engine,
    journal,
    pet,
    set_up,
    show,
    source,
    source_id,
)

VISITOR = "sensor.feeder_last_visitor"
FEEDER_PICTURE = "image.feeder_last_picture"
PNG = b"\x89PNG\r\n\x1a\n a PNG picture"

A = unit(1, 0, 0)
B = unit(0, 1, 0)
OTHER = unit(0, 0, 1)


def crop(name: bytes) -> str:
    return base64.b64encode(name).decode()


async def two_pets(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> MockConfigEntry:
    """Pet A and Pet B, each with an example, the other animal too, and the feeder."""
    entry = await set_up(hass, aioclient_mock, shown, pet(PET_A), pet(PET_B), source(FEEDER))
    store = entry.runtime_data.examples
    for key, vector in ((PET_B, B), (OTHER_ANIMAL, OTHER)):
        await store.async_add(key, Candidate(CROP, tuple(vector), MODEL), Origin.UPLOAD)
    return entry


def answer(aioclient_mock: AiohttpClientMocker, *animals: dict[str, object]) -> None:
    engine(aioclient_mock, json=analysis(*animals))


def at(minutes: float) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


async def test_the_last_visitor_is_who_came_in_the_latest_visit(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    await two_pets(hass, aioclient_mock, shown)
    state = hass.states.get(VISITOR)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["options"] == [
        "Pet a",
        "Pet b",
        "several",
        "unknown_animal",
        "other_animal",
    ]

    visits = [
        # Pet A, then Pet B at the same visit.
        (1, [animal(embedding=A)], "Pet a", ["Pet a"]),
        (2, [animal(embedding=B)], "several", ["Pet a", "Pet b"]),
        # A visit where nothing is found, at a feeder: unknown.
        (20, [], "unknown_animal", []),
        (40, [animal(embedding=OTHER)], "other_animal", []),
        (41, [animal(embedding=A)], "several", ["Pet a"]),
    ]
    for minutes, animals, visitor, names in visits:
        answer(aioclient_mock, *animals)
        await show(hass, shown, f"picture {minutes}".encode(), minutes)
        state = hass.states.get(VISITOR)
        assert state is not None
        assert (state.state, state.attributes["pets"]) == (visitor, names)


async def test_a_pet_was_last_seen_when_its_visit_started(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await two_pets(hass, aioclient_mock, shown)
    state = hass.states.get("sensor.pet_b_last_seen")
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert "source" not in state.attributes

    answer(aioclient_mock, animal(embedding=A))
    await show(hass, shown, b"Pet A", 1)
    answer(aioclient_mock, animal(embedding=unit(0.2, 1, 0)))
    await show(hass, shown, b"Pet B", 3)
    for key in ("a", "b"):
        state = hass.states.get(f"sensor.pet_{key}_last_seen")
        assert state is not None
        assert state.state == at(1)
        assert state.attributes["source"] == "Feeder"
    assert state.attributes["confidence"] == 0.98

    # A picture of Pet B where no animal can be seen, as a correction may make.
    pending = Pending(
        new_id(), source_id(entry), FEEDER, (START + timedelta(hours=1)).timestamp(), "e" * 64
    )
    await journal(entry).async_add_pending(pending, b"a picture")
    await journal(entry).async_record(
        pending,
        Record(
            pending.id,
            pending.source,
            FEEDER,
            pending.taken,
            pending.sha256,
            Status.ANALYZED,
            pets=(PET_B,),
        ),
    )
    state = hass.states.get("sensor.pet_b_last_seen")
    assert state is not None
    assert state.attributes["confidence"] is None


async def test_the_pending_pictures_are_counted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    await set_up(hass, aioclient_mock, shown, pet(PET_A), source(FEEDER), connected=False)
    state = hass.states.get("sensor.pet_recognition_pending_pictures")
    assert state is not None
    assert state.state == "0"
    await show(hass, shown, b"a meal", 1)
    state = hass.states.get("sensor.pet_recognition_pending_pictures")
    assert state is not None
    assert state.state == "1"


async def test_the_last_picture_of_a_source_shows_an_animal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    state = hass.states.get(FEEDER_PICTURE)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, FEEDER_PICTURE)

    await show(hass, shown, PNG, 1)
    image = await async_get_image(hass, FEEDER_PICTURE)
    assert (image.content_type, image.content) == ("image/png", PNG)
    # A picture where no animal is found is not kept, nor shown.
    answer(aioclient_mock)
    await show(hass, shown, b"nobody", 2)
    state = hass.states.get(FEEDER_PICTURE)
    assert state is not None
    assert state.state == at(1)
    assert (await async_get_image(hass, FEEDER_PICTURE)).content == PNG
    assert len(list(journal(entry).frames.iterdir())) == 1

    (record,) = (record for record in journal(entry).records if record.detections)
    journal(entry).frame_path(record.id).unlink()
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, FEEDER_PICTURE)


@pytest.mark.parametrize(
    ("picture", "kind"),
    [
        (PNG, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"GIF89a", "image/gif"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WAVE", "image/jpeg"),
    ],
)
def test_the_type_of_a_picture_comes_from_its_first_bytes(picture: bytes, kind: str) -> None:
    assert content_type(picture) == kind


async def test_the_last_picture_of_a_pet_is_its_crop(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await two_pets(hass, aioclient_mock, shown)
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, "image.pet_b_last_picture")
    answer(
        aioclient_mock,
        animal(embedding=A, crop=crop(b"crop of Pet A")),
        animal(embedding=B, crop=crop(b"crop of Pet B")),
    )
    await show(hass, shown, b"both pets", 1)
    for key in ("a", "b"):
        image = await async_get_image(hass, f"image.pet_{key}_last_picture")
        assert (image.content_type, image.content) == (
            "image/jpeg",
            f"crop of Pet {key.upper()}".encode(),
        )
        state = hass.states.get(f"image.pet_{key}_last_picture")
        assert state is not None
        assert state.state == at(1)

    (record,) = journal(entry).records
    journal(entry).crop_path(record.detections[1].crop).unlink()
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, "image.pet_b_last_picture")

    # A picture of Pet A where no animal can be seen has no crop to show.
    pending = Pending(
        new_id(), source_id(entry), FEEDER, (START + timedelta(minutes=2)).timestamp(), "e" * 64
    )
    await journal(entry).async_add_pending(pending, b"a picture")
    await journal(entry).async_record(
        pending,
        Record(
            pending.id,
            pending.source,
            FEEDER,
            pending.taken,
            pending.sha256,
            Status.ANALYZED,
            pets=(PET_A,),
        ),
    )
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, "image.pet_a_last_picture")
