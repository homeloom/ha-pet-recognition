"""Fixtures of the integration's tests."""

import contextlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.image import Image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .asking import Phones, register_phones
from .common import Upload
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


@pytest.fixture
def phones(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> Phones:
    """Two phones of the companion app, and what the integration sends them."""
    return register_phones(hass, monkeypatch)


@pytest.fixture
def upload(tmp_path: Path) -> Iterator[Upload]:
    """Upload a file, as the photo field of a form does, and give its ID."""
    files: dict[str, Path] = {}

    def process(hass: HomeAssistant, file_id: str) -> contextlib.AbstractContextManager[Path]:
        if file_id not in files:
            raise ValueError("Unknown file")
        return contextlib.nullcontext(files[file_id])

    def add(content: bytes) -> str:
        file_id = f"0123456789abcdef0123456789abc{len(files):03d}"
        path = tmp_path / f"{file_id}.jpg"
        path.write_bytes(content)
        files[file_id] = path
        return file_id

    with patch("custom_components.pet_recognition.config_flow.process_uploaded_file", process):
        yield add
