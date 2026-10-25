"""Tests of adding and changing the sources of pictures."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .answers import URL, info
from .common import configured, source_data

ADVANCED = {"gap": 5, "late": 2, "every_picture_shows_a_pet": True}


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


def source(title: str, *entities: str, pets: list[str] | None = None) -> ConfigSubentryData:
    return ConfigSubentryData(
        data=source_data(*entities, pets=pets), subentry_type="source", title=title, unique_id=None
    )


async def start(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "source"), context={"source": SOURCE_USER}
    )
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": " Feeder ", "kind": kind}
    )


def suggested(result: dict[str, Any], *path: str) -> object:
    """The value a form suggests for a field, or a field of a section."""
    schema: vol.Schema = result["data_schema"]
    for name in path[:-1]:
        schema = next(value for key, value in schema.schema.items() if key == name).schema
    (key,) = (key for key in schema.schema if key == path[-1])
    return key.description["suggested_value"]


def advanced(result: dict[str, Any]) -> dict[str, object]:
    return {name: suggested(result, "advanced", name) for name in ADVANCED}


async def test_a_source_is_added(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await set_up(hass, aioclient_mock, pet("a" * 32, "Pet A"), pet("b" * 32, "Pet B"))
    result = await start(hass, entry, "feeder")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "pictures")
    assert result["description_placeholders"] == {"source": "Feeder"}
    assert advanced(result) == ADVANCED
    pets = result["data_schema"].schema["pets"].config["options"]
    assert pets == [{"value": "a" * 32, "label": "Pet A"}, {"value": "b" * 32, "label": "Pet B"}]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "entities": ["image.feeder", "image.feeder", "image.bowl"],
            "pets": ["a" * 32],
            "advanced": {"gap": 10.0, "late": 0.0, "every_picture_shows_a_pet": False},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = (item for item in entry.subentries.values() if item.subentry_type == "source")
    assert subentry.title == "Feeder"
    assert dict(subentry.data) == {
        "kind": "feeder",
        "entities": ["image.feeder", "image.bowl"],
        "pets": ["a" * 32],
        "gap": 10,
        "late": 0,
        "every_picture_shows_a_pet": False,
    }


@pytest.mark.parametrize(("kind", "every"), [("litter_box", True), ("pet_door", False)])
async def test_the_kind_sets_whether_every_picture_shows_a_pet(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kind: str, every: bool
) -> None:
    entry = await set_up(hass, aioclient_mock)
    result = await start(hass, entry, kind)
    assert advanced(result)["every_picture_shows_a_pet"] is every
    assert result["data_schema"].schema["pets"].config["options"] == []


async def test_a_source_needs_a_name(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up(hass, aioclient_mock)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "source"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": " ", "kind": "other"}
    )
    assert result["errors"] == {"name": "empty_name"}


@pytest.mark.parametrize(
    ("entities", "error"),
    [
        ([], "no_entity"),
        (["image.feeder", "image.taken"], "entity_in_use"),
    ],
)
async def test_the_entities_of_a_source_are_checked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entities: list[str], error: str
) -> None:
    entry = await set_up(hass, aioclient_mock, source("Bowl", "image.taken"))
    result = await start(hass, entry, "feeder")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"entities": entities, "advanced": ADVANCED}
    )
    assert (result["step_id"], result["errors"]) == ("pictures", {"entities": error})
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
    assert suggested(result, "name") == "Feeder"
    assert suggested(result, "entities") == ["image.feeder"]
    # A pet that is gone is not shown.
    assert suggested(result, "pets") == []

    changes = {
        "name": "",
        "kind": "water",
        "entities": ["image.bowl"],
        "pets": [],
        "advanced": ADVANCED,
    }
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changes)
    assert result["errors"] == {"entities": "entity_in_use", "name": "empty_name"}
    changes |= {"name": "Fountain", "entities": ["image.feeder", "image.fountain"]}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changes)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await hass.async_block_till_done()
    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Fountain"
    assert subentry.data["kind"] == "water"
    assert subentry.data["entities"] == ["image.feeder", "image.fountain"]
