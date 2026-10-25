"""Tell which pet a picture shows, from the embeddings of example pictures.

The engine turns each animal it detects into an embedding: a unit vector
that stays close for pictures of the same animal. A pet is described by the
embeddings of its examples. A picture resembles a pet as much as the mean
cosine similarity to that pet's closest examples, and it is decided only
when the best pet is both similar enough and clearly ahead of the next one.

Embeddings from different models cannot be compared, so every embedding is
tagged with the model that produced it, and others are ignored.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

# The pseudo-pet for animals that are none of the declared pets, such as a
# visiting neighbour's cat. It learns from corrections like any pet.
OTHER_ANIMAL = "other_animal"

# How many of a pet's closest examples make its score.
NEIGHBOURS = 3

# A new example this close to an existing one of the same pet adds nothing.
NEAR_DUPLICATE = 0.98

Vector = tuple[float, ...]


def normalise(values: Iterable[float]) -> Vector:
    """`values` scaled to a unit vector, so that a dot product is a cosine."""
    vector = tuple(float(value) for value in values)
    if not vector:
        raise ValueError("an embedding needs at least one value")
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("every value of an embedding must be finite")
    largest = max(abs(value) for value in vector)
    if largest == 0:
        raise ValueError("an embedding cannot be the zero vector")
    # Scaled first, so that neither huge nor tiny values overflow.
    scaled = tuple(value / largest for value in vector)
    norm = math.hypot(*scaled)
    return tuple(value / norm for value in scaled)


def similarity(first: Vector, second: Vector) -> float:
    """The cosine similarity of two unit vectors, from -1 to 1."""
    if len(first) != len(second):
        raise ValueError("embeddings of different sizes cannot be compared")
    return math.fsum(a * b for a, b in zip(first, second, strict=True))


@dataclass(frozen=True)
class Example:
    """The embedding of one example picture of a pet.

    `uploaded` examples were given by the user and are never dropped to make
    room; the others were learned from answers and corrections.
    """

    id: str
    pet: str
    vector: Vector
    model: str
    added: float
    uploaded: bool = False


class Added(StrEnum):
    """What `Gallery.add` did with an example."""

    ADDED = "added"
    DUPLICATE = "duplicate"
    FULL = "full"


@dataclass
class Gallery:
    """The examples of every pet, at most `limit` per pet and model.

    When a pet is full for a model, its oldest learned example of that model
    makes room for a new one. A pet full of uploaded examples refuses new
    ones until some are removed. Examples of another model are never evicted
    to make room: the caller removes them when the model changes.
    """

    limit: int = 200
    _examples: dict[str, Example] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("a pet must be allowed at least one example")

    def __len__(self) -> int:
        return len(self._examples)

    def __iter__(self) -> Iterator[Example]:
        return iter(self._examples.values())

    def get(self, example_id: str) -> Example | None:
        """The example with this id, if any."""
        return self._examples.get(example_id)

    def of(self, pet: str, model: str | None = None) -> list[Example]:
        """The examples of `pet`, oldest first, only those of `model` if given."""
        found = [
            example
            for example in self._examples.values()
            if example.pet == pet and (model is None or example.model == model)
        ]
        return sorted(found, key=lambda example: (example.added, example.id))

    def pets(self, model: str | None = None) -> set[str]:
        """The pets with at least one example (of `model`, if given)."""
        return {
            example.pet
            for example in self._examples.values()
            if model is None or example.model == model
        }

    def add(self, example: Example) -> tuple[Added, list[Example]]:
        """Add `example`, and say what happened and which examples made room.

        A near duplicate of an example of the same pet and model adds
        nothing, except an uploaded example that only duplicates learned
        ones: it takes their place, so that the user's example is the one
        kept. The limit counts the examples of the same pet and model, so
        examples of an older model never block new ones.

        The caller deletes the files of the examples that made room, and
        updates the `Nearest` of each picture with `Nearest.added`.
        """
        if example.id in self._examples:
            raise ValueError(f"example {example.id} already exists")
        mine = self.of(example.pet, example.model)
        duplicates = [
            other for other in mine if similarity(example.vector, other.vector) >= NEAR_DUPLICATE
        ]
        if duplicates and (not example.uploaded or any(other.uploaded for other in duplicates)):
            return Added.DUPLICATE, []
        removed = duplicates
        while len(mine) - len(removed) >= self.limit:
            learned = [other for other in mine if not other.uploaded and other not in removed]
            if not learned:
                # Nothing was deleted yet, so the refusal changes nothing.
                return Added.FULL, []
            removed.append(learned[0])
        for other in removed:
            del self._examples[other.id]
        self._examples[example.id] = example
        return Added.ADDED, removed

    def restore(self, examples: Iterable[Example]) -> None:
        """Put back examples that the gallery held before, such as from storage.

        Their duplicates and the limit were checked when they were first
        added, and are not checked again: rounding in storage could make a
        pair look like duplicates. It is also how an uploaded example keeps
        its place when it is embedded again with another model. Only their
        IDs must be new.
        """
        examples = list(examples)
        ids = [example.id for example in examples]
        if len(set(ids)) != len(ids) or not self._examples.keys().isdisjoint(ids):
            raise ValueError("examples restored twice")
        self._examples.update((example.id, example) for example in examples)

    def remove(self, example_id: str) -> Example | None:
        """Remove one example, and return it if it existed."""
        return self._examples.pop(example_id, None)

    def remove_pet(self, pet: str) -> list[Example]:
        """Remove every example of `pet`, and return them."""
        removed = self.of(pet)
        for example in removed:
            del self._examples[example.id]
        return removed


@dataclass(frozen=True)
class MatchSettings:
    """When a score is good enough to decide.

    `threshold`: the lowest score of the decided pet. `margin`: how far
    ahead of the next candidate it must be.
    """

    threshold: float = 0.6
    margin: float = 0.04

    def __post_init__(self) -> None:
        if not -1 <= self.threshold <= 1:
            raise ValueError("the threshold is a cosine similarity, from -1 to 1")
        if not 0 <= self.margin <= 2:
            raise ValueError("the margin is a difference of similarities, from 0 to 2")


@dataclass
class Nearest:
    """The closest similarities of one picture to the examples of each pet.

    They are kept with the picture, so that a new example usually costs a
    single comparison per picture, instead of comparing again with every
    example. It costs comparing again with the pet's examples only when an
    example that made room was one of the picture's closest.
    """

    vector: Vector
    model: str
    top: dict[str, tuple[float, ...]] = field(default_factory=dict)

    @classmethod
    def compute(cls, vector: Vector, model: str, gallery: Gallery) -> Nearest:
        """Compare `vector` with every example of `model` in `gallery`."""
        nearest = cls(vector, model)
        for pet in gallery.pets(model):
            nearest.refresh(pet, gallery)
        return nearest

    def added(self, example: Example, removed: Iterable[Example], gallery: Gallery) -> None:
        """Follow `Gallery.add`, given the example it added and those that made room.

        This is the one call after an add: it never counts an example twice.
        """
        if example.model != self.model:
            return
        best = self.top.get(example.pet, ())
        gone = [similarity(self.vector, other.vector) for other in removed]
        if gone and (len(best) < NEIGHBOURS or max(gone) >= min(best)):
            # One of the closest made room: the gallery already holds the new one.
            self.refresh(example.pet, gallery)
            return
        best = (*best, similarity(self.vector, example.vector))
        self.top[example.pet] = tuple(sorted(best, reverse=True)[:NEIGHBOURS])

    def refresh(self, pet: str, gallery: Gallery) -> None:
        """Compare again with the examples of `pet`, such as after some were removed."""
        best = sorted(
            (similarity(self.vector, example.vector) for example in gallery.of(pet, self.model)),
            reverse=True,
        )[:NEIGHBOURS]
        if best:
            self.top[pet] = tuple(best)
        else:
            self.top.pop(pet, None)

    def scores(self) -> dict[str, float]:
        """The score of each pet with examples: the mean of its closest similarities."""
        return {pet: math.fsum(best) / len(best) for pet, best in self.top.items()}


def decide(
    scores: Mapping[str, float], expected: Sequence[str], settings: MatchSettings
) -> str | None:
    """The pet a picture shows, or None when unsure.

    `expected` lists the pets that may appear at this source. Any of them
    without examples could be the one in the picture, so nothing is decided
    until each has at least one. The other animal competes with the expected
    pets once it has examples, but it is never required.
    """
    required = [pet for pet in expected if pet != OTHER_ANIMAL]
    if not required or any(pet not in scores for pet in required):
        return None
    ranked = _ranked(scores, expected)
    best = scores[ranked[0]]
    second = scores[ranked[1]] if len(ranked) > 1 else -1.0
    if best >= settings.threshold and best - second >= settings.margin:
        return ranked[0]
    return None


def resembles(scores: Mapping[str, float], expected: Sequence[str]) -> str | None:
    """The candidate with the best score, decided or not, among those with examples."""
    ranked = _ranked(scores, expected)
    return ranked[0] if ranked else None


def _ranked(scores: Mapping[str, float], expected: Sequence[str]) -> list[str]:
    """The expected pets and the other animal that have a score, best first."""
    candidates = [pet for pet in dict.fromkeys((*expected, OTHER_ANIMAL)) if pet in scores]
    return sorted(candidates, key=lambda pet: scores[pet], reverse=True)
