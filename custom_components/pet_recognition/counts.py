"""Counts: the visits of each source that ended, per day, and their announcement.

A visit counts once it ended, when no picture can join it any more, on the
local day it started. Counts are computed again from the history each time,
so a restart rebuilds them and a correction never leaves one to repair.

Each visit that ends is announced once, oldest first, to the listeners of
its source, and again each time a correction changes it. The journal
remembers the start of the latest visit announced of each source, so that a
visit that ended while Home Assistant was stopped is announced once when it
starts again. A source seen for the first time
announces none of the visits it already had.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.util import dt as dt_util

from .core.matching import OTHER_ANIMAL
from .core.visits import DayCount, Visit, count
from .history import History
from .journal import Journal

# Seconds after the end of a visit before it is checked, so that it has ended.
_MARGIN = 1

# What is announced of a visit.
VISIT_ENDED = "visit_ended"
VISIT_CORRECTED = "visit_corrected"


def visit_data(visit: Visit, titles: dict[str, str], meals: bool) -> dict[str, Any]:
    """What is said of a visit: its pets by name, whether it is unsure, its
    times, and at a source whose pictures tell the meals (`meals`), whether
    it is a meal."""
    return {
        "visit_id": visit.id,
        "pets": sorted(titles[pet] for pet in visit.pets if pet in titles),
        "other_animal": OTHER_ANIMAL in visit.pets,
        "unsure": visit.unsure,
        "meal": visit.meal if meals else None,
        "start": _time(visit.start),
        "end": _time(visit.end),
        "pictures": len(visit.pictures),
    }


def _time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class VisitCounter:
    """The visits that ended at the sources of one entry."""

    def __init__(self, hass: HomeAssistant, journal: Journal, history: History) -> None:
        self._hass = hass
        self._journal = journal
        self._history = history
        self._listeners: list[Callable[[], None]] = []
        self._announcers: dict[str, list[Callable[[str, Visit], None]]] = {}
        self._cancel_check: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Announce the visits that ended, and follow the journal and the days.

        A source seen for the first time starts after its visits that ended.
        """
        now = dt_util.utcnow().timestamp()
        for source in self._history.sources:
            if source not in self._journal.announced:
                ended = self._history.ended(source, now)
                self._journal.async_announce(source, ended[-1].start if ended else 0)
        stop_listening = self._journal.async_add_listener(self._async_check)
        stop_days = async_track_time_change(
            self._hass, self._async_new_day, hour=0, minute=0, second=0
        )
        self._async_check()

        @callback
        def _async_stop() -> None:
            stop_listening()
            stop_days()
            self._cancel()

        return _async_stop

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `listener` when a count may have changed."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_add_announcer(
        self, source: str, announce: Callable[[str, Visit], None]
    ) -> CALLBACK_TYPE:
        """Call `announce` with each visit of `source` that ends, or is corrected
        once it ended, and which of the two."""
        announcers = self._announcers.setdefault(source, [])
        announcers.append(announce)
        return lambda: announcers.remove(announce)

    def counts_meals(self, source: str) -> bool:
        """Whether the pets of a source count per meal."""
        return self._history.counts_meals(source)

    def day(self, source: str, day: date) -> DayCount:
        """The visits of a source that ended, of those that started on `day`."""
        ended = self._history.ended(source, dt_util.utcnow().timestamp())
        return count(
            ended, day, dt_util.get_default_time_zone(), self._history.counts_meals(source)
        )

    def today(self, source: str) -> DayCount:
        """The visits of a source that ended, of those that started today."""
        return self.day(source, dt_util.now().date())

    def yesterday(self, source: str) -> DayCount:
        """The visits of a source that ended, of those that started yesterday."""
        return self.day(source, dt_util.now().date() - timedelta(days=1))

    @callback
    def _async_check(self, _now: datetime | None = None) -> None:
        """Announce the visits that ended, then wait for the next one to end."""
        self._cancel()
        now = dt_util.utcnow().timestamp()
        for source in self._history.sources:
            last = self._journal.announced.get(source, 0)
            for visit in self._history.ended(source, now):
                if visit.start <= last:
                    continue
                self._journal.async_announce(source, visit.start)
                self._announce(VISIT_ENDED, visit)
        self._notify()
        ends = [
            end
            for source in self._history.sources
            if (end := self._history.next_end(source, now)) is not None
        ]
        if ends:
            self._cancel_check = async_call_later(
                self._hass, min(ends) - now + _MARGIN, self._async_check
            )

    @callback
    def async_corrected(self, source: str, pictures: Collection[str]) -> None:
        """Announce the visits of `source` announced already that hold `pictures`,
        once corrected. The others are announced when they end."""
        last = self._journal.announced.get(source, 0)
        for visit in self._history.visits(source):
            if visit.start <= last and any(picture.id in pictures for picture in visit.pictures):
                self._announce(VISIT_CORRECTED, visit)

    @callback
    def _announce(self, kind: str, visit: Visit) -> None:
        for announce in list(self._announcers.get(visit.source, ())):
            announce(kind, visit)

    @callback
    def _async_new_day(self, _now: datetime) -> None:
        self._notify()

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def _cancel(self) -> None:
        if self._cancel_check is not None:
            self._cancel_check()
            self._cancel_check = None
