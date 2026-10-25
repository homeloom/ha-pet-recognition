"""Fixtures of the integration's tests."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.image import Image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .watching import Shown


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture(autouse=True)
def config_directory(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """Keep the files the integration writes in a directory of the test."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("hass").config.config_dir = str(tmp_path)


@pytest.fixture
def shown() -> Iterator[Shown]:
    """The picture each image entity shows."""
    pictures: Shown = {}

    async def get(hass: HomeAssistant, entity_id: str, timeout: int = 10) -> Image:
        if entity_id not in pictures:
            raise HomeAssistantError("no picture")
        return Image(content_type="image/jpeg", content=pictures[entity_id])

    with patch("custom_components.pet_recognition.sources.async_get_image", get):
        yield pictures
