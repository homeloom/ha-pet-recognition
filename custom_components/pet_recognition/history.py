"""History: the visits of each source, grouped from the records of the journal.

The visits are never stored. They are grouped again from the journal's
records when asked for after a change, so that a correction or a purge never
leaves them to repair.
"""

from __future__ import annotations

from collections.abc import Mapping

from homeassistant.core import CALLBACK_TYPE, callback

from .core.visits import Picture, Visit, VisitSettings, group
from .journal import Journal, Record


def picture(record: Record) -> Picture:
    """What the grouping of visits needs to know of a record."""
    return Picture(
        id=record.id,
        source=record.source,
        taken=record.taken,
        detections=len(record.detections),
        pets=record.pets,
        resembles=record.resembles,
    )


class History:
    """The visits of the sources of one entry."""

    def __init__(self, journal: Journal, settings: Mapping[str, VisitSettings]) -> None:
        self._journal = journal
        self._settings = dict(settings)
        self._visits: dict[str, list[Visit]] = {}

    @property
    def sources(self) -> list[str]:
        """The sources whose visits are grouped."""
        return list(self._settings)

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Forget the visits grouped so far each time the journal changes.

        The journal keeps whole the visits the entities show, and those that
        have not ended before its age limit.
        """
        stop_listening = self._journal.async_add_listener(self._visits.clear)
        stop_protecting = self._journal.async_protect(self.protected)

        @callback
        def _async_stop() -> None:
            stop_listening()
            stop_protecting()

        return _async_stop

    def protected(self, before: float | None) -> set[str]:
        """The records of the latest visit of each source, and of the visit
        where each pet was last seen, and with an age limit, of the visits
        that ended at or after it.
        """
        # The journal asks before it says it changed.
        self._visits.clear()
        kept: set[str] = set()
        for source in self._settings:
            visits = self.visits(source)
            for visit in visits[-1:] + [
                visit for visit in visits if before is not None and visit.end >= before
            ]:
                kept.update(picture.id for picture in visit.pictures)
        pets = {pet for record in self._journal.records for pet in record.pets}
        for record in filter(None, map(self._journal.last_of_pet, pets)):
            visit = self.visit_of(record) if record.source in self._settings else None
            kept.update([record.id] if visit is None else (item.id for item in visit.pictures))
        return kept

    def visits(self, source: str) -> list[Visit]:
        """The visits of a source, oldest first."""
        if (visits := self._visits.get(source)) is None:
            records = self._journal.of(source)
            visits = self._visits[source] = group(
                (picture(record) for record in records), self._settings[source]
            )
        return visits

    def ended(self, source: str, now: float) -> list[Visit]:
        """The visits of a source that no picture can join any more, oldest first."""
        settings = self._settings[source]
        return [visit for visit in self.visits(source) if visit.ended(now, settings)]

    def next_end(self, source: str, now: float) -> float | None:
        """When the next visit of a source going on ends, if no picture joins it."""
        settings = self._settings[source]
        return min(
            (
                visit.end + settings.gap + settings.late
                for visit in self.visits(source)
                if not visit.ended(now, settings)
            ),
            default=None,
        )

    def visit_of(self, record: Record) -> Visit | None:
        """The visit a record belongs to, if any."""
        return next(
            (
                visit
                for visit in reversed(self.visits(record.source))
                if any(item.id == record.id for item in visit.pictures)
            ),
            None,
        )

    def start_of(self, record: Record) -> float:
        """When the visit of a record started, or when it was taken if it has none."""
        visit = self.visit_of(record)
        return record.taken if visit is None else visit.start
