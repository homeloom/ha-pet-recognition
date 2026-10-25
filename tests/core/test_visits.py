"""Tests of the grouping of pictures into visits and runs."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.pet_recognition.core.visits import (
    UNKNOWN,
    Picture,
    Visit,
    VisitSettings,
    count,
    group,
)

ZONE = ZoneInfo("America/New_York")
# Sunday 11 October 2026, 8 am.
START = datetime(2026, 10, 11, 8, tzinfo=ZONE).timestamp()
SETTINGS = VisitSettings(gap=300, late=120)


def pic(
    picture_id: str,
    minutes: float,
    pets: tuple[str, ...] = UNKNOWN,
    *,
    detections: int = 1,
    resembles: str | None = None,
    corrected: bool = False,
    group_id: str = "",
    source: str = "feeder",
) -> Picture:
    """A picture taken `minutes` after START."""
    return Picture(
        picture_id,
        source,
        START + minutes * 60,
        detections,
        pets,
        resembles,
        corrected,
        group_id,
    )


def runs_of(
    *pictures: Picture, settings: VisitSettings = SETTINGS
) -> list[tuple[list[str], tuple]]:
    """The runs of a single visit, as picture ids and pets."""
    (visit,) = group(pictures, settings)
    return [([picture.id for picture in run.pictures], run.pets) for run in visit.runs]


def test_pictures_close_in_time_form_one_visit() -> None:
    visits = group([pic("c", 10), pic("a", 0), pic("b", 5), pic("d", 15.1)], SETTINGS)
    assert [[picture.id for picture in visit.pictures] for visit in visits] == [
        ["a", "b", "c"],
        ["d"],
    ]
    assert visits[0].id == "a"
    assert visits[0].source == "feeder"
    assert (visits[0].start, visits[0].end) == (START, START + 600)


def test_visits_are_grouped_one_source_at_a_time() -> None:
    with pytest.raises(ValueError, match="one source"):
        group([pic("a", 0), pic("b", 1, source="litter_box")], SETTINGS)


def test_a_visit_without_any_animal_is_left_out() -> None:
    assert group([pic("a", 0, detections=0), pic("b", 1, detections=0)], SETTINGS) == []


def test_a_visit_without_any_animal_counts_where_every_picture_shows_a_pet() -> None:
    settings = VisitSettings(every_picture_shows_a_pet=True)
    assert runs_of(pic("a", 0, detections=0), settings=settings) == [(["a"], UNKNOWN)]


def test_a_corrected_visit_counts_even_without_any_animal_in_sight() -> None:
    assert runs_of(pic("a", 0, ("pet_a",), detections=0, corrected=True)) == [(["a"], ("pet_a",))]


def test_pets_taking_turns_make_runs_and_count_once() -> None:
    (visit,) = group(
        [pic("a", 0, ("pet_a",)), pic("b", 1, ("pet_b",)), pic("c", 2, ("pet_a",))], SETTINGS
    )
    assert [run.pets for run in visit.runs] == [("pet_a",), ("pet_b",), ("pet_a",)]
    assert visit.pets == ("pet_a", "pet_b")
    assert count([visit], date(2026, 10, 11), ZONE).pets == {"pet_a": 1, "pet_b": 1}


@pytest.mark.parametrize(
    ("pictures", "runs"),
    [
        # An unsure animal takes the pets of the known picture before it...
        (
            [pic("a", 0, ("pet_a",)), pic("b", 1, resembles="pet_a"), pic("c", 2, ("pet_b",))],
            [(["a", "b"], ("pet_a",)), (["c"], ("pet_b",))],
        ),
        # ... or after it, when it does not resemble the one before.
        (
            [pic("a", 0, ("pet_a",)), pic("b", 1, resembles="pet_b"), pic("c", 2, ("pet_b",))],
            [(["a"], ("pet_a",)), (["b", "c"], ("pet_b",))],
        ),
        # It stays unknown when it resembles neither.
        (
            [pic("a", 0, ("pet_a",)), pic("b", 1, resembles="pet_c")],
            [(["a"], ("pet_a",)), (["b"], UNKNOWN)],
        ),
        # Only the nearest known pictures count, not one further away.
        (
            [
                pic("a", 0, ("pet_a",)),
                pic("b", 1, ("pet_b",)),
                pic("c", 2, resembles="pet_a"),
            ],
            [(["a"], ("pet_a",)), (["b"], ("pet_b",)), (["c"], UNKNOWN)],
        ),
        # Two pets corrected together: an unsure animal resembling one of them joins them.
        (
            [
                pic("a", 0, ("pet_a", "pet_b"), corrected=True, group_id="a"),
                pic("b", 1, resembles="pet_b"),
            ],
            [(["a", "b"], ("pet_a", "pet_b"))],
        ),
    ],
)
def test_an_unsure_animal_inherits_from_a_known_neighbour(
    pictures: list[Picture], runs: list[tuple[list[str], tuple]]
) -> None:
    assert runs_of(*pictures) == runs


@pytest.mark.parametrize(
    ("pictures", "runs"),
    [
        # A head in the bowl follows the picture before it...
        (
            [pic("a", 0, ("pet_a",)), pic("b", 1, detections=0), pic("c", 2, ("pet_b",))],
            [(["a", "b"], ("pet_a",)), (["c"], ("pet_b",))],
        ),
        # ... or the one after it when it comes first.
        (
            [pic("a", 0, detections=0), pic("b", 1, ("pet_b",))],
            [(["a", "b"], ("pet_b",))],
        ),
        # It follows an unsure animal too, which keeps it unknown.
        (
            [pic("a", 0, resembles="pet_c"), pic("b", 1, detections=0), pic("c", 2, ("pet_b",))],
            [(["a", "b"], UNKNOWN), (["c"], ("pet_b",))],
        ),
        # And an unsure animal that inherited a pet passes it on.
        (
            [
                pic("a", 0, ("pet_a",)),
                pic("b", 1, resembles="pet_a"),
                pic("c", 2, detections=0),
            ],
            [(["a", "b", "c"], ("pet_a",))],
        ),
    ],
)
def test_a_picture_without_an_animal_in_sight_follows_its_neighbour(
    pictures: list[Picture], runs: list[tuple[list[str], tuple]]
) -> None:
    assert runs_of(*pictures) == runs


def test_a_picture_corrected_to_unknown_is_a_screen() -> None:
    pictures = [
        pic("a", 0, ("pet_a",)),
        pic("b", 1, corrected=True, group_id="b"),
        pic("c", 2, resembles="pet_a"),
        pic("d", 3, detections=0),
    ]
    assert runs_of(*pictures) == [
        (["a"], ("pet_a",)),
        (["b"], UNKNOWN),
        (["c", "d"], UNKNOWN),
    ]
    (visit,) = group(pictures, SETTINGS)
    # The corrected run is settled; the one after it is still unsure.
    assert [(run.corrected, run.unknown) for run in visit.runs] == [
        (False, False),
        (True, True),
        (False, True),
    ]
    assert visit.unsure


def test_pictures_corrected_together_stay_a_run_of_their_own() -> None:
    pictures = [
        pic("a", 0, ("pet_a",)),
        pic("b", 1, ("pet_a",), corrected=True, group_id="b"),
        pic("c", 2, ("pet_a",), corrected=True, group_id="b"),
        pic("d", 3, ("pet_a",), corrected=True, group_id="d"),
    ]
    assert runs_of(*pictures) == [
        (["a"], ("pet_a",)),
        (["b", "c"], ("pet_a",)),
        (["d"], ("pet_a",)),
    ]
    (visit,) = group(pictures, SETTINGS)
    assert visit.pets == ("pet_a",)
    assert not visit.unsure


def test_a_run_is_shown_by_its_last_picture_with_an_animal() -> None:
    (visit,) = group(
        [
            pic("a", 0, ("pet_a",)),
            pic("b", 1, ("pet_a",)),
            pic("c", 2, detections=0),
        ],
        SETTINGS,
    )
    (run,) = visit.runs
    assert run.shown.id == "b"
    assert (run.id, run.start, run.end) == ("a", START, START + 120)
    (empty,) = group([pic("e", 0, detections=0)], VisitSettings(every_picture_shows_a_pet=True))
    assert empty.runs[0].shown.id == "e"


def test_a_visit_ends_once_no_late_picture_can_join_it() -> None:
    (visit,) = group([pic("a", 0, ("pet_a",)), pic("b", 4, ("pet_a",))], SETTINGS)
    end = START + 4 * 60
    assert not visit.ended(end + 420, SETTINGS)
    assert visit.ended(end + 421, SETTINGS)


def test_a_visit_counts_for_the_day_it_started() -> None:
    late_evening = datetime(2026, 10, 11, 23, 58, tzinfo=ZONE).timestamp()
    pictures = [
        Picture("a", "feeder", late_evening, 1, ("pet_a",)),
        Picture("b", "feeder", late_evening + 240, 1, ("pet_b",)),
    ]
    (visit,) = group(pictures, SETTINGS)
    assert visit.day(ZONE) == date(2026, 10, 11)
    assert count([visit], date(2026, 10, 11), ZONE).pets == {"pet_a": 1, "pet_b": 1}
    assert count([visit], date(2026, 10, 12), ZONE).visits == 0
    # The same instant is already the next day further east.
    assert visit.day(ZoneInfo("Europe/Berlin")) == date(2026, 10, 12)


def test_count_counts_every_visit_and_each_pet_once_per_visit() -> None:
    visits = group(
        [
            pic("a", 0, ("pet_a",)),
            pic("b", 1, ("pet_a",)),
            pic("c", 30, resembles="pet_b"),
            pic("d", 60, ("pet_a", "pet_b"), corrected=True, group_id="d"),
            pic("e", 24 * 60, ("pet_a",)),
        ],
        SETTINGS,
    )
    counted = count(visits, date(2026, 10, 11), ZONE)
    assert counted.visits == 3
    assert counted.pets == {"pet_a": 2, "pet_b": 1}
    assert count([], date(2026, 10, 11), ZONE).visits == 0


def test_the_pets_of_a_picture_are_sorted_once() -> None:
    assert pic("a", 0, ("pet_b", "pet_a", "pet_b")).pets == ("pet_a", "pet_b")
    assert runs_of(pic("a", 0, ("pet_b", "pet_a")), pic("b", 1, ("pet_a", "pet_b"))) == [
        (["a", "b"], ("pet_a", "pet_b"))
    ]


def test_an_animal_with_no_best_candidate_stays_unknown() -> None:
    assert runs_of(pic("a", 0, ("pet_a",)), pic("b", 1)) == [
        (["a"], ("pet_a",)),
        (["b"], UNKNOWN),
    ]


def test_the_other_animal_counts_like_a_pet() -> None:
    visits = group([pic("a", 0, ("other_animal",)), pic("b", 30, ("pet_a",))], SETTINGS)
    counted = count(visits, date(2026, 10, 11), ZONE)
    assert (counted.visits, counted.pets) == (2, {"other_animal": 1, "pet_a": 1})


def test_an_empty_visit_has_no_pets() -> None:
    assert Visit([pic("a", 0)]).pets == ()


@pytest.mark.parametrize(("gap", "late"), [(0, 0), (-1, 0), (300, -1)])
def test_settings_stay_in_range(gap: float, late: float) -> None:
    with pytest.raises(ValueError, match="gap"):
        VisitSettings(gap, late)
