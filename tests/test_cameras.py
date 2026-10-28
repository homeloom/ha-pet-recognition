"""Tests of taking bursts of pictures from cameras when their trigger fires."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.image import Image
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.cameras import fired, wait

from .watching import (
    PET_A,
    START,
    Shown,
    analyses,
    journal,
    pet,
    set_up,
    settle,
    source,
    source_id,
)

GARDEN = "camera.garden"
PATIO = "camera.patio"
MOTION = "binary_sensor.garden_motion"
DOORBELL = "event.doorbell"

type Frames = dict[str, list[bytes]]


@pytest.fixture
def frames() -> Iterator[Frames]:
    """The pictures each camera gives next; a camera with none left fails."""
    pictures: Frames = {}

    async def get(hass: HomeAssistant, entity_id: str, timeout: int = 10) -> Image:
        if not pictures.get(entity_id):
            raise HomeAssistantError("Unable to get image")
        return Image(content_type="image/jpeg", content=pictures[entity_id].pop(0))

    with patch("custom_components.pet_recognition.cameras.async_get_image", get):
        yield pictures


@pytest.fixture
def waits() -> Iterator[list[float]]:
    """The waits between the pictures of a burst, which take no time."""
    waited: list[float] = []

    async def wait(seconds: float) -> None:
        waited.append(seconds)

    with patch("custom_components.pet_recognition.cameras.wait", wait):
        yield waited


async def garden(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    *cameras: str,
    trigger: str = MOTION,
    frames: int = 3,
) -> MockConfigEntry:
    """Pet A, and a source of cameras that take `frames` pictures 10 s apart."""
    hass.states.async_set(trigger, STATE_OFF if trigger == MOTION else STATE_UNKNOWN)
    return await set_up(
        hass,
        aioclient_mock,
        shown,
        pet(PET_A),
        source(*(cameras or (GARDEN,)), trigger=trigger, frames=frames, interval=10),
    )


async def fire(hass: HomeAssistant, trigger: str = MOTION) -> None:
    hass.states.async_set(trigger, STATE_ON)
    await settle(hass)
    hass.states.async_set(trigger, STATE_OFF)
    await settle(hass)


async def test_a_trigger_takes_a_burst_of_pictures(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    frames: Frames,
    waits: list[float],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    entry = await garden(hass, aioclient_mock, shown, GARDEN, PATIO)
    # Nothing is taken until the trigger fires.
    assert journal(entry).records == []
    frames[GARDEN] = [b"garden 1", b"garden 2", b"garden 3"]
    frames[PATIO] = [b"patio 1", b"patio 2", b"patio 3"]
    await fire(hass)
    assert waits == [10, 10]
    assert [call[2] for call in analyses(aioclient_mock)] == [
        b"garden 1",
        b"patio 1",
        b"garden 2",
        b"patio 2",
        b"garden 3",
        b"patio 3",
    ]
    records = journal(entry).records
    assert [record.entity for record in records] == [GARDEN, PATIO] * 3
    assert {record.source for record in records} == {source_id(entry)}
    assert {record.taken for record in records} == {START.timestamp()}
    assert all(record.pets == (PET_A,) for record in records)
    # The cameras are not followed as image entities.
    assert journal(entry).seen == {}


async def test_the_wait_between_pictures_sleeps() -> None:
    with patch("asyncio.sleep") as sleep:
        await wait(10)
    sleep.assert_awaited_once_with(10)


@pytest.mark.parametrize(
    ("old", "new", "fires"),
    [
        (STATE_OFF, STATE_ON, True),
        (STATE_ON, STATE_ON, False),
        (STATE_ON, STATE_OFF, False),
        (STATE_UNAVAILABLE, STATE_ON, False),
        # A motion sensor's first motion since Home Assistant started.
        (STATE_UNKNOWN, STATE_ON, True),
        (None, STATE_ON, False),
        (STATE_OFF, None, False),
    ],
)
def test_a_binary_sensor_fires_when_it_turns_on(
    old: str | None, new: str | None, fires: bool
) -> None:
    states = [None if value is None else State(MOTION, value) for value in (old, new)]
    assert fired(*states) is fires


@pytest.mark.parametrize(
    ("old", "new", "fires"),
    [
        (STATE_UNKNOWN, "2026-10-25T08:00:00+00:00", True),
        ("2026-10-25T08:00:00+00:00", "2026-10-25T08:05:00+00:00", True),
        ("2026-10-25T08:00:00+00:00", "2026-10-25T08:00:00+00:00", False),
        (STATE_UNAVAILABLE, "2026-10-25T08:00:00+00:00", False),
        ("2026-10-25T08:00:00+00:00", STATE_UNAVAILABLE, False),
        ("2026-10-25T08:00:00+00:00", STATE_UNKNOWN, False),
        (None, "2026-10-25T08:00:00+00:00", False),
    ],
)
def test_an_event_entity_fires_with_each_new_event(
    old: str | None, new: str | None, fires: bool
) -> None:
    states = [None if value is None else State(DOORBELL, value) for value in (old, new)]
    assert fired(*states) is fires


async def test_an_event_entity_triggers_the_cameras(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    frames: Frames,
    waits: list[float],
) -> None:
    entry = await garden(hass, aioclient_mock, shown, trigger=DOORBELL, frames=1)
    frames[GARDEN] = [b"garden 1", b"garden 2"]
    hass.states.async_set(DOORBELL, "2026-10-25T08:00:00+00:00")
    await settle(hass)
    hass.states.async_set(DOORBELL, "2026-10-25T08:00:00+00:00", {"event_type": "ring"})
    await settle(hass)
    assert len(journal(entry).records) == 1
    hass.states.async_set(DOORBELL, "2026-10-25T08:05:00+00:00")
    await settle(hass)
    assert len(journal(entry).records) == 2
    assert waits == []


async def test_a_trigger_during_a_burst_adds_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown, frames: Frames
) -> None:
    entry = await garden(hass, aioclient_mock, shown, frames=2)
    frames[GARDEN] = [b"garden 1", b"garden 2", b"garden 3"]
    waiting = asyncio.Event()
    go_on = asyncio.Event()

    async def wait(seconds: float) -> None:
        waiting.set()
        await go_on.wait()

    with patch("custom_components.pet_recognition.cameras.wait", wait):
        hass.states.async_set(MOTION, STATE_ON)
        await waiting.wait()
        hass.states.async_set(MOTION, STATE_OFF)
        hass.states.async_set(MOTION, STATE_ON)
        await hass.async_block_till_done()
        go_on.set()
        await settle(hass)
    assert len(journal(entry).records) == 2
    assert frames[GARDEN] == [b"garden 3"]

    # Once the burst is over, the trigger takes another one.
    hass.states.async_set(MOTION, STATE_OFF)
    frames[GARDEN] += [b"garden 4"]
    with patch("custom_components.pet_recognition.cameras.wait", wait):
        await fire(hass)
    assert len(journal(entry).records) == 4


async def test_a_picture_the_source_has_is_not_taken_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    frames: Frames,
    waits: list[float],
) -> None:
    entry = await garden(hass, aioclient_mock, shown)
    # The same picture twice, then the camera fails.
    frames[GARDEN] = [b"still", b"still"]
    await fire(hass)
    assert len(journal(entry).records) == 1
    frames[GARDEN] = [b"still", b"moved"]
    await fire(hass)
    assert len(journal(entry).records) == 2
    assert waits == [10, 10, 10, 10]


async def test_a_burst_stops_when_it_is_cancelled(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    waits: list[float],
) -> None:
    taken: list[str] = []

    async def get(hass: HomeAssistant, entity_id: str, timeout: int = 10) -> Image:
        # As the camera does: a cancellation becomes an error.
        taken.append(entity_id)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise HomeAssistantError("Unable to get image") from None
        raise AssertionError  # pragma: no cover - the task was cancelled above

    entry = await garden(hass, aioclient_mock, shown, GARDEN, PATIO)
    with patch("custom_components.pet_recognition.cameras.async_get_image", get):
        await fire(hass)
    assert taken == [GARDEN]
    assert waits == []
    assert journal(entry).queue == []


async def test_sources_without_cameras_follow_no_trigger(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    frames: Frames,
) -> None:
    hass.states.async_set(MOTION, STATE_OFF)
    entry = await set_up(
        hass,
        aioclient_mock,
        shown,
        pet(PET_A),
        # A trigger kept from cameras that were removed.
        source("image.feeder", trigger=MOTION),
    )
    frames[GARDEN] = [b"garden 1"]
    await fire(hass)
    assert journal(entry).records == []
    assert frames[GARDEN] == [b"garden 1"]
