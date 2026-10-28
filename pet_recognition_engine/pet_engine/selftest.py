"""Check the real models when the app's image is built.

The sample picture must show a cat, and the embedding of that cat must stay
close to the reference recorded with the same embedding model. A model that
loads but gives wrong results, or a change that breaks the preparation of
the pictures, stops the build instead of reaching users.

    python -m pet_engine.selftest models.json /models selftest/cat.jpg selftest/cat.json

With --record, the reference is written instead of checked, once a new
embedding model is pinned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from .analysis import AnalysisSettings, Animal, analyse
from .detector import Detector
from .embedder import Embedder
from .images import open_image
from .models import read_manifest, verify
from .sessions import ModelError, load

MIN_SIMILARITY = 0.99


def load_engine(manifest: Path, directory: Path) -> tuple[Detector, Embedder]:
    """The detector and the embedder of `manifest`, checked in `directory`."""
    models = read_manifest(manifest)
    detector = Detector(load(verify(models["detector"], directory)))
    embedder = Embedder(load(verify(models["embedder"], directory)), models["embedder"].id)
    return detector, embedder


def find_cat(detector: Detector, embedder: Embedder, sample: bytes) -> Animal:
    """The cat of `sample`, or ModelError when there is none."""
    result = analyse(open_image(sample), detector, embedder, AnalysisSettings())
    cats = [animal for animal in result.animals if animal.species == "cat"]
    if not cats:
        raise ModelError("the self-test found no cat in the sample picture")
    return cats[0]


def record(detector: Detector, embedder: Embedder, sample: bytes) -> dict[str, object]:
    """The reference of `sample`: the embedding model and the cat's embedding."""
    cat = find_cat(detector, embedder, sample)
    return {"model": embedder.model, "embedding": [round(float(x), 6) for x in cat.embedding]}


def check(detector: Detector, embedder: Embedder, sample: bytes, reference: object) -> str:
    """A summary of the check of `sample` against `reference`, or ModelError."""
    if (
        not isinstance(reference, dict)
        or reference.get("model") != embedder.model
        or not isinstance(reference.get("embedding"), list)
    ):
        raise ModelError(f"the self-test has no reference for the model {embedder.model}")
    expected = np.asarray(reference["embedding"], dtype=np.float64)
    cat = find_cat(detector, embedder, sample)
    if cat.embedding.shape != expected.shape:
        raise ModelError(f"the self-test got an embedding of shape {cat.embedding.shape}")
    similarity = float(np.dot(cat.embedding, expected) / np.linalg.norm(expected))
    if not similarity >= MIN_SIMILARITY:
        raise ModelError(f"the self-test embedding differs from its reference ({similarity:.4f})")
    return (
        f"cat found with a score of {cat.score:.2f}, "
        f"embedding of {expected.size} values at {similarity:.4f} of its reference"
    )


def main(argv: list[str]) -> int:
    """Run the self-test. Arguments: [--record] manifest, models, picture, reference."""
    recording = argv[:1] == ["--record"]
    arguments = argv[1:] if recording else argv
    if len(arguments) != 4:
        print(
            "usage: python -m pet_engine.selftest [--record] MANIFEST DIRECTORY PICTURE REFERENCE",
            file=sys.stderr,
        )
        return 2
    manifest, directory, picture, reference = map(Path, arguments)
    detector, embedder = load_engine(manifest, directory)
    sample = picture.read_bytes()
    if recording:
        recorded = record(detector, embedder, sample)
        reference.write_text(json.dumps(recorded, indent=1) + "\n", encoding="utf-8")
        print(f"Self-test: reference of {embedder.model} written to {reference}")
        return 0
    try:
        expected = json.loads(reference.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ModelError(f"the self-test reference cannot be read: {err}") from err
    print(f"Self-test: {check(detector, embedder, sample, expected)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
