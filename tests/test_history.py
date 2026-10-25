"""Tests of grouping the records of the journal into visits."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.pet_recognition.core.visits import VisitSettings
from custom_components.pet_recognition.files import new_id
from custom_components.pet_recognition.history import History
from custom_components.pet_recognition.journal import (
    Detection,
    Journal,
    Pending,
    Record,
    Status,
)

SOURCE = "source"


async def record(
    journal: Journal,
    taken: float,
    *,
    animals: int = 0,
    pets: tuple[str, ...] = (),
    source: str = SOURCE,
) -> Record:
    pending = Pending(new_id(), source, "image.feeder", taken, new_id() * 2)
    await journal.async_add_pending(pending, b"a picture")
    done = Record(
        pending.id,
        source,
        pending.entity,
        taken,
        pending.sha256,
        Status.ANALYZED,
        detections=[Detection("cat", 0.9, 0.2, new_id(), "model") for _ in range(animals)],
        pets=pets,
    )
    await journal.async_record(pending, done)
    return done


async def test_visits_are_grouped_again_after_each_change(hass: HomeAssistant) -> None:
    journal = Journal(hass, "entry")
    history = History(journal, {SOURCE: VisitSettings(gap=300, late=0)})
    unsubscribe = history.async_start()
    first = await record(journal, 1000, animals=1, pets=("pet",))
    assert [visit.pets for visit in history.visits(SOURCE)] == [("pet",)]
    # A later picture of the same visit, where the animal is not told.
    second = await record(journal, 1200, animals=1)
    (visit,) = history.visits(SOURCE)
    assert [picture.id for picture in visit.pictures] == [first.id, second.id]
    assert history.start_of(second) == 1000

    # Where nothing is seen, at a source that takes pictures of anything,
    # there is no visit.
    nobody = await record(journal, 5000)
    assert history.visit_of(nobody) is None
    assert history.start_of(nobody) == 5000
    assert len(history.visits(SOURCE)) == 1

    unsubscribe()
    await record(journal, 5100, animals=1, pets=("pet",))
    # Not followed any more: what was grouped stays.
    assert len(history.visits(SOURCE)) == 1


async def test_the_purge_takes_whole_visits_and_keeps_what_the_entities_show(
    hass: HomeAssistant,
) -> None:
    journal = Journal(hass, "entry")
    history = History(journal, {SOURCE: VisitSettings(gap=300, late=0)})
    history.async_start()
    ended = [await record(journal, taken, animals=1, pets=("pet_a",)) for taken in (500, 800)]
    # Pet B is last seen in a visit that has ended, with Pet A in it.
    seen = [
        await record(journal, taken, animals=1, pets=(pet,))
        for taken, pet in ((1500, "pet_a"), (1560, "pet_b"), (1620, "pet_a"))
    ]
    # A visit that ends after the age limit.
    ending = [await record(journal, taken, animals=1, pets=("pet_a",)) for taken in (2900, 3200)]
    latest = await record(journal, 9000, animals=1, pets=("pet_a",))
    # A pet last seen at a source that is gone, whose visits are not known.
    elsewhere = await record(journal, 100, animals=1, pets=("pet_c",), source="gone")
    start = history.start_of(seen[1])

    await journal.async_purge(3000)
    kept = {item.id for item in journal.records}
    assert kept == {item.id for item in (*seen, *ending, latest, elsewhere)}
    assert not kept & {item.id for item in ended}
    assert history.start_of(seen[1]) == start == 1500
