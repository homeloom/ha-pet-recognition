"""Tests of corrections and answers to questions."""

from custom_components.pet_recognition.core.corrections import (
    Correction,
    answer,
    apply,
    correct,
    latest_run,
    run_of,
)
from custom_components.pet_recognition.core.doubts import Doubt, Kind, State
from custom_components.pet_recognition.core.visits import (
    UNKNOWN,
    Picture,
    VisitSettings,
    group,
)

START = 1_800_000_000.0
SETTINGS = VisitSettings(gap=300, late=120)


def pic(
    picture_id: str,
    minutes: float,
    pets: tuple[str, ...] = UNKNOWN,
    *,
    detections: int = 1,
    resembles: str | None = None,
) -> Picture:
    return Picture(picture_id, "feeder", START + minutes * 60, detections, pets, resembles)


def question(*pictures: str, shown: str | None = None) -> Doubt:
    return Doubt("q-" + pictures[0], "feeder", pictures, shown or pictures[-1], Kind.UNSURE, START)


def test_a_correction_relabels_a_whole_run_and_teaches_its_shown_picture() -> None:
    pictures = [pic("a", 0), pic("b", 1), pic("c", 2, detections=0)]
    (visit,) = group(pictures, SETTINGS)
    correction = correct(visit.runs[0], ["pet_a", "pet_a"])
    assert correction == Correction(("a", "b", "c"), ("pet_a",), "a", "b")
    assert apply(correction, pictures) == pictures
    assert [(picture.pets, picture.corrected, picture.group) for picture in pictures] == [
        (("pet_a",), True, "a")
    ] * 3


def test_only_a_single_animal_becomes_an_example() -> None:
    (visit,) = group([pic("a", 0), pic("b", 1, detections=2)], SETTINGS)
    run = visit.runs[0]
    # The shown picture has two animals: the other one teaches.
    assert correct(run, ["pet_a"]).example == "a"
    # Several pets, or none, teach nothing.
    assert correct(run, ["pet_b", "pet_a"]) == Correction(("a", "b"), ("pet_a", "pet_b"), "a", None)
    assert correct(run, []).example is None
    (crowded,) = group([pic("c", 0, detections=2)], SETTINGS)
    assert correct(crowded.runs[0], ["pet_a"]).example is None


def test_a_corrected_run_stays_apart_from_a_recognised_neighbour() -> None:
    pictures = [pic("a", 0, ("pet_a",)), pic("b", 1), pic("c", 2)]
    (visit,) = group(pictures, SETTINGS)
    apply(correct(visit.runs[1], ["pet_a"]), pictures)
    (visit,) = group(pictures, SETTINGS)
    assert [[picture.id for picture in run.pictures] for run in visit.runs] == [["a"], ["b", "c"]]


def test_an_answer_keeps_the_pictures_recognised_since() -> None:
    # Asked about b, c, d; c was recognised as pet_b since. The answer
    # covers b and d, and the shown picture d, but not c.
    pictures = [pic("b", 1), pic("c", 2, ("pet_b",)), pic("d", 3)]
    visits = group(pictures, SETTINGS)
    asked = question("b", "c", "d")
    correction = answer(asked, ["pet_a"], visits, [asked])
    assert correction == Correction(("b", "d"), ("pet_a",), "b", "d")


def test_the_shown_picture_follows_the_answer_even_if_recognised_since() -> None:
    pictures = [pic("b", 1), pic("c", 2, ("pet_b",))]
    asked = question("b", "c", shown="c")
    correction = answer(asked, ["pet_a"], group(pictures, SETTINGS), [asked])
    assert correction == Correction(("b", "c"), ("pet_a",), "b", "c")


def test_unknown_pictures_that_joined_the_run_follow_the_answer() -> None:
    # b was asked about; c and d came later and joined its unknown run; d
    # was asked about in another question, so it waits for its own answer.
    pictures = [pic("b", 1), pic("c", 2), pic("d", 3)]
    visits = group(pictures, SETTINGS)
    asked, other = question("b"), question("d")
    correction = answer(asked, ["pet_a"], visits, [asked, other])
    assert correction == Correction(("b", "c"), ("pet_a",), "b", "b")


def test_a_known_run_takes_no_joiners() -> None:
    # b, asked about, has since inherited pet_a from a: the answer covers
    # b alone, not the recognised picture a of the same run.
    pictures = [pic("a", 0, ("pet_a",)), pic("b", 1, resembles="pet_a")]
    asked = question("b")
    correction = answer(asked, ["pet_b"], group(pictures, SETTINGS), [asked])
    assert correction == Correction(("b",), ("pet_b",), "b", "b")


