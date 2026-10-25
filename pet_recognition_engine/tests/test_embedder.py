"""Tests of embedding animals with DINOv2."""

from pathlib import Path

import numpy as np
import pytest
from helpers import DIMENSIONS, dinov2
from onnx import TensorProto, helper
from PIL import Image

from pet_engine.embedder import MEAN, SIZE, Embedder, normalise, preprocess
from pet_engine.sessions import ModelError, load


def embedder(path: Path, **options: object) -> Embedder:
    return Embedder(load(dinov2(path, **options)), "test-model")


def test_pictures_are_resized_and_normalised() -> None:
    pixels = preprocess(Image.new("RGB", (50, 300), (124, 116, 104)))
    assert (pixels.shape, pixels.dtype) == ((1, 3, SIZE, SIZE), np.float32)
    # The ImageNet mean colour becomes zero.
    assert np.allclose(pixels, 0, atol=0.01)
    assert MEAN.dtype == np.float32


@pytest.mark.parametrize("pooled", [True, False])
def test_an_embedding_has_a_length_of_one(tmp_path: Path, pooled: bool) -> None:
    model = embedder(tmp_path / "dinov2.onnx", pooled=pooled)
    vector = model.embed(Image.new("RGB", (80, 60), (200, 30, 30)))
    assert (vector.shape, vector.dtype, model.model) == ((DIMENSIONS,), np.float32, "test-model")
    assert np.isclose(np.linalg.norm(vector), 1)


def test_the_pooled_output_and_the_class_token_agree(tmp_path: Path) -> None:
    image = Image.new("RGB", (80, 60), (20, 160, 90))
    pooled = embedder(tmp_path / "pooled.onnx").embed(image)
    token = embedder(tmp_path / "token.onnx", pooled=False).embed(image)
    assert np.allclose(pooled, token)


def test_different_pictures_give_different_embeddings(tmp_path: Path) -> None:
    model = embedder(tmp_path / "dinov2.onnx")
    red = model.embed(Image.new("RGB", (32, 32), (220, 20, 20)))
    blue = model.embed(Image.new("RGB", (32, 32), (20, 20, 220)))
    assert float(red @ blue) < 0.99


def test_an_empty_embedding_is_refused(tmp_path: Path) -> None:
    model = embedder(tmp_path / "zero.onnx", weights=np.zeros((3, DIMENSIONS)))
    with pytest.raises(ModelError, match="empty or invalid"):
        model.embed(Image.new("RGB", (32, 32)))


@pytest.mark.parametrize("vector", [[0.0, 0.0], [np.nan, 1.0], [np.inf, 1.0]])
def test_invalid_vectors_cannot_be_normalised(vector: list[float]) -> None:
    with pytest.raises(ModelError):
        normalise(np.array(vector))


def _model(path: Path, input_shape: list, output_shape: list) -> Path:
    graph = helper.make_graph(
        [helper.make_node("Identity", ["pixel_values"], ["features"])],
        "other",
        [helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, input_shape)],
        [helper.make_tensor_value_info("features", TensorProto.FLOAT, output_shape)],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    path.write_bytes(model.SerializeToString())
    return path


@pytest.mark.parametrize(
    "shape", [[1, 3, 224], [1, 1, 224, 224], [1, 3, 256, 256], [1, 3, "h", 112]]
)
def test_an_embedder_with_another_input_is_refused(tmp_path: Path, shape: list) -> None:
    with pytest.raises(ModelError, match="224 pixel"):
        Embedder(load(_model(tmp_path / "other.onnx", shape, shape)), "other")


def test_an_embedder_with_another_output_is_refused(tmp_path: Path) -> None:
    # Its only output is the picture itself: four dimensions, not three.
    shape = ["batch", 3, "height", "width"]
    model = Embedder(load(_model(tmp_path / "other.onnx", shape, shape)), "other")
    with pytest.raises(ModelError, match="unexpected embedder output"):
        model.embed(Image.new("RGB", (32, 32)))


def test_a_pooled_output_of_another_shape_is_refused(tmp_path: Path) -> None:
    graph = helper.make_graph(
        [helper.make_node("Identity", ["pixel_values"], ["pooler_output"])],
        "other",
        [helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, ["b", 3, "h", "w"])],
        [helper.make_tensor_value_info("pooler_output", TensorProto.FLOAT, ["b", 3, "h", "w"])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    path = tmp_path / "other.onnx"
    path.write_bytes(model.SerializeToString())
    with pytest.raises(ModelError, match="unexpected embedder output"):
        Embedder(load(path), "other").embed(Image.new("RGB", (32, 32)))


@pytest.mark.parametrize(
    "inputs",
    [
        [helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT16, [1, 3, 224, 224])],
        [
            helper.make_tensor_value_info("pixel_values", TensorProto.FLOAT, [1, 3, 224, 224]),
            helper.make_tensor_value_info("extra", TensorProto.FLOAT, [1]),
        ],
    ],
    ids=["float16", "two inputs"],
)
def test_an_embedder_with_other_inputs_is_refused(tmp_path: Path, inputs: list) -> None:
    graph = helper.make_graph(
        [helper.make_node("Identity", ["pixel_values"], ["pooler_output"])],
        "other",
        inputs,
        [
            helper.make_tensor_value_info(
                "pooler_output", inputs[0].type.tensor_type.elem_type, [1, 3, 224, 224]
            )
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    path = tmp_path / "other.onnx"
    path.write_bytes(model.SerializeToString())
    with pytest.raises(ModelError, match="one float32 picture"):
        Embedder(load(path), "other")
