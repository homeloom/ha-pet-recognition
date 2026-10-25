"""Tests of finding animals with YOLOX."""

from pathlib import Path

import numpy as np
import pytest
from helpers import CLASSES, SIZE, rows_for, yolox
from onnx import TensorProto, helper
from PIL import Image

from pet_engine import detector as detector_module
from pet_engine.detector import Detector, anchors, decode, letterbox, suppress
from pet_engine.images import Box
from pet_engine.sessions import ModelError, load

CAT, DOG, BIRD, PERSON, TEDDY_BEAR = 15, 16, 14, 0, 77


def detector(tmp_path: Path, rows: np.ndarray, **settings: float) -> Detector:
    return Detector(load(yolox(tmp_path / "yolox.onnx", rows)), **settings)


def test_the_letterbox_keeps_proportions_on_grey() -> None:
    image = Image.new("RGB", (128, 64), (255, 0, 0))
    pixels, scale = letterbox(image, SIZE)
    assert (pixels.shape, pixels.dtype, scale) == ((1, 3, SIZE, SIZE), np.float32, 0.5)
    # Red becomes the last channel: the model takes BGR.
    assert pixels[0, :, 0, 0].tolist() == [0, 0, 255]
    assert pixels[0, :, 40, 0].tolist() == [114, 114, 114]
    assert pixels[0, :, 31, 63].tolist() == [0, 0, 255]


def test_a_thin_picture_keeps_at_least_a_pixel() -> None:
    pixels, scale = letterbox(Image.new("RGB", (6400, 10)), SIZE)
    assert scale == 0.01
    assert pixels[0, 0, 0, 0] != 114
    assert pixels[0, 0, 1, 0] == 114


def test_anchors_cover_the_three_grids() -> None:
    cells, strides = anchors(SIZE)
    assert len(cells) == 64 + 16 + 4
    assert cells[9].tolist() == [1, 1]
    assert strides[:, 0].tolist() == [8] * 64 + [16] * 16 + [32] * 4


def test_animals_are_found_in_the_original_picture(tmp_path: Path) -> None:
    rows = rows_for((4, 4, 20, 28, CAT, 0.9), (30, 2, 60, 30, DOG, 0.6))
    found = detector(tmp_path, rows).detect(Image.new("RGB", (128, 64)))
    assert [(d.species, round(d.score, 3)) for d in found] == [("cat", 0.9), ("dog", 0.6)]
    cat = found[0].box
    assert [round(value, 3) for value in (cat.x0, cat.y0, cat.x1, cat.y1)] == [8, 8, 40, 56]


def test_other_classes_and_weak_scores_are_ignored(tmp_path: Path) -> None:
    rows = rows_for((4, 4, 20, 20, PERSON, 0.99), (30, 30, 60, 60, BIRD, 0.34))
    assert detector(tmp_path, rows).detect(Image.new("RGB", (64, 64))) == []
    assert [
        d.species
        for d in detector(tmp_path, rows, min_score=0.3).detect(Image.new("RGB", (64, 64)))
    ] == ["bird"]


def test_the_most_likely_species_wins() -> None:
    rows = rows_for((4, 4, 20, 20, CAT, 0.5))
    index = int(np.flatnonzero(rows[:, 4])[0])
    rows[index, 5 + DOG] = 0.8
    (found,) = decode(rows, anchors(SIZE), 1.0, 0.35, 0.45)
    assert (found.species, round(found.score, 3)) == ("dog", 0.8)


def test_overlapping_boxes_are_one_animal_whatever_the_species() -> None:
    rows = rows_for((8, 8, 40, 40, CAT, 0.9), (9, 9, 41, 41, DOG, 0.7), (44, 44, 60, 60, CAT, 0.5))
    found = decode(rows, anchors(SIZE), 1.0, 0.35, 0.45)
    assert [(d.species, round(d.score, 2)) for d in found] == [("cat", 0.9), ("cat", 0.5)]


def test_boxes_are_clipped_to_the_picture_and_empty_ones_dropped(tmp_path: Path) -> None:
    rows = rows_for((-8, 8, 24, 40, CAT, 0.9))
    outside = rows_for((40, 40, 56, 56, DOG, 0.8))
    rows[np.flatnonzero(outside[:, 4])] = outside[np.flatnonzero(outside[:, 4])]
    # The dog lies in the grey band below a picture half as tall as wide.
    found = detector(tmp_path, rows).detect(Image.new("RGB", (64, 32)))
    assert [(d.species, d.box) for d in found] == [("cat", Box(0, 8, 24, 32))]


