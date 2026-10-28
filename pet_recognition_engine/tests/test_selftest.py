"""Tests of the self-test run when the app's image is built."""

import hashlib
import json
import runpy
import sys
from pathlib import Path

import numpy as np
import pytest
from helpers import DIMENSIONS, dinov2, picture, rows_for, yolox

from pet_engine.images import open_image
from pet_engine.models import read_manifest
from pet_engine.selftest import check, load_engine, main, record
from pet_engine.sessions import ModelError

CAT, DOG = 15, 16
SAMPLE = Path(__file__).parents[1] / "selftest" / "cat.jpg"
REFERENCE = SAMPLE.with_suffix(".json")


def manifest(tmp_path: Path, *boxes: tuple[float, float, float, float, int, float]) -> Path:
    files = {
        "detector": yolox(tmp_path / "yolox.onnx", rows_for(*boxes)),
        "embedder": dinov2(tmp_path / "dinov2.onnx"),
    }
    entries = {
        role: {
            "id": f"test-{role}",
            "file": path.name,
            "url": f"https://example.com/{path.name}",
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for role, path in files.items()
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_the_sample_is_a_picture_the_engine_reads() -> None:
    assert SAMPLE.stat().st_size < 50_000
    image = open_image(SAMPLE.read_bytes())
    assert min(image.size) >= 224


def test_the_repository_has_a_reference_for_the_pinned_embedder() -> None:
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    pinned = read_manifest(Path(__file__).parents[1] / "models.json")["embedder"]
    assert reference["model"] == pinned.id
    embedding = np.asarray(reference["embedding"])
    assert embedding.shape == (384,)
    assert np.isclose(np.linalg.norm(embedding), 1, atol=1e-4)


def recorded(tmp_path: Path, path: Path) -> dict[str, object]:
    detector, embedder = load_engine(path, tmp_path)
    return record(detector, embedder, picture(64, 64))


def test_a_cat_close_to_its_reference_passes(tmp_path: Path) -> None:
    path = manifest(tmp_path, (8, 8, 56, 56, CAT, 0.9))
    reference = recorded(tmp_path, path)
    assert reference["model"] == "test-embedder"
    assert len(reference["embedding"]) == DIMENSIONS
    detector, embedder = load_engine(path, tmp_path)
    summary = check(detector, embedder, picture(64, 64), reference)
    assert summary == (
        f"cat found with a score of 0.90, embedding of {DIMENSIONS} values at 1.0000 of its"
        " reference"
    )


def test_no_cat_fails(tmp_path: Path) -> None:
    reference = recorded(tmp_path, manifest(tmp_path, (8, 8, 56, 56, CAT, 0.9)))
    detector, embedder = load_engine(manifest(tmp_path, (8, 8, 56, 56, DOG, 0.9)), tmp_path)
    with pytest.raises(ModelError, match="no cat"):
        check(detector, embedder, picture(64, 64), reference)
    with pytest.raises(ModelError, match="no cat"):
        record(detector, embedder, picture(64, 64))


@pytest.mark.parametrize(
    "reference",
    [
        None,
        [],
        {"model": "another-embedder", "embedding": [1.0]},
        {"model": "test-embedder"},
        {"model": "test-embedder", "embedding": "1"},
    ],
)
def test_a_missing_or_foreign_reference_fails(tmp_path: Path, reference: object) -> None:
    detector, embedder = load_engine(manifest(tmp_path, (8, 8, 56, 56, CAT, 0.9)), tmp_path)
    with pytest.raises(ModelError, match="no reference"):
        check(detector, embedder, picture(64, 64), reference)


def test_an_embedding_of_another_size_fails(tmp_path: Path) -> None:
    path = manifest(tmp_path, (8, 8, 56, 56, CAT, 0.9))
    detector, embedder = load_engine(path, tmp_path)
    reference = {"model": "test-embedder", "embedding": [1.0] * 384}
    with pytest.raises(ModelError, match="shape"):
        check(detector, embedder, picture(64, 64), reference)


def test_an_embedding_far_from_its_reference_fails(tmp_path: Path) -> None:
    path = manifest(tmp_path, (8, 8, 56, 56, CAT, 0.9))
    reference = recorded(tmp_path, path)
    detector, embedder = load_engine(path, tmp_path)
    opposite = {**reference, "embedding": [-x for x in reference["embedding"]]}
    with pytest.raises(ModelError, match="differs from its reference"):
        check(detector, embedder, picture(64, 64), opposite)
    unusable = {**reference, "embedding": [float("nan")] * DIMENSIONS}
    with pytest.raises(ModelError, match="differs from its reference"):
        check(detector, embedder, picture(64, 64), unusable)


def test_the_command_records_then_checks_the_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = manifest(tmp_path, (8, 8, 56, 56, CAT, 0.9))
    sample = tmp_path / "sample.png"
    sample.write_bytes(picture(64, 64, image_format="PNG"))
    reference = tmp_path / "sample.json"
    arguments = [str(path), str(tmp_path), str(sample), str(reference)]
    assert main(["--record", *arguments]) == 0
    assert capsys.readouterr().out.startswith("Self-test: reference of test-embedder")
    assert json.loads(reference.read_text())["model"] == "test-embedder"
    assert main(arguments) == 0
    assert capsys.readouterr().out.startswith("Self-test: cat found")
    assert main([str(path)]) == 2
    assert main(["--record", *arguments[:3]]) == 2


@pytest.mark.parametrize("content", [None, "not json"])
def test_an_unreadable_reference_fails(tmp_path: Path, content: str | None) -> None:
    path = manifest(tmp_path, (8, 8, 56, 56, CAT, 0.9))
    sample = tmp_path / "sample.png"
    sample.write_bytes(picture(64, 64, image_format="PNG"))
    reference = tmp_path / "sample.json"
    if content is not None:
        reference.write_text(content)
    with pytest.raises(ModelError, match="reference cannot be read"):
        main([str(path), str(tmp_path), str(sample), str(reference)])


# runpy warns that the module was imported before, by the tests themselves.
@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_the_command_runs_as_a_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["selftest"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("pet_engine.selftest", run_name="__main__")
    assert exit_info.value.code == 2
