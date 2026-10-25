"""Test helpers: small ONNX models named and shaped like the real ones, and pictures.

They are generated for each test, never committed: the real models are only
downloaded when the app is built.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from PIL import Image

from pet_engine.detector import anchors

# A small detector input keeps the tests fast: 84 predictions instead of 8400.
SIZE = 64
CLASSES = 80
DIMENSIONS = 8


def _save(graph: onnx.GraphProto, path: Path) -> Path:
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, path)
    return path


def yolox(path: Path, rows: np.ndarray, size: int = SIZE) -> Path:
    """A YOLOX graph that gives `rows` whatever the picture.

    The picture still flows through the graph, multiplied by zero, so that the
    input is checked like the real one.
    """
    raw = numpy_helper.from_array(rows[np.newaxis].astype(np.float32), "raw")
    zero = numpy_helper.from_array(np.zeros((1, 1, 1), np.float32), "zero")
    nodes = [
        helper.make_node("ReduceMean", ["images"], ["mean"], keepdims=0),
        helper.make_node("Mul", ["mean", "zero"], ["nothing"]),
        helper.make_node("Add", ["raw", "nothing"], ["output"]),
    ]
    graph = helper.make_graph(
        nodes,
        "yolox",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, size, size])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, *rows.shape])],
        [raw, zero],
    )
    return _save(graph, path)


def rows_for(*boxes: tuple[float, float, float, float, int, float], size: int = SIZE) -> np.ndarray:
    """YOLOX rows that decode to `boxes`: (x0, y0, x1, y1, class, score).

    Boxes are in the letterboxed input. Each is given to the cell of the
    finest grid that holds its centre, with full objectness.
    """
    grid, strides = anchors(size)
    rows = np.zeros((len(grid), 5 + CLASSES), np.float32)
    used: set[int] = set()
    for x0, y0, x1, y1, category, score in boxes:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        index = next(
            i
            for i in range(len(grid))
            if i not in used
            and int(cx // strides[i, 0]) == grid[i, 0]
            and int(cy // strides[i, 0]) == grid[i, 1]
        )
        used.add(index)
        stride = strides[index, 0]
        rows[index, 0] = cx / stride - grid[index, 0]
        rows[index, 1] = cy / stride - grid[index, 1]
        rows[index, 2] = np.log((x1 - x0) / stride)
        rows[index, 3] = np.log((y1 - y0) / stride)
        rows[index, 4] = 1.0
        rows[index, 5 + category] = score
    return rows


def dinov2(path: Path, *, pooled: bool = True, weights: np.ndarray | None = None) -> Path:
    """A DINOv2 graph whose embedding is a linear map of the picture's mean colour.

    It names its outputs like the real export: last_hidden_state, then
    pooler_output unless `pooled` is false.
    """
    if weights is None:
        weights = np.random.default_rng(7).normal(size=(3, DIMENSIONS))
    w = numpy_helper.from_array(weights.astype(np.float32), "weights")
    axes = numpy_helper.from_array(np.array([1], np.int64), "axes")
    nodes = [
        helper.make_node("GlobalAveragePool", ["pixel_values"], ["pooled_pixels"]),
        helper.make_node("Flatten", ["pooled_pixels"], ["colour"]),
        helper.make_node("MatMul", ["colour", "weights"], ["pooler_output"]),
        helper.make_node("Unsqueeze", ["pooler_output", "axes"], ["last_hidden_state"]),
    ]
    outputs = [
        helper.make_tensor_value_info(
            "last_hidden_state", TensorProto.FLOAT, ["batch_size", 1, DIMENSIONS]
        )
    ]
    if pooled:
        outputs.append(
            helper.make_tensor_value_info(
                "pooler_output", TensorProto.FLOAT, ["batch_size", DIMENSIONS]
            )
        )
    graph = helper.make_graph(
        nodes,
        "dinov2",
        [
            helper.make_tensor_value_info(
                "pixel_values", TensorProto.FLOAT, ["batch_size", "num_channels", "height", "width"]
            )
        ],
        outputs,
        [w, axes],
    )
    return _save(graph, path)


def picture(
    width: int = 320,
    height: int = 240,
    colour: tuple[int, int, int] = (40, 120, 200),
    image_format: str = "JPEG",
    **save: object,
) -> bytes:
    """Picture data of one colour."""
    output = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(output, format=image_format, **save)
    return output.getvalue()
