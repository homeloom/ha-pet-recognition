"""Helpers of the tests that watch sources: an engine, a pet and a feeder."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.examples import Candidate, Origin
from custom_components.pet_recognition.journal import Journal

from .answers import CROP, MODEL, URL, analysis, animal, info, unit
from .common import configured, source_data

# The picture each image entity shows, which the fixture `shown` serves.
type Shown = dict[str, bytes]

PET_A = "a" * 32
PET_B = "b" * 32
FEEDER = "image.feeder"
START = datetime(2026, 10, 25, 8, 0, tzinfo=dt_util.UTC)


def engine(aioclient_mock: AiohttpClientMocker, **analyze: object) -> None:
    """Make the engine answer /v1/info, and /v1/analyze with `analyze`."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info(max_bytes=1000))
    aioclient_mock.post(
        f"{URL}/v1/analyze", **(analyze or {"json": analysis(animal("cat", unit(1, 0, 0)))})
    )


def pet(key: str, species: str = "cat") -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"species": species}, subentry_type="pet", title=f"Pet {key[0]}", unique_id=key
    )


def source(*entities: str, title: str = "Feeder", **changes: object) -> ConfigSubentryData:
    return ConfigSubentryData(
        data=source_data(*entities, **changes),
        subentry_type="source",
        title=title,
        unique_id=None,
    )


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def set_up(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    *subentries: ConfigSubentryData,
    connected: bool = True,
) -> MockConfigEntry:
    """Set up Pet A, with one example, and the feeder, which shows a first picture."""
    if connected:
        engine(aioclient_mock)
    else:
        aioclient_mock.get(f"{URL}/v1/info", exc=ClientError("refused"))
    shown[FEEDER] = b"first picture"
    hass.states.async_set(FEEDER, START.isoformat())
    entry = configured(hass, subentries=list(subentries or (pet(PET_A), source(FEEDER))))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    if entry.state is ConfigEntryState.LOADED and PET_A in {
        subentry.unique_id for subentry in entry.subentries.values()
    }:
        store = entry.runtime_data.examples
        await store.async_add(PET_A, Candidate(CROP, tuple(unit(1, 0, 0)), MODEL), Origin.UPLOAD)
    return entry


async def show(
    hass: HomeAssistant, shown: Shown, picture: bytes, minutes: float, entity_id: str = FEEDER
) -> None:
    """Make an entity show a new picture, `minutes` after the start."""
    shown[entity_id] = picture
    hass.states.async_set(entity_id, (START + timedelta(minutes=minutes)).isoformat())
    await settle(hass)


def journal(entry: MockConfigEntry) -> Journal:
    return entry.runtime_data.journal


def analyses(aioclient_mock: AiohttpClientMocker) -> list[Any]:
    return [call for call in aioclient_mock.mock_calls if call[1].path == "/v1/analyze"]


def source_id(entry: MockConfigEntry) -> str:
    (found,) = (key for key, item in entry.subentries.items() if item.subentry_type == "source")
    return found


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await settle(hass)
