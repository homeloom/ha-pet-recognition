"""Diagnostics of the integration, without the engine's address or key."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import PetRecognitionConfigEntry
from .const import CONF_KIND, CONF_SPECIES, DOMAIN
from .core.matching import Example
from .options import entry_options
from .pets import pets
from .sources import source_settings, sources


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
    store = entry.runtime_data.examples
    journal = entry.runtime_data.journal
    counter = entry.runtime_data.counter
    options = entry_options(entry)
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
        "options": {
            "threshold": options.match.threshold,
            "margin": options.match.margin,
            "min_area": options.min_area,
            "detection_limit": options.detection_limit,
            "retention_days": options.retention_days,
            # Counted instead of named.
            "phones": len(options.phones),
            "ask_while_learning": options.ask_while_learning,
        },
        "connected": coordinator.last_update_success,
        "last_error": None if error is None else type(error.__cause__ or error).__name__,
        "failing_since": None
        if coordinator.failing_since is None
        else coordinator.failing_since.isoformat(),
        # Numbered instead of named.
        "pets": [
            {
                "pet": f"pet_{number}",
                "species": subentry.data[CONF_SPECIES],
                "examples": _count(store.gallery.of(key)),
                "learned": sum(not example.uploaded for example in store.gallery.of(key)),
            }
            for number, (key, subentry) in enumerate(pets(entry).items(), start=1)
        ],
        "sources": [
            {
                "source": f"source_{number}",
                "kind": subentry.data[CONF_KIND],
                "images": len(settings.images),
                "cameras": len(settings.cameras),
                # A trigger kept without cameras is not watched.
                "trigger": bool(settings.cameras) and settings.trigger is not None,
                "pictures": dict(
                    sorted(Counter(record.status.value for record in journal.of(source_id)).items())
                ),
                "pending": len(journal.pending(source_id)),
                "visits_today": counter.today(source_id).visits,
                "meal_pictures": len(settings.visits.meal_pictures),
                "ask_when_unsure": settings.ask_when_unsure,
                "questions": dict(
                    sorted(
                        Counter(doubt.state.value for doubt in journal.doubts(source_id)).items()
                    )
                ),
            }
            for number, (source_id, subentry) in enumerate(sources(entry).items(), start=1)
            for settings in (source_settings(entry, subentry),)
        ],
        "pending": len(journal.queue),
    }


def _count(examples: list[Example]) -> dict[str, int]:
    """The number of examples of each model."""
    return dict(sorted(Counter(example.model for example in examples).items()))
