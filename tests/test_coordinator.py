"""Tests of the engine's health: the connectivity sensor and the repairs."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN

from .answers import URL, error, info
from .common import SENSOR, configured


async def set_up(hass: HomeAssistant) -> MockConfigEntry:
    entry = configured(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry


def answer(aioclient_mock: AiohttpClientMocker, **kwargs: object) -> None:
    """Make the engine answer /v1/info with `kwargs` from now on."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", **kwargs)


async def later(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int) -> None:
    """Let `minutes` pass, one update a minute."""
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_a_stopped_engine_turns_the_sensor_off_and_raises_a_repair_later(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    answer(aioclient_mock, json=info(version="0.0.0"))
    entry = await set_up(hass)
    assert hass.states.get(SENSOR).state == STATE_ON

    answer(aioclient_mock, exc=ClientError("refused"))
    await later(hass, freezer, 1)
    assert hass.states.get(SENSOR).state == STATE_OFF
    assert entry.runtime_data.coordinator.failing_since is not None
    await later(hass, freezer, 14)
    # Not yet a quarter of an hour since the first failure.
    assert not issue_registry.issues
    await later(hass, freezer, 1)
    issue = issue_registry.async_get_issue(DOMAIN, "engine_unreachable")
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "engine_unreachable"
    assert issue.translation_placeholders == {"url": URL}

    answer(aioclient_mock, json=info(version="0.0.0"))
    await later(hass, freezer, 1)
    assert hass.states.get(SENSOR).state == STATE_ON
    assert entry.runtime_data.coordinator.failing_since is None
    assert not issue_registry.issues


@pytest.mark.parametrize(
    "kwargs",
    [
        # A failing engine, or a proxy in front of a stopped one.
        {"status": 502, "text": "Bad gateway"},
        # Another service at the engine's address.
        {"json": {"status": "ok"}},
        # An engine that refuses the request in a way it should not.
        {"status": 400, "json": error("invalid_request")},
    ],
)
async def test_an_answer_that_cannot_be_used_counts_as_a_stopped_engine(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
    kwargs: dict[str, object],
) -> None:
    answer(aioclient_mock, **kwargs)
    await set_up(hass)
    await later(hass, freezer, 15)
    assert hass.states.get(SENSOR).state == STATE_OFF
    assert issue_registry.async_get_issue(DOMAIN, "engine_unreachable")


async def test_the_engine_is_watched_with_its_sensor_disabled(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    answer(aioclient_mock, json=info(version="0.0.0"))
    await set_up(hass)
    entity_registry.async_update_entity(SENSOR, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    # Disabling an entity reloads the entry after a delay.
    await later(hass, freezer, 1)
    assert hass.states.get(SENSOR) is None

    answer(aioclient_mock, exc=ClientError("refused"))
    # A quarter of an hour after the first failure.
    await later(hass, freezer, 16)
    assert aioclient_mock.call_count == 16
    assert issue_registry.async_get_issue(DOMAIN, "engine_unreachable")


async def test_an_engine_that_dropped_this_api_asks_to_update_the_integration(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    answer(aioclient_mock, exc=ClientError("refused"))
    entry = await set_up(hass)
    await later(hass, freezer, 15)
    assert issue_registry.async_get_issue(DOMAIN, "engine_unreachable")

    # The engine is back, without version 1: it answers, so it is not unreachable.
    answer(aioclient_mock, status=404, json=error("not_found", "Not Found"))
    await later(hass, freezer, 1)
    assert hass.states.get(SENSOR).state == STATE_OFF
    assert entry.runtime_data.coordinator.failing_since is None
    assert not issue_registry.async_get_issue(DOMAIN, "engine_unreachable")
    issue = issue_registry.async_get_issue(DOMAIN, "engine_incompatible")
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "engine_incompatible"
    assert issue.translation_placeholders == {"url": URL, "api": "1"}

    answer(aioclient_mock, json=info(version="0.0.0"))
    await later(hass, freezer, 1)
    assert hass.states.get(SENSOR).state == STATE_ON
    assert not issue_registry.issues


@pytest.mark.parametrize(
    ("integration", "engine", "translation_key"),
    [("0.1.0", "0.2.0", "engine_version_newer"), ("0.2.0", "0.1.0", "engine_version_older")],
)
async def test_releases_that_differ_raise_a_repair(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
    integration: str,
    engine: str,
    translation_key: str,
) -> None:
    answer(aioclient_mock, json=info(version=integration))
    entry = await set_up(hass)
    coordinator = entry.runtime_data.coordinator
    coordinator.version = integration
    await later(hass, freezer, 1)
    assert not issue_registry.issues

    answer(aioclient_mock, json=info(version=engine))
    await later(hass, freezer, 1)
    # Both still work together.
    assert hass.states.get(SENSOR).state == STATE_ON
    issue = issue_registry.async_get_issue(DOMAIN, "engine_version_mismatch")
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == translation_key
    assert issue.translation_placeholders == {"engine": engine, "integration": integration}

    coordinator.version = engine
    await later(hass, freezer, 1)
    assert not issue_registry.issues


async def test_the_integration_compares_its_own_release(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    answer(aioclient_mock, json=info(version="0.1.0"))
    entry = await set_up(hass)
    coordinator = entry.runtime_data.coordinator
    # The manifest's version, which is not a release before the first one.
    assert coordinator.version == "0.0.0"
    assert not issue_registry.issues
    coordinator.version = "0.2.0"
    await later(hass, freezer, 1)
    assert issue_registry.async_get_issue(DOMAIN, "engine_version_mismatch")


@pytest.mark.parametrize("version", ["0.0.0", "dev", "0.2.0.dev3", "0.2.0b1", "custom build"])
async def test_a_version_that_is_not_a_release_is_not_compared(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
    version: str,
) -> None:
    # Such as an engine built by hand, without a version.
    answer(aioclient_mock, json=info(version=version))
    entry = await set_up(hass)
    entry.runtime_data.coordinator.version = "0.1.0"
    await later(hass, freezer, 1)
    assert hass.states.get(SENSOR).state == STATE_ON
    assert not issue_registry.issues


async def test_a_key_refused_later_asks_for_a_new_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    answer(aioclient_mock, json=info(version="0.0.0"))
    await set_up(hass)
    answer(aioclient_mock, status=401, json=error("unauthorized"))
    await later(hass, freezer, 1)
    assert hass.states.get(SENSOR).state == STATE_OFF
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == SOURCE_REAUTH
    # Polling waits for the new key.
    await later(hass, freezer, 5)
    assert aioclient_mock.call_count == 1
