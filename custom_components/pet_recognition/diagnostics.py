"""Diagnostics of the integration, without the engine's address or key."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import PetRecognitionConfigEntry
from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PetRecognitionConfigEntry
) -> dict[str, Any]:
    """What helps to understand a problem: versions, limits and the last failure.

    The engine's URL and key are left out, and so is the text of the last
    error, which may name the engine's host.
    """
    if entry.state is not ConfigEntryState.LOADED:
        # Such as after the engine refused the key: there is no coordinator.
        integration = await async_get_integration(hass, DOMAIN)
        return {
            "integration_version": str(integration.version),
            "state": entry.state.value,
            "engine": None,
        }
    coordinator = entry.runtime_data.coordinator
    info = coordinator.data
    error = coordinator.last_exception
    return {
        "integration_version": coordinator.version,
        "state": entry.state.value,
        "engine": None
        if info is None
        else {
            "version": info.version,
            "model": info.model,
            "species": sorted(info.species),
            "max_bytes": info.max_bytes,
            "max_pixels": info.max_pixels,
        },
        "connected": coordinator.last_update_success,
        "last_error": None if error is None else type(error.__cause__ or error).__name__,
        "failing_since": None
        if coordinator.failing_since is None
        else coordinator.failing_since.isoformat(),
    }
