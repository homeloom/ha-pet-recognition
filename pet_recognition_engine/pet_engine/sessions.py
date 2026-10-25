"""Load ONNX models to run on the CPU."""

from __future__ import annotations

from pathlib import Path

import onnxruntime as ort


class ModelError(RuntimeError):
    """A model is missing, unreadable, or not shaped as the engine expects."""


def load(path: Path) -> ort.InferenceSession:
    """The inference session of the model at `path`."""
    try:
        return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    except Exception as err:
        raise ModelError(f"cannot load {path.name}: {err}") from err
