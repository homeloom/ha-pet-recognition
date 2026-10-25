"""Helpers shared by the integration's tests."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pet_recognition.const import DOMAIN

from .answers import KEY, URL

SENSOR = "binary_sensor.pet_recognition_engine"


def configured(
    hass: HomeAssistant, subentries: list[ConfigSubentryData] | None = None, **data: str
) -> MockConfigEntry:
    """An entry of the engine at `URL`, added but not set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Pet Recognition",
        data={CONF_URL: URL, CONF_API_KEY: KEY, **data},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    return entry


def source_data(*entities: str, pets: list[str] | None = None, **changes: object) -> dict[str, Any]:
    """The data of a source subentry that watches `entities`."""
    return {
        "kind": "feeder",
        "entities": list(entities),
        "pets": pets or [],
        "gap": 5,
        "late": 2,
        "every_picture_shows_a_pet": True,
        "trigger": None,
        "frames": 6,
        "interval": 10,
        **changes,
    }
