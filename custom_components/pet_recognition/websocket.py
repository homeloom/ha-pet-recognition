"""Websocket commands: what a dashboard shows of the pets, and its corrections.

`pet_recognition/overview` gives, to any user, the pets, the sources and the
visits of a period: yesterday and today by default, never before the days
the pictures are kept. Each visit has a link to its best crop, signed for
that user for half an hour. Pets and sources are given by their subentry
IDs, which the visits and `pet_recognition/assign` use too.

`pet_recognition/assign` corrects a visit as the `assign_visit` action does,
for any user who may control the source's **Correct last visit**, as any
phone of the house may already answer a question. The action stays for
administrators.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import CONF_KIND, CONF_SPECIES, DOMAIN
from .core.matching import OTHER_ANIMAL
from .core.visits import Run, Visit
from .options import entry_options
from .pets import pets
from .services import async_assign, find_visit, invalid, loaded_entries
from .sources import source_settings, sources
from .views import picture_paths, signed_url

if TYPE_CHECKING:
    from . import PetRecognitionConfigEntry

OVERVIEW = "pet_recognition/overview"
ASSIGN = "pet_recognition/assign"


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    """Register the commands of the integration."""
    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_assign)


@websocket_api.websocket_command(
    {
        vol.Required("type"): OVERVIEW,
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
    }
)
@callback
def ws_overview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The pets, the sources and the visits of a period, of every loaded entry."""
    now = dt_util.utcnow()
    yesterday = dt_util.start_of_local_day(dt_util.now().date() - timedelta(days=1))
    start = dt_util.as_utc(msg.get("start", yesterday))
    end = dt_util.as_utc(msg.get("end", now))
    result: dict[str, list[dict[str, Any]]] = {"pets": [], "sources": [], "visits": []}
    for entry in loaded_entries(hass):
        kept = now - timedelta(days=entry_options(entry).retention_days)
        devices = _devices(hass, entry)
        ids = {key: subentry.subentry_id for key, subentry in pets(entry).items()}
        result["pets"] += [
            {
                "id": subentry.subentry_id,
                "name": subentry.title,
                "species": subentry.data[CONF_SPECIES],
                "device_id": devices.get(subentry.subentry_id),
            }
            for subentry in pets(entry).values()
        ]
        registry = er.async_get(hass)
        for source_id, subentry in sources(entry).items():
            result["sources"].append(
                {
                    "id": source_id,
                    "name": subentry.title,
                    "kind": subentry.data[CONF_KIND],
                    "pets": [ids[key] for key in source_settings(entry, subentry).pets],
                    "counts_meals": entry.runtime_data.history.counts_meals(source_id),
                    "device_id": devices.get(source_id),
                    "visit_entity": registry.async_get_entity_id(
                        "event", DOMAIN, f"{entry.entry_id}_{source_id}_visit"
                    ),
                }
            )
            ended, paths = _ended(entry, source_id), _paths(entry, source_id)
            result["visits"] += [
                _visit(hass, connection, entry, visit, visit.id in ended, paths)
                for visit in entry.runtime_data.history.visits(source_id)
                if _in(visit, max(start, kept), end)
            ]
    result["visits"].sort(key=lambda visit: (visit["start"], visit["id"]))
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): ASSIGN,
        vol.Required("visit_id"): str,
        vol.Optional("pets", default=[]): [str],
        vol.Optional("other_animal", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_assign(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Correct a visit to the pets given, and answer with the visit as it is now."""
    entry, source, visit = find_visit(hass, msg["visit_id"], None)
    control = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_{source}_correct_last_visit"
    )
    user = connection.user
    if not user.is_admin and (
        control is None or not user.permissions.check_entity(control, POLICY_CONTROL)
    ):
        raise Unauthorized(context=connection.context(msg), entity_id=control)
    keys = {subentry.subentry_id: key for key, subentry in pets(entry).items()}
    chosen = []
    for pet_id in msg["pets"]:
        if pet_id not in keys:
            raise invalid("unknown_pet_id", pet_id=pet_id)
        chosen.append(keys[pet_id])
    if msg["other_animal"]:
        chosen.append(OTHER_ANIMAL)
    now = await async_assign(entry, source, visit, chosen)
    ended = now.id in _ended(entry, source)
    shown = _visit(hass, connection, entry, now, ended, _paths(entry, source))
    connection.send_result(msg["id"], {"visit": shown})


def _in(visit: Visit, start: datetime, end: datetime) -> bool:
    """Whether a visit goes on at some time from `start` to `end`."""
    return visit.end >= start.timestamp() and visit.start <= end.timestamp()


def _devices(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> dict[str, str]:
    """The device of each pet and source of `entry`, by subentry ID."""
    found = {}
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        for domain, identifier in device.identifiers:
            prefix = f"{entry.entry_id}_"
            if domain == DOMAIN and identifier.startswith(prefix):
                found[identifier.removeprefix(prefix)] = device.id
    return found


def _ended(entry: PetRecognitionConfigEntry, source: str) -> set[str]:
    """The IDs of the visits of a source that no picture can join any more."""
    now = dt_util.utcnow().timestamp()
    return {visit.id for visit in entry.runtime_data.history.ended(source, now)}


def _visit(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    entry: PetRecognitionConfigEntry,
    visit: Visit,
    ended: bool,
    paths: dict[str, str],
) -> dict[str, Any]:
    """A visit as the commands give it, with what shows the picture that
    stands for it, as a question shows it."""
    ids = {key: subentry.subentry_id for key, subentry in pets(entry).items()}
    path = paths.get(Run(list(visit.pictures), visit.pets).shown.id)
    return {
        "id": visit.id,
        "source": visit.source,
        "start": dt_util.utc_from_timestamp(visit.start).isoformat(),
        "end": dt_util.utc_from_timestamp(visit.end).isoformat(),
        "ended": ended,
        "pets": [ids[pet] for pet in visit.pets if pet in ids],
        "other_animal": OTHER_ANIMAL in visit.pets,
        "unsure": visit.unsure,
        "meal": visit.meal if entry.runtime_data.history.counts_meals(visit.source) else None,
        "picture": None if path is None else signed_url(hass, path, connection.refresh_token_id),
    }


def _paths(entry: PetRecognitionConfigEntry, source: str) -> dict[str, str]:
    """What shows each picture of a source, by picture ID."""
    return picture_paths(entry.entry_id, entry.runtime_data.journal, source)
