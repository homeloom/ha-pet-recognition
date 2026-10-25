"""Tests of following the entities of the sources: renamed, or gone."""

from __future__ import annotations

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_OFF
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN
from custom_components.pet_recognition.repairs import async_create_fix_flow

from .watching import FEEDER, PET_A, Shown, journal, pet, set_up, settle, show, source, source_id

MOTION = "binary_sensor.garden_motion"


def issue_id(entry: MockConfigEntry, title: str = "Feeder") -> str:
    (found,) = (
        key
        for key, item in entry.subentries.items()
        if item.subentry_type == "source" and item.title == title
    )
    return f"source_entity_missing_{found}"


def registered(hass: HomeAssistant, domain: str, unique_id: str, object_id: str) -> str:
    return (
        er.async_get(hass)
        .async_get_or_create(domain, "test", unique_id, suggested_object_id=object_id)
        .entity_id
    )


async def test_a_renamed_entity_is_followed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    registered(hass, "image", "feeder", "feeder")
    registered(hass, "binary_sensor", "motion", "garden_motion")
    entry = await set_up(
        hass,
        aioclient_mock,
        shown,
        pet(PET_A),
        source(FEEDER, "camera.garden", trigger=MOTION),
        source("image.bowl", title="Bowl"),
    )
    registry = er.async_get(hass)
    registry.async_update_entity(FEEDER, new_entity_id="image.kitchen_feeder")
    registry.async_update_entity(MOTION, new_entity_id="binary_sensor.kitchen_motion")
    await settle(hass)
    feeder, bowl = (
        entry.subentries[issue_id(entry, title).removeprefix("source_entity_missing_")]
        for title in ("Feeder", "Bowl")
    )
    assert feeder.data["entities"] == ["image.kitchen_feeder", "camera.garden"]
    assert feeder.data["trigger"] == "binary_sensor.kitchen_motion"
    assert bowl.data["entities"] == ["image.bowl"]
    # The entry was loaded again, and follows the new name.
    await show(hass, shown, b"first picture", 1, entity_id="image.kitchen_feeder")
    await show(hass, shown, b"a meal", 2, entity_id="image.kitchen_feeder")
    assert [record.entity for record in journal(entry).records] == ["image.kitchen_feeder"]


async def test_a_source_with_an_entity_gone_raises_a_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(
        hass,
        aioclient_mock,
        shown,
        pet(PET_A),
        source(FEEDER, "image.gone", "camera.garden", trigger=MOTION),
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry))
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == "source_entity_missing"
    assert issue.translation_placeholders == {
        "source": "Feeder",
        "entities": "image.gone, camera.garden, binary_sensor.garden_motion",
    }

    # Entities that come back, in the registry or with a state, clear it.
    registered(hass, "image", "gone", "gone")
    hass.states.async_set(MOTION, STATE_OFF)
    registered(hass, "camera", "garden", "garden")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry)) is None

    # It goes when the entry unloads, since nothing would clear it then.
    er.async_get(hass).async_remove("image.gone")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry)) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry)) is None


async def test_the_entities_are_checked_once_home_assistant_has_started(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    hass.set_state(CoreState.starting)
    entry = await set_up(hass, aioclient_mock, shown, pet(PET_A), source(FEEDER, "image.gone"))
    registered(hass, "image", "other", "other")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry)) is None
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry)) is not None


async def fix(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_init(DOMAIN, data={"issue_id": issue_id(entry)})


async def test_the_repair_chooses_the_entities_of_the_source(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    hass.states.async_set("image.bowl", "2026-10-25T08:00:00+00:00")
    entry = await set_up(
        hass,
        aioclient_mock,
        shown,
        pet(PET_A),
        source(FEEDER, "camera.gone", trigger="binary_sensor.gone"),
        source("image.bowl", title="Bowl"),
    )
    result = await fix(hass, entry)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "entities")
    assert result["description_placeholders"] == {
        "source": "Feeder",
        "entities": "camera.gone, binary_sensor.gone",
    }
    schema = result["data_schema"].schema
    suggested = {str(key): (key.description or {}).get("suggested_value") for key in schema}
    # What is gone is not suggested.
    assert suggested == {"entities": [FEEDER], "trigger": None}

    manager = repairs_flow_manager(hass)
    assert manager is not None
    for user_input, errors in (
        ({"entities": []}, {"entities": "no_entity"}),
        ({"entities": [FEEDER, "image.bowl"]}, {"entities": "entity_in_use"}),
        ({"entities": [FEEDER, "camera.patio"]}, {"base": "trigger_needed"}),
    ):
        result = await manager.async_configure(result["flow_id"], user_input)
        assert result["errors"] == errors
    result = await manager.async_configure(
        result["flow_id"], {"entities": [FEEDER, "camera.patio", FEEDER], "trigger": MOTION}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await settle(hass)
    (subentry,) = (
        item
        for item in entry.subentries.values()
        if item.title == "Feeder" and item.subentry_type == "source"
    )
    assert (subentry.data["entities"], subentry.data["trigger"]) == (
        [FEEDER, "camera.patio"],
        MOTION,
    )


async def test_a_repair_without_cameras_keeps_the_trigger(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(
        hass, aioclient_mock, shown, pet(PET_A), source("camera.gone", trigger=MOTION)
    )
    result = await fix(hass, entry)
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_configure(result["flow_id"], {"entities": [FEEDER]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await settle(hass)
    data = entry.subentries[source_id(entry)].data
    assert (data["entities"], data["trigger"]) == ([FEEDER], MOTION)
    # A trigger without cameras is not watched, so its absence raises nothing.
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry)) is None


async def test_the_repair_of_a_source_removed_stops(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown, pet(PET_A), source("image.gone"))
    result = await fix(hass, entry)
    gone = source_id(entry)
    assert hass.config_entries.async_remove_subentry(entry, gone)
    await settle(hass)
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_configure(result["flow_id"], {"entities": [FEEDER]})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "source_removed")


async def test_a_repair_names_its_source(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError, match="does not say which source"):
        await async_create_fix_flow(hass, "source_entity_missing_x", None)
