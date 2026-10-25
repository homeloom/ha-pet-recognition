"""Tests of the diagnostics, which leave out the engine's address and key."""

from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN
from custom_components.pet_recognition.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.pet_recognition.examples import Candidate, Origin

from .answers import CROP, KEY, MODEL, URL, error, info, unit
from .common import configured


async def test_the_diagnostics_of_a_connected_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await async_get_config_entry_diagnostics(hass, entry) == {
        "integration_version": "0.0.0",
        "state": "loaded",
        "engine": {
            "version": "0.1.0",
            "model": MODEL,
            "species": ["bird", "cat", "dog"],
            "max_bytes": 20 * 1024 * 1024,
            "max_pixels": 40_000_000,
        },
        "connected": True,
        "last_error": None,
        "failing_since": None,
        "pets": [],
        "sources": [],
        "pending": 0,
    }


async def test_the_diagnostics_of_an_engine_that_stopped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", exc=ClientError(f"refused by {URL}"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    started = entry.runtime_data.coordinator.failing_since
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics == {
        "integration_version": "0.0.0",
        "state": "loaded",
        "engine": None,
        "connected": False,
        "last_error": "EngineConnectionError",
        "failing_since": started.isoformat(),
        "pets": [],
        "sources": [],
        "pending": 0,
    }
    assert "engine.test" not in str(diagnostics)
    assert KEY not in str(diagnostics)


async def test_the_diagnostics_of_an_entry_that_did_not_load(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", status=401, json=error("unauthorized"))
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert await async_get_config_entry_diagnostics(hass, entry) == {
        "integration_version": "0.0.0",
        "state": "setup_error",
        "engine": None,
    }


async def test_the_diagnostics_number_the_pets_instead_of_naming_them(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_URL: URL, CONF_API_KEY: KEY},
        subentries_data=[
            ConfigSubentryData(
                data={"species": "cat"}, subentry_type="pet", title="Pet A", unique_id="a" * 32
            ),
            ConfigSubentryData(
                data={"species": "dog"}, subentry_type="pet", title="Pet B", unique_id="b" * 32
            ),
        ],
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    store = entry.runtime_data.examples
    for vector, model in (((1, 0), MODEL), ((0, 1), MODEL), ((1, 0), "old@1")):
        await store.async_add("a" * 32, Candidate(CROP, tuple(unit(*vector)), model), Origin.UPLOAD)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["pets"] == [
        {"pet": "pet_1", "species": "cat", "examples": {"old@1": 1, MODEL: 2}},
        {"pet": "pet_2", "species": "dog", "examples": {}},
    ]
    assert "Pet A" not in str(diagnostics)
    assert "a" * 32 not in str(diagnostics)
