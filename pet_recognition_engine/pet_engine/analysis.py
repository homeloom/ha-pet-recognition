"""Analyse a picture: find its animals, cut each one out and embed it."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from .detector import SPECIES, Detector
from .embedder import Embedder
from .images import Box, crop, encode_jpeg


@dataclass(frozen=True)
class AnalysisSettings:
    """Which animals to keep.

    `min_area`: the smallest share of the picture an animal covers, to leave
    out what is too far or too small to tell. `limit`: the most animals kept,
    the most certain first. `species`: the species to look for. `margin`:
    the share of an animal's size kept around its crop.
    """

    min_area: float = 0.01
    limit: int = 5
    species: frozenset[str] = frozenset(SPECIES.values())
    margin: float = 0.08

    def __post_init__(self) -> None:
        if not 0 <= self.min_area < 1:
            raise ValueError("the minimum area must be in [0, 1[")
        if self.limit < 1:
            raise ValueError("at least one animal must be kept")
        if not self.species or not self.species <= set(SPECIES.values()):
            raise ValueError(f"the species must be among {sorted(SPECIES.values())}")
        if not 0 <= self.margin <= 1:
            raise ValueError("the margin must be in [0, 1]")


@dataclass(frozen=True)
class Animal:
    """An animal of a picture: where it is, its crop and its embedding.

    `area` is the share of the picture its box covers. The crop is shrunk to
    at most 512 pixels on its longest side; the embedding comes from the crop
    at full size.
    """

    species: str
    score: float
    box: Box
    area: float
    crop: bytes
    embedding: np.ndarray


@dataclass(frozen=True)
class Analysis:
    """The animals of a picture, most certain first."""

    width: int
    height: int
    animals: tuple[Animal, ...]


def analyse(
    image: Image.Image, detector: Detector, embedder: Embedder, settings: AnalysisSettings
) -> Analysis:
    """The animals of `image` that `settings` keep."""
    size = image.width * image.height
    animals = []
    for detection in detector.detect(image):
        if len(animals) == settings.limit:
            break
        area = detection.box.area / size
        if detection.species not in settings.species or area < settings.min_area:
            continue
        cut = crop(image, detection.box, settings.margin)
        animals.append(
            Animal(
                detection.species,
                detection.score,
                detection.box,
                area,
                encode_jpeg(cut),
                embedder.embed(cut),
            )
        )
    return Analysis(image.width, image.height, tuple(animals))
