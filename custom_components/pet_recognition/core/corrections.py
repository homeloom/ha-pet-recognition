"""Correct who was at a source, and learn from it.

A correction relabels the pictures of a run with the pets the user named,
or with none to say it is unknown. The pictures corrected together form a
group, which stays a run of its own. When a single pet is named, one animal
becomes a new example of that pet, so the next visits are recognised: the
animal of the picture shown to the user when it is alone in it, otherwise
another picture of the run with a single animal.

An answer to a question only covers the pictures of that question: a
picture recognised on its own since the question was asked, or corrected
by hand to a pet since, keeps its pet, so a late tap never undoes a hand
correction. The picture the user saw always follows the answer. The
unknown pictures that joined the run since, that no other question was
about and that were not corrected, follow the answer too.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .doubts import Doubt, State, settled
from .visits import Picture, Run, Visit


@dataclass(frozen=True)
class Correction:
    """What a correction changes.

    `pictures`: the pictures to relabel, in order. `pets`: their new pets,
    sorted, or none for unknown. `group`: the group they form. `example`:
    the picture whose animal becomes an example of the single pet named, if
    any. The caller also forgets any example learned before from these
    pictures, since their label changed.
    """

    pictures: tuple[str, ...]
    pets: tuple[str, ...]
    group: str
    example: str | None


def run_of(visits: Iterable[Visit], picture_id: str) -> Run | None:
    """The run that holds the picture `picture_id`, if it is still kept."""
    for visit in visits:
        for run in visit.runs:
            if any(picture.id == picture_id for picture in run.pictures):
                return run
    return None


def latest_run(visits: Iterable[Visit]) -> Run | None:
    """The run with the most recent picture."""
    runs = [run for visit in visits for run in visit.runs]
    return max(runs, key=lambda run: (run.end, run.id)) if runs else None


def correct(run: Run, pets: Sequence[str]) -> Correction:
    """The correction of every picture of `run` to `pets`."""
    named = tuple(sorted(set(pets)))
    return Correction(
        tuple(picture.id for picture in run.pictures),
        named,
        run.id,
        _example(named, run.shown, run.pictures),
    )


def answer(
    doubt: Doubt, pets: Sequence[str], visits: Iterable[Visit], doubts: Iterable[Doubt]
) -> Correction | None:
    """The correction that answers `doubt` with `pets`.

    `visits` are those of the doubt's source, and `doubts` every question
    asked there. Returns None when the question is no longer open, when its
    pictures were told since, even if `review` has not settled it yet, or
    when none of its pictures is left to correct. The example comes from the
    pictures of the question, the one shown in the notification first.
    """
    if doubt.state is not State.OPEN:
        return None
    runs = {
        picture.id: (picture, run)
        for visit in visits
        for run in visit.runs
        for picture in run.pictures
    }
    kept = [runs[picture] for picture in doubt.pictures if picture in runs]
    if kept and settled(doubt, kept):
        return None
    # A picture recognised on its own or corrected to a pet since the
    # question keeps its pet, except the one the user saw.
    mine = [
        picture
        for picture, _ in sorted(runs.values(), key=lambda item: _order(item[0]))
        if picture.id in doubt.pictures and (picture.id == doubt.shown or not picture.pets)
    ]
    if not mine:
        return None
    shown = next((picture for picture in mine if picture.id == doubt.shown), None)
    if shown is None:
        shown = ([picture for picture in mine if picture.visible] or mine)[-1]
    asked = {picture: other.id for other in doubts for picture in other.pictures}
    chosen = {picture.id: picture for picture in mine}
    for picture in mine:
        run = runs[picture.id][1]
        if not run.unknown:
            continue
        for other in run.pictures:
            if not other.corrected and asked.get(other.id, doubt.id) == doubt.id:
                chosen.setdefault(other.id, other)
    pictures = sorted(chosen.values(), key=_order)
    named = tuple(sorted(set(pets)))
    return Correction(
        tuple(picture.id for picture in pictures),
        named,
        pictures[0].id,
        _example(named, shown, mine),
    )


def apply(correction: Correction, pictures: Iterable[Picture]) -> list[Picture]:
    """Relabel the pictures of `correction`, and return them."""
    changed = []
    for picture in pictures:
        if picture.id in correction.pictures:
            picture.pets = correction.pets
            picture.corrected = True
            picture.group = correction.group
            changed.append(picture)
    return changed


def _order(picture: Picture) -> tuple[float, str]:
    return (picture.taken, picture.id)


def _example(pets: tuple[str, ...], shown: Picture, candidates: list[Picture]) -> str | None:
    """The picture whose single animal becomes an example: the one shown if it
    has a single animal, else the first candidate that has one."""
    if len(pets) != 1:
        return None
    for picture in sorted(candidates, key=lambda picture: picture is not shown):
        if picture.detections == 1:
            return picture.id
    return None
