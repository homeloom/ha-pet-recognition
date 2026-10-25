"""Turn the picture of an animal into an embedding with DINOv2.

An embedding is a list of numbers that stays close for pictures of the same
animal. The crop is resized to the model's 224 pixel square, without keeping
its proportions so that the whole animal stays in, and normalised with the
ImageNet mean and deviation. The embedding is the model's pooled output, or
else its class token, scaled to a length of 1.
"""

from __future__ import annotations

import numpy as np
import onnxruntime as ort
from PIL import Image

from .sessions import ModelError

SIZE = 224
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
DEVIATION = np.array([0.229, 0.224, 0.225], dtype=np.float32)
POOLED = "pooler_output"


class Embedder:
    """DINOv2 on its ONNX session.

    `model` names the model and its version. Embeddings of different models
    cannot be compared, so the integration keeps it with every example.
    """

    def __init__(self, session: ort.InferenceSession, model: str) -> None:
        inputs = session.get_inputs()
        if len(inputs) != 1 or inputs[0].type != "tensor(float)":
            raise ModelError("the embedder must take one float32 picture")
        model_input = inputs[0]
        shape = model_input.shape
        # Exports often leave the sizes free, named instead of numbered.
        expected = (None, 3, SIZE, SIZE)
        if len(shape) != 4 or any(
            isinstance(side, int) and want is not None and side != want
            for side, want in zip(shape, expected, strict=True)
        ):
            raise ModelError(f"the embedder input must take {SIZE} pixel pictures, not {shape}")
        outputs = [output.name for output in session.get_outputs()]
        self.session = session
        self.input = model_input.name
        self.output = POOLED if POOLED in outputs else outputs[0]
        self.model = model

    def embed(self, image: Image.Image) -> np.ndarray:
        """The embedding of `image`, as float32 of length 1."""
        (output,) = self.session.run([self.output], {self.input: preprocess(image)})
        output = np.asarray(output, dtype=np.float32)
        if self.output != POOLED:
            if output.ndim != 3:
                raise ModelError(f"unexpected embedder output {output.shape}")
            output = output[:, 0]
        if output.ndim != 2 or output.shape[0] != 1:
            raise ModelError(f"unexpected embedder output {output.shape}")
        return normalise(output[0])


def preprocess(image: Image.Image) -> np.ndarray:
    """The model input for `image`: one RGB picture, channels first, normalised."""
    resized = image.resize((SIZE, SIZE), Image.Resampling.BICUBIC)
    pixels = np.asarray(resized, dtype=np.float32) / 255.0
    return ((pixels - MEAN) / DEVIATION).transpose(2, 0, 1)[np.newaxis].astype(np.float32)


def normalise(vector: np.ndarray) -> np.ndarray:
    """`vector` scaled to a length of 1."""
    length = float(np.linalg.norm(vector))
    if not np.isfinite(length) or length == 0:
        raise ModelError("the embedder gave an empty or invalid embedding")
    return (vector / length).astype(np.float32)
