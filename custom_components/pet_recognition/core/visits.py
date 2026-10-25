"""Group the pictures of a source into visits, and each visit into runs.

A visit is a series of pictures of one source, each taken at most `gap`
seconds after the previous one: a meal at a feeder, a trip to the litter
box. Inside a visit, a run is a series of consecutive pictures of the same
pets. Two pets taking turns at a feeder make several runs, but each pet
counts once per visit.

Not every picture can be decided on its own. An animal that is seen but not
recognised takes the pets of a neighbouring picture when it resembles one
of them. A picture without any animal in sight, such as a head in the bowl,
follows the nearest picture where something is seen. A picture corrected
to unknown is a screen: nothing passes through it.

Counts are always computed again from the pictures, so a correction never
leaves a counter to repair.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo

UNKNOWN: tuple[str, ...] = ()


@dataclass
class Picture:
    """What was found in one picture of a source.

    `pets` holds the decided pets, sorted, or nothing when unknown.
    `resembles` is the best candidate of a picture with exactly one animal,
    decided or not. Pictures corrected together share a `group`, so that
    they stay a run of their own.
    """

    id: str
    source: str
    taken: float
    detections: int = 0
    pets: tuple[str, ...] = UNKNOWN
    resembles: str | None = None
    corrected: bool = False
    group: str = ""

    def __post_init__(self) -> None:
        self.pets = tuple(sorted(set(self.pets)))

    @property
    def visible(self) -> bool:
        """At least one animal can be seen in the picture."""
        return self.detections > 0

    @property
    def known(self) -> bool:
        """The picture was decided on its own, or corrected."""
        return bool(self.pets) or self.corrected


@dataclass(frozen=True)
class VisitSettings:
    """How the pictures of a source form visits.

    `gap`: the longest wait between two pictures of the same visit.
    `late`: how long pictures may arrive after they were taken, which delays
    the end of a visit. `every_picture_shows_a_pet`: the source only takes
    pictures of a pet (a feeder's meal pictures), so a visit where no animal
    is found still counts, as unknown.
    """

    gap: float = 300
    late: float = 120
    every_picture_shows_a_pet: bool = False

    def __post_init__(self) -> None:
        if self.gap <= 0 or self.late < 0:
            raise ValueError("the gap must be positive and the delay not negative")


# The run a picture belongs to: its pets, whether it was corrected to
# unknown, and its correction group.
_Key = tuple[tuple[str, ...], bool, str]
_UNKNOWN_KEY: _Key = (UNKNOWN, False, "")


@dataclass
class Run:
    """Consecutive pictures of a visit that show the same pets."""

    pictures: list[Picture]
    pets: tuple[str, ...]

    @property
    def id(self) -> str:
        """The id of the first picture."""
        return self.pictures[0].id

    @property
    def start(self) -> float:
        """When the first picture was taken."""
        return self.pictures[0].taken

    @property
    def end(self) -> float:
        """When the last picture was taken."""
        return self.pictures[-1].taken

    @property
    def unknown(self) -> bool:
        """No pet could be told."""
        return not self.pets

    @property
    def corrected(self) -> bool:
        """At least one picture of the run was corrected."""
        return any(picture.corrected for picture in self.pictures)

    @property
    def shown(self) -> Picture:
        """The picture that stands for the run: the last one where an animal is seen.

        It is the picture shown when asking who it was, and the first choice
        for a new example when the answer comes.
        """
        visible = [picture for picture in self.pictures if picture.visible]
        return (visible or self.pictures)[-1]


@dataclass
class Visit:
    """Pictures of one source, each taken soon after the previous one."""

    pictures: list[Picture]
    runs: list[Run] = field(default_factory=list)

    @property
    def id(self) -> str:
        """The id of the first picture."""
        return self.pictures[0].id

    @property
    def source(self) -> str:
        """The source the pictures come from."""
        return self.pictures[0].source

    @property
    def start(self) -> float:
        """When the first picture was taken."""
        return self.pictures[0].taken

    @property
    def end(self) -> float:
        """When the last picture was taken."""
        return self.pictures[-1].taken

    @property
    def pets(self) -> tuple[str, ...]:
        """Every pet of the visit, sorted."""
        return tuple(sorted({pet for run in self.runs for pet in run.pets}))

    @property
    def unsure(self) -> bool:
        """A run of the visit is still unknown and was not corrected."""
        return any(run.unknown and not run.corrected for run in self.runs)

    def ended(self, now: float, settings: VisitSettings) -> bool:
        """No picture can join the visit any more."""
        return now - self.end > settings.gap + settings.late

    def day(self, zone: tzinfo) -> date:
        """The local day the visit counts for: the day it started."""
        return datetime.fromtimestamp(self.start, zone).date()


def group(pictures: Iterable[Picture], settings: VisitSettings) -> list[Visit]:
    """The visits of one source's pictures, oldest first.

    A visit where no animal is seen and nothing was decided or corrected is
    left out, unless the source only takes pictures of a pet.
    """
    ordered = sorted(pictures, key=lambda picture: (picture.taken, picture.id))
    if len({picture.source for picture in ordered}) > 1:
        raise ValueError("visits are grouped one source at a time")
    visits: list[Visit] = []
    for picture in ordered:
        if visits and picture.taken - visits[-1].end <= settings.gap:
            visits[-1].pictures.append(picture)
        else:
            visits.append(Visit([picture]))
    kept = []
    for visit in visits:
        if settings.every_picture_shows_a_pet or any(
            picture.visible or picture.known for picture in visit.pictures
        ):
            visit.runs = _runs(visit.pictures)
            kept.append(visit)
    return kept


def _runs(pictures: list[Picture]) -> list[Run]:
    runs: list[Run] = []
    previous: _Key | None = None
    for picture, key in zip(pictures, _keys(pictures), strict=True):
        if key != previous:
            runs.append(Run([], key[0]))
            previous = key
        runs[-1].pictures.append(picture)
    return runs


def _keys(pictures: list[Picture]) -> list[_Key]:
    """The run of each picture of a visit.

    An animal seen but not recognised takes the run of the nearest known
    picture before it, or else after it, when it resembles one of that
    run's pets; otherwise it stays unknown. A picture where no animal is seen
    follows the nearest picture that is known or shows an animal, before it
    first, even an unknown one. A picture corrected to unknown keeps its own
    run, so nothing is inherited through it.
    """
    own: list[_Key] = [
        (
            picture.pets,
            picture.corrected and not picture.pets,
            picture.group if picture.corrected else "",
        )
        for picture in pictures
    ]
    keys = list(own)
    known = [index for index, picture in enumerate(pictures) if picture.known]
    for index, picture in enumerate(pictures):
        if picture.known or not picture.visible:
            continue
        neighbours = [
            neighbour
            for neighbour in (
                max((j for j in known if j < index), default=None),
                min((j for j in known if j > index), default=None),
            )
            if neighbour is not None
        ]
        keys[index] = next(
            (own[j] for j in neighbours if picture.resembles in own[j][0]), _UNKNOWN_KEY
        )
    landmarks = [
        index for index, picture in enumerate(pictures) if picture.known or picture.visible
    ]
    for index, picture in enumerate(pictures):
        if picture.known or picture.visible:
            continue
        before = max((j for j in landmarks if j < index), default=None)
        nearest = (
            before if before is not None else min((j for j in landmarks if j > index), default=None)
        )
        keys[index] = _UNKNOWN_KEY if nearest is None else keys[nearest]
    return keys


@dataclass
class DayCount:
    """The visits of one day: all of them, and per pet."""

    visits: int = 0
    pets: dict[str, int] = field(default_factory=dict)


def count(visits: Iterable[Visit], day: date, zone: tzinfo) -> DayCount:
    """How many visits started on `day`, in the time zone `zone`.

    A pet counts once per visit, even when it comes back to the bowl after
    another pet in the same visit. The other animal counts like a pet, so
    its visits can be shown too.
    """
    counted = DayCount()
    for visit in visits:
        if visit.day(zone) != day:
            continue
        counted.visits += 1
        for pet in visit.pets:
            counted.pets[pet] = counted.pets.get(pet, 0) + 1
    return counted
