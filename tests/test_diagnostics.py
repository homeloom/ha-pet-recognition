"""Tests of the diagnostics, which leave out the engine's address and key."""

from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .answers import KEY, MODEL, URL, error, info
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
