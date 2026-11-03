"""Tests of the visits that sensors report, such as a litter box's date of
last use of each pet."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics
from custom_components.pet_recognition.journal import Status

from .test_counts import announced, until, value  # noqa: F401
from .watching import FEEDER, PET_A, PET_B, START, Shown, journal, pet, set_up, settle, source

USE_A = "sensor.pet_a_last_use"
USE_B = "sensor.pet_b_last_use"
VISITS = "sensor.box_visits_today"
VISITS_A = "sensor.box_pet_a_visits_today"
VISITS_B = "sensor.box_pet_b_visits_today"
ENDS = timedelta(minutes=7, seconds=2)


def local(minutes: float) -> str:
    """A time after the start, as a litter box writes it: local, without a time zone."""
    moment = dt_util.as_local(START + timedelta(minutes=minutes))
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def box_id(entry: MockConfigEntry) -> str:
    (found,) = (key for key, item in entry.subentries.items() if item.title == "Box")
    return found


async def litter_box(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    **changes: object,
) -> MockConfigEntry:
    """Pet A and Pet B, the feeder, and a litter box that reports the use of each pet."""
    freezer.move_to(START)
    changes.setdefault("sensor_pets", {USE_A: PET_A, USE_B: PET_B})
    hass.states.async_set(USE_A, local(-60))
    hass.states.async_set(USE_B, "Unknown")
    return await set_up(
        hass,
        aioclient_mock,
        shown,
        pet(PET_A),
        pet(PET_B),
        source(FEEDER),
        source(
            USE_A,
            USE_B,
            title="Box",
            kind="litter_box",
            **changes,
        ),
    )


async def report(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entity_id: str, minutes: float
) -> None:
    """At `minutes` after the start, a sensor reports a use at that time."""
    freezer.move_to(START + timedelta(minutes=minutes))
    hass.states.async_set(entity_id, local(minutes))
    await settle(hass)


async def test_each_new_value_of_a_sensor_is_a_visit_of_its_pet(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],  # noqa: F811
) -> None:
    entry = await litter_box(hass, aioclient_mock, shown, freezer)
    box = box_id(entry)
    # What the sensors show when the source is added is no visit.
    assert journal(entry).of(box) == []

    await report(hass, freezer, USE_A, 10)
    await report(hass, freezer, USE_B, 30)
    (use_a, use_b) = journal(entry).of(box)
    assert (use_a.pets, use_a.status, use_a.detections) == ((PET_A,), Status.REPORTED, [])
    assert use_b.pets == (PET_B,)
    # The time it reports, in the time zone of Home Assistant.
    assert use_a.taken == (START + timedelta(minutes=10)).timestamp()
    assert use_a.entity == USE_A

    # The same value again, after a restart of its integration, then its
    # own "Unknown" after midnight, and a time before the last one: no visit.
    for state in (STATE_UNAVAILABLE, local(10), "Unknown", local(5), local(10)):
        hass.states.async_set(USE_A, state)
        await settle(hass)
    assert len(journal(entry).of(box)) == 2

    await until(hass, freezer, START + timedelta(minutes=30) + ENDS)
    assert value(hass, VISITS) == ("2", 0)
    assert value(hass, VISITS_A) == ("1", 0)
    assert value(hass, VISITS_B) == ("1", 0)
    assert [(item["pets"], item["unsure"]) for item in announced] == [
        (["Pet a"], False),
        (["Pet b"], False),
    ]
    # Nothing to show: no picture of the box is kept.
    kept = journal(entry).kept_frames()
    assert journal(entry).last_picture(box) is None
    assert use_a.id not in kept

    # The values seen are kept across a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    hass.states.async_set(USE_A, STATE_UNAVAILABLE)
    hass.states.async_set(USE_A, local(10))
    await settle(hass)
    assert len(journal(entry).of(box)) == 2
    assert value(hass, VISITS_A) == ("1", 0)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    (_feeder, counted) = diagnostics["sources"]
    assert (counted["sensors"], counted["pictures"]) == (2, {"reported": 2})


async def test_a_sensor_without_its_pet_reports_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A pet removed, and a sensor whose pet was never chosen.
    entry = await litter_box(hass, aioclient_mock, shown, freezer, sensor_pets={USE_A: "gone"})
    await report(hass, freezer, USE_A, 10)
    await report(hass, freezer, USE_B, 20)
    assert journal(entry).of(box_id(entry)) == []


async def test_old_reports_go_past_the_limit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.pet_recognition.journal.PICTURE_LIMIT", 2)
    entry = await litter_box(hass, aioclient_mock, shown, freezer)
    for minutes in (10, 20, 30):
        await report(hass, freezer, USE_A, minutes)
    taken = [record.taken for record in journal(entry).of(box_id(entry))]
    assert taken == [(START + timedelta(minutes=minutes)).timestamp() for minutes in (20, 30)]


def test_a_time_with_a_time_zone_is_kept() -> None:
    from custom_components.pet_recognition.sources import _earlier, _taken

    moment = datetime(2026, 10, 25, 8, 0, tzinfo=dt_util.UTC)
    state = type("State", (), {"state": moment.isoformat(), "last_changed": moment})()
    assert _taken(state) == moment.timestamp()  # type: ignore[arg-type]
    # Times of different kinds are not compared.
    assert not _earlier("2026-10-25 08:00:00", moment.isoformat())
    assert not _earlier("a meal", moment.isoformat())
