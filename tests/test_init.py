"""Tests of loading and unloading the integration."""

from __future__ import annotations

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN

from .answers import KEY, MODEL, URL, info


def configured(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data={CONF_URL: URL, CONF_API_KEY: KEY}
    )
    entry.add_to_hass(hass)
    return entry


async def test_the_entry_connects_to_the_engine_and_unloads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.info.model == MODEL
    assert entry.runtime_data.client.url == URL
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_an_engine_that_is_not_ready_is_retried(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", exc=ClientError("refused"))
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "Cannot reach the Pet Recognition Engine" in entry.reason


async def test_a_wrong_engine_is_retried_too(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", json={**info(), "api": 2})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "not answer as a Pet Recognition Engine of a supported version" in entry.reason
