"""Decide when to ask who was at a source, and when the question is settled.

A run that stays unknown once its visit is over becomes a question: one
notification, with the picture of the run and a button per pet. A run is
asked about only once, whatever happens to it later, and never long after
its visit, for example when Home Assistant starts again after a while.

While a pet expected at the source has no example yet, every run is
unknown. The questions then teach the first examples, so only one is open
at a time per source, and it expires if nobody answers.

A question is settled when the pictures it was about are told: at least one
was decided on its own or corrected, and each of the others was too, or
belongs to a run with pets without an animal in sight, or belongs to a run
that a picture of the same question told. An animal that only resembles a
neighbour outside the question does not settle it, because that label can
change with the next example.

Questions are planned and reviewed one source at a time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .visits import Picture, Run, Visit, VisitSettings


class Kind(StrEnum):
    """Why a question is asked."""

    UNSURE = "unsure"
    LEARNING = "learning"


class State(StrEnum):
    """Where a question stands."""

    OPEN = "open"
    SETTLED = "settled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class QuestionSettings:
    """When to ask, and for how long.

    `recent`: the longest time after the end of a visit for a question to be
    asked. `learning_expiry`: how long a learning question stays open.
    """

    recent: float = 2 * 3600
    learning_expiry: float = 2 * 3600

    def __post_init__(self) -> None:
        if self.recent <= 0 or self.learning_expiry <= 0:
            raise ValueError("durations must be positive")


@dataclass(frozen=True)
class Question:
    """A run to ask about, with the visit it belongs to."""

    run: Run
    visit: Visit
    kind: Kind


@dataclass
class Doubt:
    """A question that was asked, and the pictures it was about."""

    id: str
    source: str
    pictures: tuple[str, ...]
    shown: str
    kind: Kind
    asked: float
    state: State = State.OPEN

    @classmethod
    def ask(cls, question: Question, doubt_id: str, now: float) -> Doubt:
        """The doubt for `question`, asked now under `doubt_id`."""
        return cls(
            doubt_id,
            question.visit.source,
            tuple(picture.id for picture in question.run.pictures),
            question.run.shown.id,
            question.kind,
            now,
        )


def questions(
    source: str,
    visits: Iterable[Visit],
    doubts: Iterable[Doubt],
    now: float,
    visit_settings: VisitSettings,
    settings: QuestionSettings,
    *,
    learned: bool,
    ask_when_unsure: bool = True,
    ask_while_learning: bool = True,
) -> list[Question]:
    """The runs of `source` to ask about now, oldest first.

    `learned`: every pet expected at the source has an example. The oldest
    runs come first because they are the next to leave the time when they
    can still be asked about. A learning question left unanswered past its
    expiry no longer holds the next one back, even before `review` expires
    it, so two can be open for a short while.
    """
    visits = sorted(visits, key=lambda visit: visit.start)
    if any(visit.source != source for visit in visits):
        raise ValueError("questions are planned one source at a time")
    doubts = [doubt for doubt in doubts if doubt.source == source]
    asked = {picture for doubt in doubts for picture in doubt.pictures}
    learning_open = any(
        doubt.kind is Kind.LEARNING
        and doubt.state is State.OPEN
        and not _too_old(doubt, now, settings)
        for doubt in doubts
    )
    if (learned and not ask_when_unsure) or (not learned and not ask_while_learning):
        return []
    found: list[Question] = []
    for visit in visits:
        if not visit.ended(now, visit_settings) or now - visit.end > settings.recent:
            continue
        for run in visit.runs:
            if not run.unknown or run.corrected:
                continue
            if any(picture.id in asked for picture in run.pictures):
                continue
            if learned:
                found.append(Question(run, visit, Kind.UNSURE))
            elif not learning_open:
                found.append(Question(run, visit, Kind.LEARNING))
                learning_open = True
    return found


def review(
    source: str,
    doubts: Iterable[Doubt],
    visits: Iterable[Visit],
    now: float,
    settings: QuestionSettings,
) -> list[Doubt]:
    """Settle or expire the open doubts of `source` that need it, and return them.

    The caller then clears their notifications. A doubt expires when none of
    its pictures is kept any more, and a learning doubt when nobody answered
    in time.
    """
    pictures: dict[str, tuple[Picture, Run]] = {}
    for visit in visits:
        if visit.source != source:
            raise ValueError("questions are reviewed one source at a time")
        for run in visit.runs:
            for picture in run.pictures:
                pictures[picture.id] = (picture, run)
    changed = []
    for doubt in doubts:
        if doubt.source != source or doubt.state is not State.OPEN:
            continue
        kept = [pictures[picture] for picture in doubt.pictures if picture in pictures]
        if kept and settled(doubt, kept):
            doubt.state = State.SETTLED
        elif not kept or _too_old(doubt, now, settings):
            doubt.state = State.EXPIRED
        else:
            continue
        changed.append(doubt)
    return changed


def settled(doubt: Doubt, pictures: Iterable[tuple[Picture, Run]]) -> bool:
    """The pictures of `doubt`, with their runs, are all told."""
    pictures = list(pictures)
    return any(picture.known for picture, _ in pictures) and all(
        picture.known or _told_by_the_doubt(picture, run, doubt) for picture, run in pictures
    )


def _too_old(doubt: Doubt, now: float, settings: QuestionSettings) -> bool:
    """A learning doubt nobody answered in time."""
    return doubt.kind is Kind.LEARNING and now - doubt.asked > settings.learning_expiry


def _told_by_the_doubt(picture: Picture, run: Run, doubt: Doubt) -> bool:
    """`picture` has pets through its run: without an animal in sight, or
    with a picture of the doubt telling the run."""
    if run.unknown:
        return False
    return not picture.visible or any(
        other.known and other.id in doubt.pictures for other in run.pictures
    )
