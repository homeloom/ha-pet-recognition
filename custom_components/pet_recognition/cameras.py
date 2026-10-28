"""Cameras: take a burst of pictures when the trigger of their source fires.

A camera shows a live view, not pictures, so it is asked for pictures only
when something may be there: when the source's trigger fires. A trigger is
a binary sensor turning on, such as a motion sensor, or an event entity
receiving an event. Each camera of the source then takes a few pictures, a
few seconds apart, and each is queued for the engine like a new picture of
an image entity. The burst is not saved: one cut short by a restart or a
reload is simply not finished.

A trigger that fires again during a burst adds nothing to it. A picture the
source already has, such as the same frame of a camera that did not move,
is not queued again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.camera import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .files import new_id
from .journal import Journal, Pending
from .sources import SourceSettings, source_settings, sources

_LOGGER = logging.getLogger(__name__)


async def wait(seconds: float) -> None:
    """Wait between two pictures of a burst."""
    await asyncio.sleep(seconds)


def fired(old: State | None, new: State | None) -> bool:
    """Whether a change of the trigger's state fires it.

    A binary sensor fires when it turns on from off, or from unknown, as a
    motion sensor that has sent nothing since Home Assistant started does,
    but not when it comes back on after being unavailable. An event
    entity's state is the time of its last event, so a new time is a new
    event, except when the entity comes back after being unavailable, with
    the time of an event already seen.
    """
    if old is None or new is None:
        return False
    if new.domain == BINARY_SENSOR_DOMAIN:
        return old.state in (STATE_OFF, STATE_UNKNOWN) and new.state == STATE_ON
    return (
        new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        and old.state != STATE_UNAVAILABLE
        and new.state != old.state
    )


class CameraWatcher:
    """Takes a burst of pictures from the cameras of a source when its trigger fires."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        journal: Journal,
        on_new: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._journal = journal
        self._on_new = on_new
        self._settings: dict[str, SourceSettings] = {}
        # The sources each trigger fires, and the sources taking a burst.
        self._fires: dict[str, list[str]] = {}
        self._bursts: set[str] = set()
        for source_id, subentry in sources(entry).items():
            settings = source_settings(entry, subentry)
            if settings.cameras and settings.trigger:
                self._settings[source_id] = settings
                self._fires.setdefault(settings.trigger, []).append(source_id)

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Follow the triggers."""
        return async_track_state_change_event(self._hass, list(self._fires), self._async_changed)

    @callback
    def _async_changed(self, event: Event[EventStateChangedData]) -> None:
        if not fired(event.data["old_state"], event.data["new_state"]):
            return
        for source_id in self._fires[event.data["entity_id"]]:
            if source_id in self._bursts:
                continue
            self._bursts.add(source_id)
            self._entry.async_create_background_task(
                self._hass, self._async_burst(source_id), f"pet_recognition burst {source_id}"
            )

    async def _async_burst(self, source_id: str) -> None:
        settings = self._settings[source_id]
        try:
            for frame in range(settings.frames):
                if frame:
                    await wait(settings.interval)
                for entity_id in settings.cameras:
                    await self._async_take(source_id, entity_id)
        finally:
            self._bursts.discard(source_id)

    async def _async_take(self, source_id: str, entity_id: str) -> None:
        try:
            image = await async_get_image(self._hass, entity_id)
        except HomeAssistantError as err:
            # The camera turns a cancellation, such as the entry unloading,
            # into this error: the burst must stop all the same.
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise asyncio.CancelledError from err
            _LOGGER.debug("Cannot take a picture with %s: %s", entity_id, err)
            return
        sha256 = hashlib.sha256(image.content).hexdigest()
        if self._journal.known(source_id, sha256):
            return
        pending = Pending(new_id(), source_id, entity_id, dt_util.utcnow().timestamp(), sha256)
        await self._journal.async_add_pending(pending, image.content)
        self._on_new()
