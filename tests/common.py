"""Helpers shared by the integration's tests."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pet_recognition.const import DOMAIN

from .answers import KEY, URL

SENSOR = "binary_sensor.pet_recognition_engine"


def configured(hass: HomeAssistant, **data: str) -> MockConfigEntry:
    """An entry of the engine at `URL`, added but not set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Pet Recognition",
        data={CONF_URL: URL, CONF_API_KEY: KEY, **data},
    )
    entry.add_to_hass(hass)
    return entry
