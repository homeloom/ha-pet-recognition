"""Tests of the options of the entry."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics

from .answers import analysis, animal, unit
from .watching import (
    PET_A,
    START,
    Shown,
    analyses,
    engine,
    journal,
    set_up,
    settle,
    show,
    tick,
)

CHANGED = {
    "threshold": 0.95,
    "margin": 0.1,
    "min_area": 2.5,
    "detection_limit": 3,
    "retention": 2,
}


async def shown_defaults(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """The values the options form shows, and its flow ID."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    advanced = result["data_schema"].schema["advanced"]
    defaults = {
        str(key): key.default() for key in advanced.schema.schema if isinstance(key, vol.Required)
    }
    return {"flow_id": result["flow_id"], **defaults}


async def change(hass: HomeAssistant, entry: MockConfigEntry, **values: float) -> None:
    flow = await shown_defaults(hass, entry)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"advanced": {**CHANGED, **values}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await settle(hass)


def analysis_of(embedding: list[float]) -> dict[str, Any]:
    return analysis(animal(embedding=embedding))


async def test_the_options_start_from_their_defaults(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    flow = await shown_defaults(hass, entry)
    del flow["flow_id"]
    assert flow == {
        "threshold": 0.6,
        "margin": 0.04,
        "min_area": 1,
        "detection_limit": 5,
        "retention": 7,
    }
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["options"] == {
        "threshold": 0.6,
        "margin": 0.04,
        "min_area": 0.01,
        "detection_limit": 5,
        "retention_days": 7,
        "phones": 0,
        "ask_while_learning": True,
    }

    await change(hass, entry)
    assert entry.options == {**CHANGED, "phones": [], "ask_while_learning": True}
    flow = await shown_defaults(hass, entry)
    del flow["flow_id"]
    assert flow == CHANGED


async def test_the_options_tune_the_recognition(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    # Close to Pet A's example, but not enough for the new threshold.
    near = unit(1, 0.5, 0)
    await change(hass, entry)
    engine(aioclient_mock, json=analysis_of(near))
    await show(hass, shown, b"a meal", 1)
    (call,) = analyses(aioclient_mock)
    assert call[1].query == {"min_area": "0.025", "limit": "3", "species": "cat"}
    (record,) = journal(entry).records
    assert record.pets == ()
    assert record.resembles == PET_A

    # With the default threshold, the same animal is Pet A.
    await change(hass, entry, threshold=0.6)
    engine(aioclient_mock, json=analysis_of(near))
    await show(hass, shown, b"another meal", 2)
    assert journal(entry).records[-1].pets == (PET_A,)


async def test_pictures_are_kept_as_long_as_the_options_say(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    entry = await set_up(hass, aioclient_mock, shown)
    await change(hass, entry)
    engine(aioclient_mock)
    await show(hass, shown, b"a meal", 1)
    await show(hass, shown, b"another meal", 30)
    (first, _) = journal(entry).records
    await tick(hass, freezer, timedelta(days=2, hours=1).total_seconds())
    assert first not in journal(entry).records
    assert len(journal(entry).records) == 1
