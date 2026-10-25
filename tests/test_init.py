"""Tests of loading, unloading and removing the integration."""

from __future__ import annotations

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN

from .answers import MODEL, URL, error, info
from .common import SENSOR, configured


async def test_the_entry_connects_to_the_engine_and_unloads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data.model == MODEL
    assert entry.runtime_data.client.url == URL
    assert hass.states.get(SENSOR).state == STATE_ON
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_the_entry_loads_while_the_engine_is_stopped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", exc=ClientError("refused"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data is None
    assert hass.states.get(SENSOR).state == STATE_OFF


async def test_the_entry_loads_beside_a_wrong_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", json={"status": "ok"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SENSOR).state == STATE_OFF


async def test_a_refused_key_stops_the_setup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", status=401, json=error("unauthorized"))
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason == "The Pet Recognition Engine refused its API key"


async def test_unloading_the_entry_removes_its_repairs(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    issue_registry: ir.IssueRegistry,
) -> None:
    entry = configured(hass)
    # An engine without this API raises one repair; the two others are added here.
    aioclient_mock.get(f"{URL}/v1/info", status=404, json=error("not_found"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    for issue in ("engine_unreachable", "engine_version_mismatch"):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="engine_unreachable",
        )
    assert len(issue_registry.issues) == 3
    # Such as when the user disables the entry, which nothing would clear.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not issue_registry.issues
