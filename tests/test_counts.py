"""Tests of the visits counted per day, and of their announcement."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics
from custom_components.pet_recognition.event import VisitEvent
from custom_components.pet_recognition.examples import Candidate, Origin

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
    settle,
    source,
    source_id,
)

VISITS = "sensor.feeder_visits_today"
MEALS_A = "sensor.feeder_pet_a_meals_today"
MEALS_B = "sensor.feeder_pet_b_meals_today"
VISIT = "event.feeder_visit"

A = unit(1, 0, 0)
B = unit(0, 1, 0)
OTHER = unit(0, 0, 1)

# A visit ends 7 minutes after its last picture: the gap of 5 minutes, and 2
# for late pictures.
ENDS = timedelta(minutes=7, seconds=2)


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The visits the event entities announce."""
    calls: list[dict[str, Any]] = []
    trigger = EventEntity._trigger_event

    def record(
        self: VisitEvent, event_type: str, event_attributes: dict[str, Any] | None = None
    ) -> None:
        calls.append({"event_type": event_type, **(event_attributes or {})})
        trigger(self, event_type, event_attributes)

    monkeypatch.setattr(VisitEvent, "_trigger_event", record)
    return calls


async def two_pets(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    **changes: object,
) -> MockConfigEntry:
    """Pet A and Pet B, each with an example, the other animal too, and the feeder."""
    freezer.move_to(START)
    entry = await set_up(
        hass, aioclient_mock, shown, pet(PET_A), pet(PET_B), source(FEEDER, **changes)
    )
    store = entry.runtime_data.examples
    for key, vector in ((PET_B, B), (OTHER_ANIMAL, OTHER)):
        await store.async_add(key, Candidate(CROP, tuple(vector), MODEL), Origin.UPLOAD)
    return entry


async def come(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    minutes: float,
    *animals: dict[str, object],
) -> None:
    """At `minutes` after the start, the feeder takes a picture of `animals`."""
    taken = START + timedelta(minutes=minutes)
    freezer.move_to(taken)
    engine(aioclient_mock, json=analysis(*animals))
    shown[FEEDER] = f"picture {minutes}".encode()
    hass.states.async_set(FEEDER, taken.isoformat())
    await settle(hass)


async def until(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moment: datetime) -> None:
    freezer.move_to(moment)
    async_fire_time_changed(hass)
    await settle(hass)


def value(hass: HomeAssistant, entity_id: str) -> tuple[str, Any]:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state, state.attributes["yesterday"]


def journal_ids(hass: HomeAssistant, index: int) -> str:
    """The ID of a record of the only entry, in the order taken."""
    entry = hass.config_entries.async_entries("pet_recognition")[0]
    return journal(entry).of(source_id(entry))[index].id


async def test_visits_count_once_they_ended(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
) -> None:
    await two_pets(hass, aioclient_mock, shown, freezer)
    state = hass.states.get(MEALS_A)
    assert state is not None
    assert state.attributes["state_class"] == "total"
    # Midnight in the time zone of Home Assistant.
    midnight = dt_util.start_of_local_day(dt_util.as_local(START))
    assert state.attributes["last_reset"] == midnight.isoformat()
    for entity_id in (VISITS, MEALS_A, MEALS_B):
        assert value(hass, entity_id) == ("0", 0)

    # Pet A, then Pet B, at the same visit: nothing counts while it goes on.
    await come(hass, aioclient_mock, shown, freezer, 1, animal(embedding=A))
    await come(hass, aioclient_mock, shown, freezer, 2, animal(embedding=B))
    await until(hass, freezer, START + timedelta(minutes=2) + ENDS - timedelta(seconds=2))
    assert value(hass, VISITS) == ("0", 0)
    assert announced == []
    await until(hass, freezer, START + timedelta(minutes=2) + ENDS)
    for entity_id in (VISITS, MEALS_A, MEALS_B):
        assert value(hass, entity_id) == ("1", 0)
    (first,) = announced
    assert first == {
        "event_type": "visit_ended",
        "visit_id": journal_ids(hass, 0),
        "pets": ["Pet a", "Pet b"],
        "other_animal": False,
        "unsure": False,
        "meal": None,
        "start": (START + timedelta(minutes=1)).isoformat(),
        "end": (START + timedelta(minutes=2)).isoformat(),
        "pictures": 2,
    }
    state = hass.states.get(VISIT)
    assert state is not None
    assert state.attributes["event_type"] == "visit_ended"
    assert state.attributes["pets"] == ["Pet a", "Pet b"]

    # A visit where nothing is found, at a feeder, counts as unknown; then
    # the other animal.
    await come(hass, aioclient_mock, shown, freezer, 20)
    await come(hass, aioclient_mock, shown, freezer, 40, animal(embedding=OTHER))
    await until(hass, freezer, START + timedelta(minutes=40) + ENDS)
    assert value(hass, VISITS) == ("3", 0)
    assert value(hass, MEALS_A) == ("1", 0)
    assert [(item["pets"], item["other_animal"], item["unsure"]) for item in announced[1:]] == [
        ([], False, True),
        ([], True, False),
    ]

    # Every visit is announced once, even after a reload.
    entry = hass.config_entries.async_entries("pet_recognition")[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert len(announced) == 3
    assert value(hass, VISITS) == ("3", 0)

    # At midnight the counts start again, and those of the day before stay
    # as an attribute.
    await come(hass, aioclient_mock, shown, freezer, 60, animal(embedding=A))
    await until(hass, freezer, midnight + timedelta(days=1, seconds=1))
    assert value(hass, VISITS) == ("0", 4)
    assert value(hass, MEALS_A) == ("0", 2)
    assert value(hass, MEALS_B) == ("0", 1)
    state = hass.states.get(MEALS_A)
    assert state is not None
    assert state.attributes["last_reset"] == (midnight + timedelta(days=1)).isoformat()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["sources"][0]["visits_today"] == 0


async def test_a_visit_that_ended_while_stopped_is_announced_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
) -> None:
    entry = await two_pets(hass, aioclient_mock, shown, freezer)
    await come(hass, aioclient_mock, shown, freezer, 1, animal(embedding=A))
    assert await hass.config_entries.async_unload(entry.entry_id)
    freezer.move_to(START + timedelta(hours=1))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert [item["pets"] for item in announced] == [["Pet a"]]
    assert value(hass, MEALS_A) == ("1", 0)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert len(announced) == 1


