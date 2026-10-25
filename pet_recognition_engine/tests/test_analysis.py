"""Tests of analysing a picture from end to end."""

import io
from pathlib import Path

import numpy as np
import pytest
from helpers import dinov2, picture, rows_for, yolox
from PIL import Image

from pet_engine.analysis import Analysis, AnalysisSettings, analyse
from pet_engine.detector import Detector
from pet_engine.embedder import Embedder
from pet_engine.images import Box, open_image
from pet_engine.sessions import load

CAT, DOG, BIRD = 15, 16, 14


def models(tmp_path: Path, *boxes: tuple[float, float, float, float, int, float]) -> tuple:
    detector = Detector(load(yolox(tmp_path / "yolox.onnx", rows_for(*boxes))))
    return detector, Embedder(load(dinov2(tmp_path / "dinov2.onnx")), "test-model")


def two_coloured() -> Image.Image:
    """A 128 by 64 picture, red on the left half and blue on the right."""
    image = Image.new("RGB", (128, 64), (220, 20, 20))
    image.paste((20, 20, 220), (64, 0, 128, 64))
    return image


def test_each_animal_gets_its_crop_and_embedding(tmp_path: Path) -> None:
    detector, embedder = models(tmp_path, (2, 2, 30, 30, CAT, 0.9), (34, 2, 62, 30, DOG, 0.8))
    result = analyse(two_coloured(), detector, embedder, AnalysisSettings())
    assert (result.width, result.height) == (128, 64)
    cat, dog = result.animals
    assert (cat.species, dog.species) == ("cat", "dog")
    assert cat.box == Box(4, 4, 60, 60)
    assert round(cat.area, 4) == round(56 * 56 / (128 * 64), 4)
    with Image.open(io.BytesIO(cat.crop)) as crop:
        assert crop.format == "JPEG"
        red, _, blue = crop.convert("RGB").getpixel((crop.width // 2, crop.height // 2))
        assert red > blue
    # Red and blue animals are told apart.
    assert float(cat.embedding @ dog.embedding) < 0.99
    assert np.isclose(np.linalg.norm(dog.embedding), 1)


def test_small_animals_and_other_species_are_left_out(tmp_path: Path) -> None:
    detector, embedder = models(
        tmp_path, (2, 2, 30, 30, CAT, 0.9), (40, 40, 42, 42, DOG, 0.8), (34, 2, 62, 30, BIRD, 0.7)
    )
    settings = AnalysisSettings(min_area=0.01, species=frozenset({"cat", "dog"}))
    result = analyse(two_coloured(), detector, embedder, settings)
    assert [animal.species for animal in result.animals] == ["cat"]


def test_only_the_most_certain_animals_are_kept(tmp_path: Path) -> None:
    detector, embedder = models(
        tmp_path, (2, 2, 30, 30, CAT, 0.6), (34, 2, 62, 30, DOG, 0.9), (2, 34, 30, 62, CAT, 0.7)
    )
    image = Image.new("RGB", (64, 64), (90, 90, 90))
    result = analyse(image, detector, embedder, AnalysisSettings(limit=2))
    assert [round(animal.score, 1) for animal in result.animals] == [0.9, 0.7]


def test_a_picture_without_animals_gives_none(tmp_path: Path) -> None:
    detector, embedder = models(tmp_path)
    image = open_image(picture())
    assert analyse(image, detector, embedder, AnalysisSettings()) == Analysis(320, 240, ())


@pytest.mark.parametrize(
    "settings",
    [
        {"min_area": 1},
        {"min_area": -0.1},
        {"limit": 0},
        {"species": frozenset()},
        {"species": frozenset({"horse"})},
        {"margin": 2},
    ],
)
def test_settings_stay_in_range(settings: dict) -> None:
    with pytest.raises(ValueError, match="must"):
        AnalysisSettings(**settings)
