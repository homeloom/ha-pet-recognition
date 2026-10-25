"""Tests of when questions are asked and settled."""

import pytest

from custom_components.pet_recognition.core.doubts import (
    Doubt,
    Kind,
    Question,
    QuestionSettings,
    State,
    questions,
    review,
    settled,
)
from custom_components.pet_recognition.core.visits import (
    UNKNOWN,
    Picture,
    Visit,
    VisitSettings,
    group,
)

START = 1_800_000_000.0
VISITS = VisitSettings(gap=300, late=120)
SETTINGS = QuestionSettings(recent=7200, learning_expiry=7200)
# A visit is over 7 minutes after its last picture.
OVER = 421


def pic(
    picture_id: str,
    minutes: float,
    pets: tuple[str, ...] = UNKNOWN,
    *,
    detections: int = 1,
    resembles: str | None = None,
    corrected: bool = False,
) -> Picture:
    return Picture(
        picture_id,
        "feeder",
        START + minutes * 60,
        detections,
        pets,
        resembles,
        corrected,
        picture_id if corrected else "",
    )


def ask(
    pictures: list[Picture], now: float, doubts: list[Doubt] | None = None, **kwargs: bool
) -> list[Question]:
    return questions(
        "feeder", group(pictures, VISITS), doubts or [], now, VISITS, SETTINGS, **kwargs
    )


def doubt(
    *pictures: str, kind: Kind = Kind.UNSURE, asked: float = START, state: State = State.OPEN
) -> Doubt:
    return Doubt("q-" + pictures[0], "feeder", pictures, pictures[-1], kind, asked, state)


def test_an_unknown_run_is_asked_about_once_its_visit_is_over() -> None:
    pictures = [pic("a", 0, ("pet_a",)), pic("b", 1), pic("c", 2, detections=0)]
    end = START + 120
    assert ask(pictures, end + OVER - 1, learned=True) == []
    (question,) = ask(pictures, end + OVER, learned=True)
    assert question.kind is Kind.UNSURE
    assert [picture.id for picture in question.run.pictures] == ["b", "c"]
    assert question.visit.id == "a"
    created = Doubt.ask(question, "q1", end + OVER)
    assert created == Doubt("q1", "feeder", ("b", "c"), "b", Kind.UNSURE, end + OVER)


def test_a_visit_ended_too_long_ago_is_not_asked_about() -> None:
    pictures = [pic("a", 0)]
    assert ask(pictures, START + SETTINGS.recent, learned=True)
    assert ask(pictures, START + SETTINGS.recent + 1, learned=True) == []


def test_known_and_corrected_runs_are_not_asked_about() -> None:
    pictures = [pic("a", 0, ("pet_a",)), pic("b", 1, corrected=True)]
    assert ask(pictures, START + 3600, learned=True) == []


def test_a_run_is_asked_about_only_once() -> None:
    pictures = [pic("a", 0), pic("b", 1, ("pet_a",)), pic("c", 2)]
    now = START + 3600
    # Each run is asked about on its own; a picture already asked about,
    # even in a settled question, is never asked again.
    assert [question.run.id for question in ask(pictures, now, learned=True)] == ["a", "c"]
    asked = [doubt("a", state=State.SETTLED), doubt("x", "c")]
    assert ask(pictures, now, asked, learned=True) == []


def test_runs_are_asked_about_oldest_first() -> None:
    pictures = [pic("b", 20), pic("a", 0)]
    assert [question.run.id for question in ask(pictures, START + 3600, learned=True)] == [
        "a",
        "b",
    ]


def test_while_learning_one_question_is_open_per_source() -> None:
    pictures = [pic("a", 0), pic("b", 20)]
    now = START + 3600
    (question,) = ask(pictures, now, learned=False)
    assert (question.kind, question.run.id) == (Kind.LEARNING, "a")
    assert ask(pictures, now, [doubt("a", kind=Kind.LEARNING)], learned=False) == []
    # Once the first one is answered, the next one comes.
    answered = [doubt("a", kind=Kind.LEARNING, state=State.SETTLED)]
    assert [question.run.id for question in ask(pictures, now, answered, learned=False)] == ["b"]


@pytest.mark.parametrize(
    ("learned", "options"),
    [
        (True, {"ask_when_unsure": False}),
        (False, {"ask_while_learning": False}),
    ],
)
def test_questions_can_be_turned_off(learned: bool, options: dict[str, bool]) -> None:
    assert ask([pic("a", 0)], START + 3600, learned=learned, **options) == []


def test_a_learning_question_does_not_need_unsure_questions() -> None:
    assert ask([pic("a", 0)], START + 3600, learned=False, ask_when_unsure=False)


def review_after(pictures: list[Picture], *doubts: Doubt, now: float = START + 600) -> list[Doubt]:
    return review("feeder", doubts, group(pictures, VISITS), now, SETTINGS)