async def test_a_source_seen_first_announces_none_of_its_visits(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
) -> None:
    entry = await two_pets(hass, aioclient_mock, shown, freezer)
    await come(hass, aioclient_mock, shown, freezer, 1, animal(embedding=A))
    kept = journal(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    # As after an update from a release that did not announce visits.
    kept.announced.clear()
    freezer.move_to(START + timedelta(hours=1))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert announced == []
    assert journal(entry).announced == {
        source_id(entry): (START + timedelta(minutes=1)).timestamp()
    }
    # It still counts.
    assert value(hass, MEALS_A) == ("1", 0)
    await come(hass, aioclient_mock, shown, freezer, 70, animal(embedding=B))
    await until(hass, freezer, START + timedelta(minutes=70) + ENDS)
    assert [item["pets"] for item in announced] == [["Pet b"]]


async def test_counts_of_pets_no_longer_expected_are_removed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await two_pets(hass, aioclient_mock, shown, freezer, pets=[PET_A], kind="litter_box")
    registry = er.async_get(hass)
    # At a litter box, they are visits.
    assert hass.states.get("sensor.feeder_pet_a_visits_today") is not None
    assert hass.states.get("sensor.feeder_pet_b_visits_today") is None

    feeder = entry.subentries[source_id(entry)]
    hass.config_entries.async_update_subentry(entry, feeder, data={**feeder.data, "pets": [PET_B]})
    await settle(hass)
    assert registry.async_get("sensor.feeder_pet_a_visits_today") is None
    assert hass.states.get("sensor.feeder_pet_b_visits_today") is not None

    # Pet B is removed: every pet left is expected.
    (pet_b,) = (key for key, subentry in entry.subentries.items() if subentry.unique_id == PET_B)
    assert hass.config_entries.async_remove_subentry(entry, pet_b)
    await settle(hass)
    assert registry.async_get("sensor.feeder_pet_b_visits_today") is None
    assert hass.states.get("sensor.feeder_pet_a_visits_today") is not None
    assert registry.async_get(VISITS) is not None


MEAL = "image.feeder_meal"


async def meal_feeder(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> MockConfigEntry:
    """Two pets, and a feeder with a picture of each visit and one of each meal."""
    shown[MEAL] = b"first meal"
    hass.states.async_set(MEAL, START.isoformat())
    return await two_pets(
        hass, aioclient_mock, shown, freezer, entities=[FEEDER, MEAL], meal_pictures=[MEAL]
    )


async def eat(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    minutes: float,
    *animals: dict[str, object],
) -> None:
    """At `minutes` after the start, the feeder takes a picture of a meal of `animals`."""
    taken = START + timedelta(minutes=minutes)
    freezer.move_to(taken)
    engine(aioclient_mock, json=analysis(*animals))
    shown[MEAL] = f"meal {minutes}".encode()
    hass.states.async_set(MEAL, taken.isoformat())
    await settle(hass)


async def test_with_meal_pictures_the_pets_count_per_meal(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],
) -> None:
    entry = await meal_feeder(hass, aioclient_mock, shown, freezer)
    # Pet A eats; Pet B only comes by.
    await come(hass, aioclient_mock, shown, freezer, 1, animal(embedding=A))
    await eat(hass, aioclient_mock, shown, freezer, 2, animal(embedding=A))
    await come(hass, aioclient_mock, shown, freezer, 20, animal(embedding=B))
    # A long meal of Pet A: its picture comes after the gap, so it is a
    # visit of its own, and the meal counts once.
    await come(hass, aioclient_mock, shown, freezer, 40, animal(embedding=A))
    await eat(hass, aioclient_mock, shown, freezer, 46, animal(embedding=A))
    await until(hass, freezer, START + timedelta(minutes=46) + ENDS)
    assert value(hass, VISITS) == ("4", 0)
    assert value(hass, MEALS_A) == ("2", 0)
    assert value(hass, MEALS_B) == ("0", 0)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["sources"][0]["meal_pictures"] == 1
    assert [(item["pets"], item["meal"]) for item in announced] == [
        (["Pet a"], True),
        (["Pet b"], False),
        (["Pet a"], False),
        (["Pet a"], True),
    ]

    # Without meal pictures, every visit counts again.
    feeder = entry.subentries[source_id(entry)]
    hass.config_entries.async_update_subentry(
        entry, feeder, data={**feeder.data, "meal_pictures": []}
    )
    await settle(hass)
    assert value(hass, MEALS_A) == ("3", 0)
    assert value(hass, MEALS_B) == ("1", 0)
