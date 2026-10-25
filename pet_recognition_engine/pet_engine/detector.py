"""Find animals in a picture with YOLOX, a detector trained on COCO.

The picture is scaled to fit the model's square input, keeping its
proportions, on a grey background of 114 (a letterbox). YOLOX takes BGR
values from 0 to 255, without normalisation. For each cell of its three grids
(strides 8, 16 and 32) it gives a box, an objectness and the probability of
each of the 80 COCO classes; only the birds, cats and dogs are kept.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import onnxruntime as ort
from PIL import Image

from .images import Box
from .sessions import ModelError

# The COCO classes of the animals the engine looks for.
SPECIES = {14: "bird", 15: "cat", 16: "dog"}
STRIDES = (8, 16, 32)
# The most candidates kept for the suppression of overlapping boxes.
MAX_CANDIDATES = 300
GREY = 114.0
# Box, objectness, then one probability per class up to the last species.
_COLUMNS = 5 + max(SPECIES) + 1


@dataclass(frozen=True)
class Detection:
    """An animal found in a picture."""

    box: Box
    species: str
    score: float


class Detector:
    """YOLOX on its ONNX session."""

    def __init__(
        self, session: ort.InferenceSession, *, min_score: float = 0.35, overlap: float = 0.45
    ) -> None:
        if not 0 < min_score <= 1 or not 0 < overlap <= 1:
            raise ValueError("the minimum score and the overlap must be in ]0, 1]")
        inputs = session.get_inputs()
        if len(inputs) != 1 or inputs[0].type != "tensor(float)":
            raise ModelError("the detector must take one float32 picture")
        model_input = inputs[0]
        shape = model_input.shape
        if (
            len(shape) != 4
            or shape[1] != 3
            or not isinstance(shape[2], int)
            or shape[2] != shape[3]
            or shape[2] % max(STRIDES)
        ):
            raise ModelError(f"the detector input must be a fixed square picture, not {shape}")
        self.session = session
        self.input = model_input.name
        self.size: int = shape[2]
        self.min_score = min_score
        self.overlap = overlap
        self._anchors = anchors(self.size)

    def detect(self, image: Image.Image) -> list[Detection]:
        """The animals in `image`, most certain first, with boxes inside the picture."""
        pixels, scale = letterbox(image, self.size)
        output = np.asarray(self.session.run(None, {self.input: pixels})[0])
        if (
            output.ndim != 3
            or output.shape[1] != len(self._anchors[0])
            or output.shape[2] < _COLUMNS
        ):
            raise ModelError(f"unexpected detector output {output.shape}")
        found = []
        for detection in decode(output[0], self._anchors, scale, self.min_score, self.overlap):
            box = detection.box.clipped(image.width, image.height)
            if box.area > 0:
                found.append(Detection(box, detection.species, detection.score))
        return found


def letterbox(image: Image.Image, size: int) -> tuple[np.ndarray, float]:
    """`image` fitted into a `size` square from its top left corner, and its scale.

    The array is the model input: one picture, BGR channels first, float32.
    """
    scale = min(size / image.width, size / image.height)
    width = max(1, min(size, round(image.width * scale)))
    height = max(1, min(size, round(image.height * scale)))
    resized = image.resize((width, height), Image.Resampling.BILINEAR)
    canvas = np.full((size, size, 3), GREY, dtype=np.float32)
    canvas[:height, :width] = np.asarray(resized, dtype=np.float32)[:, :, ::-1]
    return canvas.transpose(2, 0, 1)[np.newaxis].copy(), scale


def anchors(size: int) -> tuple[np.ndarray, np.ndarray]:
    """The cell of each prediction, as (column, row), and its stride."""
    cells, strides = [], []
    for stride in STRIDES:
        count = size // stride
        columns, rows = np.meshgrid(np.arange(count), np.arange(count))
        cells.append(np.stack((columns, rows), axis=2).reshape(-1, 2))
        strides.append(np.full((count * count, 1), stride))
    return (
        np.concatenate(cells).astype(np.float32),
        np.concatenate(strides).astype(np.float32),
    )


def decode(
    output: np.ndarray,
    cells: tuple[np.ndarray, np.ndarray],
    scale: float,
    min_score: float,
    overlap: float,
) -> list[Detection]:
    """The animals of one YOLOX output, in the pixels of the original picture.

    An animal's score is its objectness times the probability of its most
    likely species, and that species must be the most likely of all classes,
    so that a toy or a person is not taken for a pet. Overlapping boxes are
    merged whatever their species, as one animal cannot be two.
    """
    grid, strides = cells
    classes = np.array(list(SPECIES))
    probabilities = output[:, 5:]
    animals = probabilities[:, classes]
    best = animals.argmax(axis=1)
    probability = animals[np.arange(len(output)), best]
    # A broken model may give anything: what overflows or is not a number is
    # dropped below, without warnings.
    with np.errstate(over="ignore", invalid="ignore"):
        scores = output[:, 4] * probability
        keep = np.flatnonzero((scores >= min_score) & (probability >= probabilities.max(axis=1)))
        # Suppression costs the square of the candidates: keep the best ones.
        if len(keep) > MAX_CANDIDATES:
            keep = keep[np.argsort(-scores[keep], kind="stable")[:MAX_CANDIDATES]]
        centres = (output[keep, :2] + grid[keep]) * strides[keep]
        sizes = np.exp(np.minimum(output[keep, 2:4], 20.0)) * strides[keep]
        boxes = np.concatenate((centres - sizes / 2, centres + sizes / 2), axis=1) / scale
    # A box with an invalid value would suppress every other one.
    finite = np.isfinite(boxes).all(axis=1)
    keep, boxes = keep[finite], boxes[finite]
    return [
        Detection(
            Box(*(float(value) for value in boxes[index])),
            SPECIES[int(classes[best[keep[index]]])],
            float(scores[keep[index]]),
        )
        for index in suppress(boxes, scores[keep], overlap)
    ]


def suppress(boxes: np.ndarray, scores: np.ndarray, overlap: float) -> list[int]:
    """Non-maximum suppression: the boxes to keep, best first.

    A box is dropped when it overlaps a better one by more than `overlap`
    (intersection over union).
    """
    with np.errstate(over="ignore", invalid="ignore"):
        areas = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
        order = list(np.argsort(-scores, kind="stable"))
        kept = []
        while order:
            best, rest = order[0], np.array(order[1:], dtype=int)
            kept.append(int(best))
            top_left = np.maximum(boxes[best, :2], boxes[rest, :2])
            bottom_right = np.minimum(boxes[best, 2:], boxes[rest, 2:])
            inter = np.prod(np.maximum(0, bottom_right - top_left), axis=1)
            union = areas[best] + areas[rest] - inter
            iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
            order = [int(index) for index in rest[iou <= overlap]]
    return kept