def test_an_answer_teaches_only_from_the_pictures_of_the_question() -> None:
    pictures = [pic("b", 1, detections=2), pic("c", 2)]
    asked = question("b")
    correction = answer(asked, ["pet_a"], group(pictures, SETTINGS), [asked])
    assert correction == Correction(("b", "c"), ("pet_a",), "b", None)


def test_an_answer_finds_its_shown_picture_again_when_the_notification_one_is_gone() -> None:
    pictures = [pic("b", 1), pic("c", 2, detections=0)]
    asked = question("b", "c", shown="gone")
    correction = answer(asked, ["pet_a"], group(pictures, SETTINGS), [asked])
    assert correction is not None
    assert correction.example == "b"


def test_an_answer_about_pictures_that_are_gone_does_nothing() -> None:
    visits = group([pic("x", 0)], SETTINGS)
    assert answer(question("b"), ["pet_a"], visits, []) is None


def test_an_answer_still_covers_the_pictures_left_when_the_first_is_gone() -> None:
    visits = group([pic("c", 2), pic("d", 3)], SETTINGS)
    asked = question("b", "c")
    assert answer(asked, ["pet_a"], visits, [asked]) == Correction(("c", "d"), ("pet_a",), "c", "c")


def test_joiners_come_from_the_run_of_the_shown_picture_too() -> None:
    # b, the first picture of the question, was recognised as pet_b since;
    # e joined the unknown run of c, the shown picture.
    pictures = [pic("b", 1, ("pet_b",)), pic("c", 2), pic("e", 3)]
    asked = question("b", "c", shown="c")
    assert answer(asked, ["pet_a"], group(pictures, SETTINGS), [asked]) == Correction(
        ("c", "e"), ("pet_a",), "c", "c"
    )


def test_an_answer_covers_its_pictures_split_into_several_runs() -> None:
    pictures = [pic("b", 1), pic("m", 2, ("pet_b",)), pic("c", 3)]
    asked = question("b", "c", shown="c")
    correction = answer(asked, ["pet_a"], group(pictures, SETTINGS), [asked])
    assert correction == Correction(("b", "c"), ("pet_a",), "b", "c")


def test_a_missing_shown_picture_is_never_one_recognised_since() -> None:
    pictures = [pic("b", 1), pic("c", 2, ("pet_b",))]
    asked = question("b", "c", shown="gone")
    correction = answer(asked, ["pet_a"], group(pictures, SETTINGS), [asked])
    assert correction == Correction(("b",), ("pet_a",), "b", "b")


def test_an_answer_to_a_question_no_longer_open_does_nothing() -> None:
    visits = group([pic("b", 1)], SETTINGS)
    for state in (State.SETTLED, State.EXPIRED):
        asked = question("b")
        asked.state = state
        assert answer(asked, ["pet_a"], visits, [asked]) is None


def test_an_answer_to_a_question_told_since_does_nothing() -> None:
    # The user corrected b by hand before the question was reviewed: a late
    # tap on the notification must not undo it.
    pictures = [pic("b", 1)]
    (visit,) = group(pictures, SETTINGS)
    apply(correct(visit.runs[0], ["pet_b"]), pictures)
    asked = question("b")
    assert answer(asked, ["pet_a"], group(pictures, SETTINGS), [asked]) is None


def test_a_question_about_a_visit_without_animals_teaches_nothing() -> None:
    settings = VisitSettings(every_picture_shows_a_pet=True)
    asked = question("b")
    correction = answer(asked, ["pet_a"], group([pic("b", 1, detections=0)], settings), [asked])
    assert correction == Correction(("b",), ("pet_a",), "b", None)


def test_a_corrected_run_can_be_corrected_again() -> None:
    pictures = [pic("a", 0), pic("b", 1)]
    (visit,) = group(pictures, SETTINGS)
    apply(correct(visit.runs[0], ["pet_a"]), pictures)
    (visit,) = group(pictures, SETTINGS)
    correction = correct(visit.runs[0], ["pet_a", "pet_b"])
    apply(correction, pictures)
    (visit,) = group(pictures, SETTINGS)
    assert [run.pets for run in visit.runs] == [("pet_a", "pet_b")]
    assert correction.example is None


def test_runs_are_found_by_picture_and_by_time() -> None:
    visits = group([pic("a", 0, ("pet_a",)), pic("b", 1, ("pet_b",)), pic("c", 30)], SETTINGS)
    assert run_of(visits, "b").id == "b"
    assert run_of(visits, "missing") is None
    assert latest_run(visits).id == "c"
    assert latest_run([]) is None