def test_wild_outputs_never_give_infinite_or_invalid_boxes(tmp_path: Path) -> None:
    rows = rows_for((8, 8, 40, 40, CAT, 0.9), (44, 44, 60, 60, DOG, 0.95))
    cat, dog = np.flatnonzero(rows[:, 4])
    rows[cat, 2:4] = 1e6
    # The best box is invalid: it must not suppress the others.
    rows[dog, 0] = np.nan
    found = detector(tmp_path, rows).detect(Image.new("RGB", (64, 64)))
    assert [(d.species, d.box) for d in found] == [("cat", Box(0, 0, 64, 64))]


def test_suppression_keeps_the_best_and_distinct_boxes() -> None:
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30], [5, 5, 5, 5]], float)
    scores = np.array([0.5, 0.9, 0.8, 0.95])
    assert suppress(boxes, scores, 0.45) == [3, 1, 2]
    assert suppress(boxes[:1], scores[:1], 0.45) == [0]


@pytest.mark.parametrize(
    "shape",
    [[1, 3, 64], [1, 1, 64, 64], [1, 3, "height", "width"], [1, 3, 64, 32], [1, 3, 48, 48]],
)
def test_a_detector_with_another_input_is_refused(tmp_path: Path, shape: list) -> None:
    graph = helper.make_graph(
        [helper.make_node("Identity", ["images"], ["output"])],
        "other",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, shape)],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, shape)],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    path = tmp_path / "other.onnx"
    path.write_bytes(model.SerializeToString())
    with pytest.raises(ModelError, match="fixed square"):
        Detector(load(path))


@pytest.mark.parametrize(
    "rows", [np.zeros((84, 21)), np.zeros((83, 5 + CLASSES))], ids=["columns", "predictions"]
)
def test_a_detector_with_another_output_is_refused(tmp_path: Path, rows: np.ndarray) -> None:
    with pytest.raises(ModelError, match="unexpected detector output"):
        detector(tmp_path, rows).detect(Image.new("RGB", (64, 64)))


@pytest.mark.parametrize(("min_score", "overlap"), [(0, 0.45), (1.1, 0.45), (0.35, 0)])
def test_settings_stay_in_range(tmp_path: Path, min_score: float, overlap: float) -> None:
    with pytest.raises(ValueError, match="must be in"):
        detector(tmp_path, rows_for(), min_score=min_score, overlap=overlap)


def test_a_missing_or_damaged_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match=r"missing\.onnx"):
        load(tmp_path / "missing.onnx")
    damaged = tmp_path / "damaged.onnx"
    damaged.write_bytes(b"not a model")
    with pytest.raises(ModelError, match=r"damaged\.onnx"):
        load(damaged)


def test_an_animal_must_be_the_most_likely_class() -> None:
    # A toy that looks a little like a dog is not taken for one.
    rows = rows_for((4, 4, 20, 20, DOG, 0.4))
    index = int(np.flatnonzero(rows[:, 4])[0])
    rows[index, 5 + TEDDY_BEAR] = 0.8
    assert decode(rows, anchors(SIZE), 1.0, 0.35, 0.45) == []


def test_only_the_best_candidates_are_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detector_module, "MAX_CANDIDATES", 2)
    rows = rows_for((2, 2, 14, 14, CAT, 0.5), (34, 2, 46, 14, CAT, 0.9), (2, 34, 14, 46, DOG, 0.7))
    found = decode(rows, anchors(SIZE), 1.0, 0.35, 0.45)
    assert [round(d.score, 1) for d in found] == [0.9, 0.7]


def test_huge_values_overflow_without_warnings(tmp_path: Path) -> None:
    rows = rows_for((8, 8, 40, 40, CAT, 0.9), (44, 44, 60, 60, DOG, 0.8))
    rows[np.flatnonzero(rows[:, 4])[1], :4] = 3e38
    found = detector(tmp_path, rows).detect(Image.new("RGB", (64, 64)))
    assert [d.species for d in found] == ["cat"]


def _single_input(path: Path, inputs: list) -> Path:
    graph = helper.make_graph(
        [helper.make_node("Identity", [inputs[0].name], ["output"])],
        "other",
        inputs,
        [
            helper.make_tensor_value_info(
                "output", inputs[0].type.tensor_type.elem_type, [1, 3, 64, 64]
            )
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    path.write_bytes(model.SerializeToString())
    return path


@pytest.mark.parametrize(
    "inputs",
    [
        [helper.make_tensor_value_info("images", TensorProto.FLOAT16, [1, 3, 64, 64])],
        [
            helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 64, 64]),
            helper.make_tensor_value_info("extra", TensorProto.FLOAT, [1]),
        ],
    ],
    ids=["float16", "two inputs"],
)
def test_a_detector_with_other_inputs_is_refused(tmp_path: Path, inputs: list) -> None:
    with pytest.raises(ModelError, match="one float32 picture"):
        Detector(load(_single_input(tmp_path / "other.onnx", inputs)))
