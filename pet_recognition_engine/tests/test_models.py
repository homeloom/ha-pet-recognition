"""Tests of the pinned models: manifest, download and checks."""

import hashlib
import io
import json
import runpy
import sys
from pathlib import Path

import pytest

from pet_engine import models
from pet_engine.models import Model, download, main, read_manifest, verify
from pet_engine.sessions import ModelError

DATA = b"model bytes" * 1000
REPOSITORY_MANIFEST = Path(__file__).parents[1] / "models.json"


def pinned(data: bytes = DATA, **changes: object) -> Model:
    fields = {
        "role": "detector",
        "id": "test-model",
        "file": "test.onnx",
        "url": "https://example.com/test.onnx",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    fields.update(changes)
    return Model(**fields)


def write_manifest(path: Path, **changes: object) -> Path:
    entry = {key: value for key, value in vars(pinned()).items() if key != "role"}
    entries = {"detector": dict(entry), "embedder": dict(entry, file="other.onnx")}
    entries["embedder"].update(changes)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def opener(data: bytes = DATA) -> models.Opener:
    return lambda _url: io.BytesIO(data)


def test_the_repository_pins_both_models() -> None:
    pins = read_manifest(REPOSITORY_MANIFEST)
    assert sorted(pins) == ["detector", "embedder"]
    assert all(model.url.startswith("https://") for model in pins.values())


def test_a_manifest_gives_both_models(tmp_path: Path) -> None:
    pins = read_manifest(write_manifest(tmp_path / "models.json"))
    assert (pins["detector"].role, pins["embedder"].file) == ("detector", "other.onnx")


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://example.com/test.onnx"},
        {"sha256": "0" * 63},
        {"sha256": "A" * 64},
        {"size": 0},
        {"size": "12"},
        {"file": "../escape.onnx"},
        {"file": "model.bin"},
        {"id": ""},
        {"size": True},
        {"file": 1},
        {"url": None},
        {"id": ["detector"]},
        {"sha256": 0},
        {"unexpected": 1},
    ],
)
def test_invalid_entries_are_refused(tmp_path: Path, changes: dict[str, object]) -> None:
    with pytest.raises(ModelError, match="invalid"):
        read_manifest(write_manifest(tmp_path / "models.json", **changes))


def test_a_manifest_must_name_both_roles(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text('{"detector": {}}', encoding="utf-8")
    with pytest.raises(ModelError, match="invalid model manifest"):
        read_manifest(path)
    with pytest.raises(ModelError, match="invalid model manifest"):
        read_manifest(tmp_path / "missing.json")


def test_a_download_is_checked_then_kept(tmp_path: Path) -> None:
    path = download(pinned(), tmp_path / "models", opener())
    assert path.read_bytes() == DATA
    # Already there and checked: nothing is downloaded again.
    assert download(pinned(), tmp_path / "models", opener(b"")) == path
    assert verify(pinned(), tmp_path / "models") == path


@pytest.mark.parametrize(
    ("served", "message"),
    [
        (DATA[:-1], "does not match"),
        (DATA[:-1] + b"x", "does not match"),
        (DATA + b"x", "larger than"),
    ],
)
def test_a_download_that_differs_is_refused(tmp_path: Path, served: bytes, message: str) -> None:
    with pytest.raises(ModelError, match=message):
        download(pinned(), tmp_path, opener(served))
    assert list(tmp_path.iterdir()) == []


def test_a_model_changed_on_disk_or_missing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match="missing"):
        verify(pinned(), tmp_path)
    (tmp_path / "test.onnx").write_bytes(DATA[::-1])
    with pytest.raises(ModelError, match="does not match"):
        verify(pinned(), tmp_path)
    with pytest.raises(ModelError, match="does not match"):
        download(pinned(), tmp_path, opener())


def test_the_command_downloads_every_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "_open", opener())
    manifest = write_manifest(tmp_path / "models.json")
    assert main([str(manifest), str(tmp_path / "models")]) == 0
    assert sorted(path.name for path in (tmp_path / "models").iterdir()) == [
        "other.onnx",
        "test.onnx",
    ]
    assert "detector: test-model" in capsys.readouterr().out
    assert main([]) == 2


# runpy warns that the module was imported before, by the tests themselves.
@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_the_command_runs_as_a_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["models"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("pet_engine.models", run_name="__main__")
    assert exit_info.value.code == 2


def test_downloads_use_https_with_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        models.urllib.request, "urlopen", lambda url, timeout: calls.append((url, timeout))
    )
    models._open("https://example.com/model.onnx")
    assert calls == [("https://example.com/model.onnx", 120)]
