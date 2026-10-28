"""Fixtures of the integration's tests."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture(autouse=True)
def config_directory(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """Keep the files the integration writes in a directory of the test."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("hass").config.config_dir = str(tmp_path)
