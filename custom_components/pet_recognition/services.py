"""Actions: assign a visit to the pets that were there, for administrators.

A visit is given by its ID, as the visit events and the visits to review
give it, or by an entity of its source for the latest visit there. The
pets are their devices; none, and not the other animal, says it is
unknown. Every picture of the visit is corrected, which teaches its animal
when a single pet is named. This is how a visit of several pets, or an old
one, is corrected.

The dashboards correct visits with the `pet_recognition/assign` command
instead (see `websocket`), which any user who controls the source may use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN
from .core.corrections import correct
from .core.matching import OTHER_ANIMAL
from .core.visits import Run, Visit
from .counts import visit_data
from .examples import Origin
from .pets import pets
from .sources import sources

if TYPE_CHECKING:
    from . import PetRecognitionConfigEntry

SERVICE_ASSIGN_VISIT = "assign_visit"
ATTR_VISIT_ID = "visit_id"
ATTR_SOURCE = "source"
ATTR_PETS = "pets"
ATTR_OTHER_ANIMAL = "other_animal"

ASSIGN_VISIT_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_VISIT_ID): cv.string,
            vol.Optional(ATTR_SOURCE): cv.entity_id,
            vol.Optional(ATTR_PETS, default=[]): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_OTHER_ANIMAL, default=False): cv.boolean,
        }
    ),
    cv.has_at_least_one_key(ATTR_VISIT_ID, ATTR_SOURCE),
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions of the integration."""
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_ASSIGN_VISIT,
        _async_assign_visit,
        schema=ASSIGN_VISIT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def _async_assign_visit(call: ServiceCall) -> ServiceResponse:
    """Correct a visit to the pets given, and answer with the visit as it is now."""
    hass = call.hass
    entry, source, visit = find_visit(
        hass, call.data.get(ATTR_VISIT_ID), call.data.get(ATTR_SOURCE)
    )
    chosen = [_pet(hass, entry, device_id) for device_id in call.data[ATTR_PETS]]
    if call.data[ATTR_OTHER_ANIMAL]:
        chosen.append(OTHER_ANIMAL)
    now = await async_assign(entry, source, visit, chosen)
    if not call.return_response:
        return None
    titles = {key: subentry.title for key, subentry in pets(entry).items()}
    return {"visit": visit_data(now, titles, entry.runtime_data.history.counts_meals(source))}


async def async_assign(
    entry: PetRecognitionConfigEntry, source: str, visit: Visit, chosen: list[str]
) -> Visit:
    """Correct every picture of `visit` to the pets `chosen`, and return the visit now."""
    data = entry.runtime_data
    correction = correct(Run(list(visit.pictures), visit.pets), chosen)
    await data.learner.async_correct(correction, Origin.CORRECTION)
    return next(item for item in data.history.visits(source) if item.id == visit.id)


def loaded_entries(hass: HomeAssistant) -> list[PetRecognitionConfigEntry]:
    """The loaded entries of the integration."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


def find_visit(
    hass: HomeAssistant, visit_id: str | None, entity_id: str | None
) -> tuple[PetRecognitionConfigEntry, str, Visit]:
    """The entry, the source and the visit an action is about."""
    if entity_id is not None:
        registered = er.async_get(hass).async_get(entity_id)
        source = None if registered is None else registered.config_subentry_id
        entry = next(
            (
                entry
                for entry in loaded_entries(hass)
                if registered is not None and registered.config_entry_id == entry.entry_id
            ),
            None,
        )
        if entry is None or source is None or source not in sources(entry):
            raise invalid("unknown_source", entity_id=entity_id)
        places = [(entry, source)]
    else:
        places = [(entry, source) for entry in loaded_entries(hass) for source in sources(entry)]
    for entry, source in places:
        visits = entry.runtime_data.history.visits(source)
        if visit_id is None:
            if not visits:
                raise invalid("no_visit")
            return entry, source, visits[-1]
        for visit in visits:
            if visit.id == visit_id:
                return entry, source, visit
    raise invalid("unknown_visit", visit_id=str(visit_id))


def _pet(hass: HomeAssistant, entry: PetRecognitionConfigEntry, device_id: str) -> str:
    """The key of the pet of a device of `entry`."""
    device = dr.async_get(hass).async_get(device_id)
    for key, subentry in pets(entry).items():
        if device is not None and (DOMAIN, f"{entry.entry_id}_{subentry.subentry_id}") in (
            device.identifiers
        ):
            return key
    raise invalid("unknown_pet", device_id=device_id)


def invalid(error: str, **placeholders: str) -> ServiceValidationError:
    """An error of the input of an action or a command, with its translation."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=error,
        translation_placeholders=placeholders,
    )
