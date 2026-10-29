"""Tests of the matching of embeddings to pets."""

import math

import pytest

from custom_components.pet_recognition.core.matching import (
    NEAR_DUPLICATE,
    OTHER_ANIMAL,
    Added,
    Example,
    Gallery,
    MatchSettings,
    Nearest,
    decide,
    normalise,
    resembles,
    similarity,
    tell,
)

MODEL = "model-a"


def unit(angle: float) -> tuple[float, ...]:
    """A unit vector in the plane, `angle` degrees from the first axis."""
    return normalise((math.cos(math.radians(angle)), math.sin(math.radians(angle))))


def example(
    example_id: str,
    pet: str,
    angle: float,
    added: float = 0,
    *,
    uploaded: bool = False,
    model: str = MODEL,
) -> Example:
    return Example(example_id, pet, unit(angle), model, added, uploaded)


def test_normalise_makes_a_unit_vector() -> None:
    vector = normalise([3, 4])
    assert vector == pytest.approx((0.6, 0.8))
    assert similarity(vector, vector) == pytest.approx(1)


@pytest.mark.parametrize("scale", [1e-200, 1e200, 1e307])
def test_normalise_takes_huge_and_tiny_values(scale: float) -> None:
    assert normalise([3 * scale, 4 * scale]) == pytest.approx((0.6, 0.8))


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "at least one value"),
        ([0, 0], "zero vector"),
        ([1, math.nan], "must be finite"),
        ([math.inf, 1], "must be finite"),
    ],
)
def test_normalise_refuses_vectors_without_a_direction(values: list[float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalise(values)


def test_similarity_is_the_cosine() -> None:
    assert similarity(unit(0), unit(60)) == pytest.approx(0.5)
    assert similarity(unit(0), unit(180)) == pytest.approx(-1)


def test_similarity_refuses_different_sizes() -> None:
    with pytest.raises(ValueError, match="sizes"):
        similarity((1.0,), (1.0, 0.0))


def test_gallery_lists_examples_by_pet_and_model() -> None:
    gallery = Gallery()
    first, second = example("1", "pet_a", 0, 2), example("2", "pet_a", 90, 1)
    other_model = example("3", "pet_a", 45, model="model-b")
    for item in (first, second, other_model, example("4", "pet_b", 30)):
        assert gallery.add(item) == (Added.ADDED, [])
    assert len(gallery) == 4
    assert gallery.of("pet_a", MODEL) == [second, first]
    assert gallery.of("pet_a") == [other_model, second, first]
    assert gallery.pets(MODEL) == {"pet_a", "pet_b"}
    assert gallery.pets("model-b") == {"pet_a"}
    assert gallery.get("2") == second
    assert {item.id for item in gallery} == {"1", "2", "3", "4"}


def test_gallery_refuses_a_near_duplicate_of_the_same_pet_and_model() -> None:
    gallery = Gallery()
    gallery.add(example("1", "pet_a", 0))
    close = math.degrees(math.acos(NEAR_DUPLICATE)) / 2
    assert gallery.add(example("2", "pet_a", close)) == (Added.DUPLICATE, [])
    # The same picture can teach another pet, or another model.
    assert gallery.add(example("3", "pet_b", close))[0] is Added.ADDED
    assert gallery.add(example("4", "pet_a", close, model="model-b"))[0] is Added.ADDED


def test_an_uploaded_example_takes_the_place_of_the_learned_ones_it_duplicates() -> None:
    gallery = Gallery(limit=2)
    learned = example("learned", "pet_a", 0)
    gallery.add(learned)
    upload = example("upload", "pet_a", 0, 1, uploaded=True)
    assert gallery.add(upload) == (Added.ADDED, [learned])
    # It is kept when learned examples come and go.
    for index, angle in enumerate((90, 180, 270)):
        gallery.add(example(f"learned_{index}", "pet_a", angle, 2 + index))
    assert [item.id for item in gallery.of("pet_a")] == ["upload", "learned_2"]
    # The other way, a learned example adds nothing, and neither does a
    # second upload.
    assert gallery.add(example("again", "pet_a", 0, 9)) == (Added.DUPLICATE, [])
    assert gallery.add(example("twice", "pet_a", 0, 9, uploaded=True)) == (Added.DUPLICATE, [])
    assert [item.id for item in gallery.of("pet_a")] == ["upload", "learned_2"]


def test_gallery_refuses_an_id_twice() -> None:
    gallery = Gallery()
    gallery.add(example("1", "pet_a", 0))
    with pytest.raises(ValueError, match="already exists"):
        gallery.add(example("1", "pet_b", 90))


def test_a_full_pet_drops_its_oldest_learned_example() -> None:
    gallery = Gallery(limit=3)
    gallery.add(example("old_upload", "pet_a", 0, 1, uploaded=True))
    gallery.add(example("old_learned", "pet_a", 30, 2))
    gallery.add(example("new_learned", "pet_a", 60, 3))
    gallery.add(example("other_pet", "pet_b", 0, 0))
    status, removed = gallery.add(example("newest", "pet_a", 90, 4))
    assert status is Added.ADDED
    assert removed == [example("old_learned", "pet_a", 30, 2)]
    assert [item.id for item in gallery.of("pet_a")] == ["old_upload", "new_learned", "newest"]
    assert gallery.get("other_pet") is not None


def test_a_pet_full_of_uploaded_examples_refuses_more() -> None:
    gallery = Gallery(limit=2)
    gallery.add(example("1", "pet_a", 0, uploaded=True))
    gallery.add(example("2", "pet_a", 90, uploaded=True))
    assert gallery.add(example("3", "pet_a", 45, uploaded=True)) == (Added.FULL, [])
    assert gallery.add(example("4", "pet_a", 135)) == (Added.FULL, [])
    assert len(gallery) == 2


def test_a_refusal_drops_nothing_even_after_choosing_what_would_make_room() -> None:
    gallery = Gallery(limit=3)
    for item in (
        example("1", "pet_a", 0, 1, uploaded=True),
        example("2", "pet_a", 90, 2, uploaded=True),
        example("3", "pet_a", 180, 3),
    ):
        gallery.add(item)
    gallery.limit = 1
    # The learned example would make room, but two uploaded ones remain.
    assert gallery.add(example("4", "pet_a", 270, 4)) == (Added.FULL, [])
    assert [item.id for item in gallery.of("pet_a")] == ["1", "2", "3"]


def test_examples_of_an_older_model_do_not_fill_a_pet() -> None:
    gallery = Gallery(limit=1)
    gallery.add(example("old", "pet_a", 0, uploaded=True, model="model-old"))
    assert gallery.add(example("new", "pet_a", 0, uploaded=True)) == (Added.ADDED, [])


def test_a_smaller_limit_makes_room_for_several() -> None:
    gallery = Gallery(limit=3)
    for index in range(3):
        gallery.add(example(str(index), "pet_a", index * 40, index))
    gallery.limit = 1
    status, removed = gallery.add(example("new", "pet_a", 170, 9))
    assert status is Added.ADDED
    assert [item.id for item in removed] == ["0", "1", "2"]


def test_gallery_needs_room_for_one_example() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Gallery(limit=0)


def test_gallery_restores_examples_without_checks() -> None:
    gallery = Gallery(limit=1)
    gallery.add(example("1", "pet_a", 0, uploaded=True))
    # A duplicate beyond the limit, as stored before.
    gallery.restore([example("2", "pet_a", 0, uploaded=True)])
    assert len(gallery.of("pet_a")) == 2
    with pytest.raises(ValueError, match="restored twice"):
        gallery.restore([example("2", "pet_a", 90)])
    with pytest.raises(ValueError, match="restored twice"):
        gallery.restore([example("3", "pet_a", 90), example("3", "pet_a", 90)])
    assert gallery.get("3") is None


def test_gallery_examples_are_not_a_field() -> None:
    gallery = Gallery()
    gallery.add(example("1", "pet_a", 0))
    assert repr(gallery) == "Gallery(limit=200)"
    assert gallery == Gallery()
    with pytest.raises(TypeError):
        Gallery(_examples={})  # type: ignore[call-arg]


def test_gallery_removes_examples() -> None:
    gallery = Gallery()
    first = example("1", "pet_a", 0)
    gallery.add(first)
    gallery.add(example("2", "pet_b", 90))
    gallery.add(example("3", "pet_b", 0))
    assert gallery.remove("1") == first
    assert gallery.remove("1") is None
    assert [item.id for item in gallery.remove_pet("pet_b")] == ["2", "3"]
    assert len(gallery) == 0


def test_nearest_scores_with_the_three_closest_examples() -> None:
    gallery = Gallery()
    for index, angle in enumerate((0, 20, 40, 90)):
        assert gallery.add(example(f"a{index}", "pet_a", angle))[0] is Added.ADDED
    gallery.add(example("b", "pet_b", 70))
    gallery.add(example("old", "pet_b", 20, model="model-b"))
    scores = Nearest.compute(unit(20), MODEL, gallery).scores()
    # pet_a: itself and the two examples 20 degrees away; pet_b: one example
    # of this model, 50 degrees away.
    closest_a = (1 + 2 * math.cos(math.radians(20))) / 3
    assert scores == pytest.approx({"pet_a": closest_a, "pet_b": math.cos(math.radians(50))})


def added(gallery: Gallery, nearest: Nearest, item: Example) -> None:
    status, removed = gallery.add(item)
    assert status is Added.ADDED
    nearest.added(item, removed, gallery)


def test_nearest_follows_one_example_at_a_time() -> None:
    gallery = Gallery()
    gallery.add(example("a", "pet_a", 90))
    nearest = Nearest.compute(unit(0), MODEL, gallery)
    for item in (example("b", "pet_a", 0), example("c", "pet_b", 60)):
        added(gallery, nearest, item)
    added(gallery, nearest, example("d", "pet_b", 0, model="model-b"))
    assert nearest.scores() == pytest.approx(Nearest.compute(unit(0), MODEL, gallery).scores())
    assert nearest.scores() == pytest.approx({"pet_a": 0.5, "pet_b": 0.5})


def test_nearest_keeps_only_the_closest_similarities() -> None:
    gallery = Gallery()
    nearest = Nearest(unit(0), MODEL)
    for index, angle in enumerate((80, 0, 55, 110, 30)):
        added(gallery, nearest, example(str(index), "pet_a", angle, index))
    assert nearest.top["pet_a"] == pytest.approx(
        (1, math.cos(math.radians(30)), math.cos(math.radians(55)))
    )


@pytest.mark.parametrize(
    ("angles", "new", "decided"),
    [
        # The example that makes room, at 40 degrees, is one of the closest.
        ((40, 70, 100), 15, "pet_b"),
        # The example that makes room, at 170 degrees, is not.
        ((170, 15, 30, 45), 0, "pet_a"),
    ],
)
def test_nearest_follows_an_example_that_makes_room(
    angles: tuple[float, ...], new: float, decided: str
) -> None:
    gallery = Gallery(limit=len(angles))
    for index, angle in enumerate(angles):
        gallery.add(example(f"a{index}", "pet_a", angle, index))
    gallery.add(example("b", "pet_b", -45))
    nearest = Nearest.compute(unit(0), MODEL, gallery)
    added(gallery, nearest, example("new", "pet_a", new, 9))
    assert gallery.get("a0") is None
    scores = Nearest.compute(unit(0), MODEL, gallery).scores()
    assert nearest.scores() == pytest.approx(scores)
    assert decide(nearest.scores(), ["pet_a", "pet_b"], MatchSettings()) == decided


def test_nearest_follows_an_upload_that_replaces_its_duplicate() -> None:
    gallery = Gallery()
    gallery.add(example("learned", "pet_a", 0))
    nearest = Nearest.compute(unit(10), MODEL, gallery)
    added(gallery, nearest, example("upload", "pet_a", 1, uploaded=True))
    assert nearest.scores() == pytest.approx(Nearest.compute(unit(10), MODEL, gallery).scores())
    assert nearest.scores() == pytest.approx({"pet_a": math.cos(math.radians(9))})


def test_nearest_refreshes_a_pet_after_a_removal() -> None:
    gallery = Gallery()
    gallery.add(example("a", "pet_a", 0))
    gallery.add(example("b", "pet_b", 0))
    nearest = Nearest.compute(unit(0), MODEL, gallery)
    gallery.remove("a")
    nearest.refresh("pet_a", gallery)
    assert nearest.scores() == pytest.approx({"pet_b": 1})


SETTINGS = MatchSettings(threshold=0.6, margin=0.04)


@pytest.mark.parametrize(
    ("scores", "expected", "decided"),
    [
        ({"pet_a": 0.8, "pet_b": 0.7}, ["pet_a", "pet_b"], "pet_a"),
        ({"pet_a": 0.7, "pet_b": 0.8}, ["pet_a", "pet_b"], "pet_b"),
        # Too close to the next pet, or not similar enough.
        ({"pet_a": 0.8, "pet_b": 0.79}, ["pet_a", "pet_b"], None),
        ({"pet_a": 0.59, "pet_b": 0.1}, ["pet_a", "pet_b"], None),
        # A pet without examples could be the one in the picture.
        ({"pet_a": 0.9}, ["pet_a", "pet_b"], None),
        # A pet that never comes to this source does not compete.
        ({"pet_a": 0.8, "pet_b": 0.79}, ["pet_a"], "pet_a"),
        ({"pet_a": 0.6}, ["pet_a"], "pet_a"),
        # The other animal competes once it has examples, and is never required.
        ({"pet_a": 0.8, OTHER_ANIMAL: 0.9}, ["pet_a"], OTHER_ANIMAL),
        ({"pet_a": 0.8, OTHER_ANIMAL: 0.78}, ["pet_a"], None),
        ({"pet_a": 0.8, OTHER_ANIMAL: 0.5}, ["pet_a", OTHER_ANIMAL], "pet_a"),
        ({OTHER_ANIMAL: 0.9}, [OTHER_ANIMAL], None),
        ({"pet_a": 0.8, "pet_b": 0.5}, ["pet_a", "pet_b", "pet_a"], "pet_a"),
        ({"pet_a": 0.8}, [], None),
    ],
)
def test_decide(scores: dict[str, float], expected: list[str], decided: str | None) -> None:
    assert decide(scores, expected, SETTINGS) == decided


def test_exactly_the_threshold_and_the_margin_is_enough() -> None:
    settings = MatchSettings(threshold=0.5, margin=0.25)
    assert decide({"pet_a": 0.75, "pet_b": 0.5}, ["pet_a", "pet_b"], settings) == "pet_a"
    assert decide({"pet_a": 0.5, "pet_b": 0.25}, ["pet_a", "pet_b"], settings) == "pet_a"
    assert decide({"pet_a": 0.5, "pet_b": 0.375}, ["pet_a", "pet_b"], settings) is None


def test_a_zero_margin_breaks_ties_by_order() -> None:
    assert (
        decide({"pet_a": 0.7, "pet_b": 0.7}, ["pet_a", "pet_b"], MatchSettings(margin=0)) == "pet_a"
    )


@pytest.mark.parametrize(
    ("scores", "expected", "best"),
    [
        ({"pet_a": 0.3, "pet_b": 0.4}, ["pet_a", "pet_b"], "pet_b"),
        ({"pet_a": 0.3}, ["pet_a", "pet_b"], "pet_a"),
        ({"pet_a": 0.3, "pet_c": 0.9}, ["pet_a"], "pet_a"),
        ({"pet_a": 0.3, OTHER_ANIMAL: 0.5}, ["pet_a"], OTHER_ANIMAL),
        ({}, ["pet_a"], None),
    ],
)
def test_resembles(scores: dict[str, float], expected: list[str], best: str | None) -> None:
    assert resembles(scores, expected) == best


@pytest.mark.parametrize(
    ("threshold", "margin"), [(1.5, 0.04), (-1.5, 0.04), (0.6, -0.1), (0.6, 2.5)]
)
def test_settings_stay_in_range(threshold: float, margin: float) -> None:
    with pytest.raises(ValueError, match="from"):
        MatchSettings(threshold, margin)


@pytest.mark.parametrize(
    ("angles", "pets", "closest"),
    [
        # One animal, told or not, and the pet it resembles.
        ([0], ("a",), "a"),
        ([45], (), "a"),
        # Each animal told: the pets of the picture, once each and sorted.
        ([90, 0, 2], ("a", "b"), None),
        # One animal not told leaves the picture unknown.
        ([90, 45], (), None),
        # No animal tells nothing.
        ([], (), None),
    ],
)
def test_tell(angles: list[float], pets: tuple[str, ...], closest: str | None) -> None:
    gallery = Gallery()
    for number, (pet, angle) in enumerate([("a", 0), ("b", 90), ("b", 92)]):
        gallery.add(example(f"{number}", pet, angle))
    told = tell([(unit(angle), MODEL) for angle in angles], gallery, ["b", "a"], MatchSettings())
    assert told.pets == pets
    assert told.resembles == closest
    assert told.scores == tuple(
        Nearest.compute(unit(angle), MODEL, gallery).scores() for angle in angles
    )
