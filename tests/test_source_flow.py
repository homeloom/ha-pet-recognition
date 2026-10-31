"""Tests of adding and changing the sources of pictures."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .answers import URL, info
from .common import configured, source_data

ADVANCED = {"gap": 5, "late": 2, "every_picture_shows_a_pet": True}
CAMERA = {"trigger": "binary_sensor.garden_motion", "frames": 6, "interval": 10}


async def set_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, *subentries: ConfigSubentryData
) -> MockConfigEntry:
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    entry = configured(hass, subentries=list(subentries))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def pet(key: str, title: str) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"species": "cat"}, subentry_type="pet", title=title, unique_id=key
    )


def source(
    title: str, *entities: str, pets: list[str] | None = None, **changes: object
) -> ConfigSubentryData:
    return ConfigSubentryData(
        data=source_data(*entities, pets=pets, **changes),
        subentry_type="source",
        title=title,
        unique_id=None,
    )


async def start(
    hass: HomeAssistant, entry: MockConfigEntry, kind: str, *entities: str
) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "source"), context={"source": SOURCE_USER}
    )
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": " Feeder ", "kind": kind, "entities": list(entities or ["image.feeder"])},
    )


def fields(result: dict[str, Any], *path: str) -> list[str]:
    """The fields of a form, or of one of its sections."""
    schema: vol.Schema = result["data_schema"]
    for name in path:
        schema = next(value for key, value in schema.schema.items() if key == name).schema
    return [str(key) for key in schema.schema]


def suggested(result: dict[str, Any], *path: str) -> object:
    """The value a form suggests for a field, or a field of a section."""
    schema: vol.Schema = result["data_schema"]
    for name in path[:-1]:
        schema = next(value for key, value in schema.schema.items() if key == name).schema
    (key,) = (key for key in schema.schema if key == path[-1])
    return key.description["suggested_value"]


def collapsed(result: dict[str, Any], name: str) -> bool:
    schema: vol.Schema = result["data_schema"]
    return next(value for key, value in schema.schema.items() if key == name).options["collapsed"]


def advanced(result: dict[str, Any]) -> dict[str, object]:
    return {name: suggested(result, "advanced", name) for name in ADVANCED}


async def test_a_source_of_images_is_added(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up(hass, aioclient_mock, pet("a" * 32, "Pet A"), pet("b" * 32, "Pet B"))
    result = await start(hass, entry, "feeder", "image.feeder", "image.feeder", "image.bowl")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "details")
    assert result["description_placeholders"] == {"source": "Feeder"}
    # Without a camera, there is nothing to trigger.
    assert fields(result) == ["pets", "ask_when_unsure", "meal_pictures", "advanced"]
    assert suggested(result, "ask_when_unsure") is True
    # The meal pictures are chosen among the source's own entities.
    meals = result["data_schema"].schema["meal_pictures"].config
    assert meals["include_entities"] == ["image.feeder", "image.bowl"]
    assert advanced(result) == ADVANCED
    pets = result["data_schema"].schema["pets"].config["options"]
    assert pets == [{"value": "a" * 32, "label": "Pet A"}, {"value": "b" * 32, "label": "Pet B"}]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "pets": ["a" * 32],
            "advanced": {"gap": 10.0, "late": 0.0, "every_picture_shows_a_pet": False},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The entry loads again with the source.
    await hass.async_block_till_done()
    (subentry,) = (item for item in entry.subentries.values() if item.subentry_type == "source")
    assert subentry.title == "Feeder"
    assert dict(subentry.data) == {
        "kind": "feeder",
        "entities": ["image.feeder", "image.bowl"],
        "pets": ["a" * 32],
        "ask_when_unsure": True,
        "meal_pictures": [],
        "gap": 10,
        "late": 0,
        "every_picture_shows_a_pet": False,
        "trigger": None,
        "frames": 6,
        "interval": 10,
    }


async def test_a_source_with_a_camera_needs_a_trigger(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up(hass, aioclient_mock)
    result = await start(hass, entry, "feeder", "camera.garden", "image.feeder")
    assert fields(result) == ["pets", "ask_when_unsure", "meal_pictures", "camera", "advanced"]
    assert fields(result, "camera") == ["trigger", "frames", "interval"]
    assert not collapsed(result, "camera")
    assert (suggested(result, "camera", "frames"), suggested(result, "camera", "interval")) == (
        6,
        10,
    )
    # A camera takes pictures whether a pet is there or not.
    assert advanced(result)["every_picture_shows_a_pet"] is False

    user_input = {"camera": {"frames": 3.0, "interval": 5.0}, "advanced": ADVANCED}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    assert (result["step_id"], result["errors"]) == ("details", {"base": "trigger_needed"})
    user_input["camera"] |= {"trigger": "event.doorbell"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The entry loads again with the source.
    await hass.async_block_till_done()
    (subentry,) = (item for item in entry.subentries.values() if item.subentry_type == "source")
    assert subentry.data["entities"] == ["camera.garden", "image.feeder"]
    assert (subentry.data["trigger"], subentry.data["frames"], subentry.data["interval"]) == (
        "event.doorbell",
        3,
        5,
    )


async def test_the_meal_pictures_are_among_the_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up(hass, aioclient_mock)
    result = await start(hass, entry, "feeder", "image.feeder_visit", "image.feeder_meal")
    user_input = {"meal_pictures": ["image.other"], "advanced": ADVANCED}
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    user_input["meal_pictures"] = ["image.feeder_meal", "image.feeder_meal"]
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = (item for item in entry.subentries.values() if item.subentry_type == "source")
    assert subentry.data["meal_pictures"] == ["image.feeder_meal"]


@pytest.mark.parametrize(("kind", "every"), [("litter_box", True), ("pet_door", False)])
async def test_the_kind_sets_whether_every_picture_shows_a_pet(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kind: str, every: bool
) -> None:
    entry = await set_up(hass, aioclient_mock)
    result = await start(hass, entry, kind)
    assert advanced(result)["every_picture_shows_a_pet"] is every
    assert result["data_schema"].schema["pets"].config["options"] == []


@pytest.mark.parametrize(
    ("name", "entities", "errors"),
    [
        (" ", ["image.feeder"], {"name": "empty_name"}),
        ("Feeder", [], {"entities": "no_entity"}),
        ("Feeder", ["image.feeder", "image.taken"], {"entities": "entity_in_use"}),
        ("", [], {"entities": "no_entity", "name": "empty_name"}),
    ],
)
async def test_the_first_step_is_checked(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    name: str,
    entities: list[str],
    errors: dict[str, str],
) -> None:
    entry = await set_up(hass, aioclient_mock, source("Bowl", "image.taken"))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "source"), context={"source": SOURCE_USER}
    )
    user_input = {"name": name, "kind": "other", "entities": entities}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    assert (result["step_id"], result["errors"]) == ("user", errors)
    # What was entered stays.
    assert suggested(result, "entities") == entities


async def test_a_source_is_changed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up(
        hass,
        aioclient_mock,
        pet("a" * 32, "Pet A"),
        source("Feeder", "image.feeder", pets=["gone"]),
        source("Bowl", "image.bowl"),
    )
    (subentry_id,) = (
        subentry_id for subentry_id, item in entry.subentries.items() if item.title == "Feeder"
    )
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "reconfigure")
    assert fields(result) == [
        "name",
        "kind",
        "entities",
        "pets",
        "ask_when_unsure",
        "meal_pictures",
        "camera",
        "advanced",
    ]
    # A source without a camera has its camera settings out of the way.
    assert collapsed(result, "camera")
    assert suggested(result, "name") == "Feeder"
    assert suggested(result, "entities") == ["image.feeder"]
    assert suggested(result, "camera", "trigger") is None
    # A pet that is gone is not shown.
    assert suggested(result, "pets") == []
    # A source added before the questions asks, and one added before the
    # meals has no meal pictures.
    assert suggested(result, "ask_when_unsure") is True
    assert suggested(result, "meal_pictures") == []

    changes: dict[str, Any] = {
        "name": "",
        "kind": "water",
        "entities": ["image.bowl"],
        "pets": [],
        "ask_when_unsure": False,
        "camera": {"frames": 6, "interval": 10},
        "advanced": ADVANCED,
    }
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changes)
    assert result["errors"] == {"entities": "entity_in_use", "name": "empty_name"}
    changes |= {"name": "Fountain", "entities": ["image.feeder", "camera.fountain"]}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changes)
    assert result["errors"] == {"base": "trigger_needed"}
    changes["camera"] |= {"trigger": "binary_sensor.fountain_motion", "frames": 2}
    # A meal picture of an entity the source no longer has.
    changes["meal_pictures"] = ["image.feeder"]
    changes["entities"] = ["camera.fountain"]
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changes)
    assert result["errors"] == {"meal_pictures": "meal_picture_not_watched"}
    changes["entities"] = ["image.feeder", "camera.fountain"]
    changes["meal_pictures"] = ["image.feeder"]
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changes)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await hass.async_block_till_done()
    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Fountain"
    assert subentry.data["kind"] == "water"
    assert subentry.data["ask_when_unsure"] is False
    assert subentry.data["entities"] == ["image.feeder", "camera.fountain"]
    assert subentry.data["meal_pictures"] == ["image.feeder"]
    assert (subentry.data["trigger"], subentry.data["frames"]) == (
        "binary_sensor.fountain_motion",
        2,
    )


async def test_a_camera_removed_keeps_its_settings(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up(hass, aioclient_mock, source("Garden", "camera.garden", **CAMERA))
    (subentry_id,) = (
        subentry_id for subentry_id, item in entry.subentries.items() if item.title == "Garden"
    )
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert not collapsed(result, "camera")
    assert {name: suggested(result, "camera", name) for name in CAMERA} == CAMERA
    changes = {
        "name": "Garden",
        "kind": "other",
        "entities": ["image.garden"],
        "camera": CAMERA | {"frames": 2},
        "advanced": ADVANCED,
    }
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changes)
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    data = entry.subentries[subentry_id].data
    assert data["entities"] == ["image.garden"]
    assert (data["trigger"], data["frames"]) == ("binary_sensor.garden_motion", 2)