def test_a_question_is_settled_when_its_picture_is_recognised() -> None:
    question = doubt("b")
    assert review_after([pic("a", 0, ("pet_a",)), pic("b", 1)], question) == []
    assert question.state is State.OPEN
    assert review_after([pic("a", 0, ("pet_a",)), pic("b", 1, ("pet_b",))], question) == [question]
    assert question.state is State.SETTLED


def test_a_question_is_settled_by_a_correction_even_to_unknown() -> None:
    question = doubt("b")
    assert review_after([pic("b", 1, corrected=True)], question) == [question]
    assert question.state is State.SETTLED


def test_a_label_inherited_from_outside_the_question_does_not_settle_it() -> None:
    # b now takes pet_a from a, recognised since: b itself is still not told.
    question = doubt("b")
    pictures = [pic("a", 0, ("pet_a",)), pic("b", 1, resembles="pet_a")]
    assert review_after(pictures, question) == []
    assert question.state is State.OPEN


def test_a_head_in_the_bowl_follows_the_told_picture_of_its_run() -> None:
    question = doubt("b", "c")
    pictures = [pic("b", 1, corrected=True, pets=("pet_a",)), pic("c", 2, detections=0)]
    assert review_after(pictures, question) == [question]


def test_an_animal_inheriting_from_a_picture_of_the_question_is_told() -> None:
    question = doubt("b", "c")
    pictures = [pic("b", 1, ("pet_a",)), pic("c", 2, resembles="pet_a")]
    assert review_after(pictures, question) == [question]


def test_every_picture_of_the_question_must_be_told() -> None:
    question = doubt("b", "c")
    pictures = [pic("b", 1, ("pet_a",)), pic("c", 2, resembles="pet_b")]
    assert review_after(pictures, question) == []


def test_a_question_expires_when_its_pictures_are_gone() -> None:
    question = doubt("b")
    assert review_after([pic("a", 0, ("pet_a",))], question) == [question]
    assert question.state is State.EXPIRED


def test_a_learning_question_expires_when_nobody_answers() -> None:
    question = doubt("b", kind=Kind.LEARNING, asked=START)
    unsure = doubt("c", asked=START)
    pictures = [pic("b", 1), pic("c", 30)]
    assert review_after(pictures, question, unsure, now=START + 7200) == []
    assert review_after(pictures, question, unsure, now=START + 7201) == [question]
    assert (question.state, unsure.state) == (State.EXPIRED, State.OPEN)


def test_only_open_questions_are_reviewed() -> None:
    question = doubt("b", state=State.EXPIRED)
    assert review_after([pic("b", 1, ("pet_a",))], question) == []
    assert question.state is State.EXPIRED


def test_settled_needs_a_told_picture() -> None:
    (visit,) = group([pic("b", 1, detections=0)], VisitSettings(every_picture_shows_a_pet=True))
    assert not settled(doubt("b"), [(visit.pictures[0], visit.runs[0])])
    assert not settled(doubt("b"), [])


def test_pictures_outside_any_run_expire_open_questions() -> None:
    question = doubt("b")
    assert review("feeder", [question], [Visit([pic("x", 0)])], START, SETTINGS) == [question]


@pytest.mark.parametrize(("recent", "expiry"), [(0, 1), (1, 0), (-1, 1)])
def test_settings_stay_positive(recent: float, expiry: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        QuestionSettings(recent, expiry)


def test_questions_of_another_source_are_left_alone() -> None:
    elsewhere = Doubt("q-x", "litter_box", ("x",), "x", Kind.LEARNING, START + 3000)
    # An open learning question elsewhere does not block this source...
    assert ask([pic("a", 0)], START + 3600, [elsewhere], learned=False)
    # ... and is not expired because its pictures are not among these.
    assert review_after([pic("a", 0)], elsewhere) == []
    assert elsewhere.state is State.OPEN


def test_visits_of_another_source_are_refused() -> None:
    other = group([Picture("x", "litter_box", START, 1)], VISITS)
    with pytest.raises(ValueError, match="one source"):
        questions("feeder", other, [], START + 3600, VISITS, SETTINGS, learned=True)
    with pytest.raises(ValueError, match="one source"):
        review("feeder", [], other, START, SETTINGS)
    with pytest.raises(ValueError, match="one source"):
        questions(
            "feeder", other, [], START + 3600, VISITS, SETTINGS, learned=True, ask_when_unsure=False
        )


def test_a_learning_question_left_unanswered_too_long_lets_the_next_one_come() -> None:
    pictures = [pic("a", 0), pic("b", 20)]
    stale = doubt("a", kind=Kind.LEARNING, asked=START)
    now = START + SETTINGS.learning_expiry + 1
    assert [question.run.id for question in ask(pictures, now, [stale], learned=False)] == ["b"]
