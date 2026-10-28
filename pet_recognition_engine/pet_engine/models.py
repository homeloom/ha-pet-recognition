"""The models the engine runs, pinned by URL, size and SHA-256 in models.json.

They are downloaded once, when the app's image is built, and checked again
each time the engine starts: a model that changed upstream or on disk is
never run.

    python -m pet_engine.models models.json /models
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .sessions import ModelError

ROLES = ("detector", "embedder")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILE = re.compile(r"[A-Za-z0-9_.-]+\.onnx")
_CHUNK = 1024 * 1024

Opener = Callable[[str], IO[bytes]]


@dataclass(frozen=True)
class Model:
    """One pinned model.

    `id` names the model and its version; embeddings are only compared
    between examples of the same embedder id.
    """

    role: str
    id: str
    file: str
    url: str
    size: int
    sha256: str


def read_manifest(path: Path) -> dict[str, Model]:
    """The models of the manifest at `path`, by role."""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        models = {role: Model(role=role, **entries[role]) for role in ROLES}
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise ModelError(f"invalid model manifest {path.name}: {err}") from err
    for model in models.values():
        texts = (model.id, model.file, model.url, model.sha256)
        if (
            not all(isinstance(text, str) for text in texts)
            or not model.id
            or not _FILE.fullmatch(model.file)
            or not model.url.startswith("https://")
            # A boolean is an int to Python, but not a size.
            or type(model.size) is not int
            or model.size <= 0
            or not _SHA256.fullmatch(model.sha256)
        ):
            raise ModelError(f"invalid entry for the {model.role} in {path.name}")
    return models


def verify(model: Model, directory: Path) -> Path:
    """The path of `model` in `directory`, once its size and SHA-256 are checked."""
    path = directory / model.file
    try:
        with path.open("rb") as file:
            size, digest = _hash(file)
    except OSError as err:
        raise ModelError(f"the {model.role} model is missing: {err}") from err
    _check(model, size, digest)
    return path


def download(model: Model, directory: Path, opener: Opener | None = None) -> Path:
    """Download `model` into `directory`, unless a checked copy is there already."""
    path = directory / model.file
    if path.exists():
        return verify(model, directory)
    directory.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    open_url = opener or _open
    try:
        with open_url(model.url) as response, partial.open("wb") as file:
            size, digest = _hash(response, file, limit=model.size)
        _check(model, size, digest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(path)
    return path


def _open(url: str) -> IO[bytes]:
    # The URL comes from the manifest, which only holds https URLs.
    return urllib.request.urlopen(url, timeout=120)  # noqa: S310


def _hash(source: IO[bytes], copy: IO[bytes] | None = None, limit: int = 0) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(_CHUNK):
        size += len(chunk)
        if limit and size > limit:
            raise ModelError(f"the download is larger than the expected {limit} bytes")
        digest.update(chunk)
        if copy is not None:
            copy.write(chunk)
    return size, digest.hexdigest()


def _check(model: Model, size: int, digest: str) -> None:
    if size != model.size or digest != model.sha256:
        raise ModelError(
            f"the {model.role} model does not match its pin: "
            f"{size} bytes and SHA-256 {digest}, expected {model.size} bytes and {model.sha256}"
        )


def main(argv: list[str]) -> int:
    """Download the models of a manifest. Arguments: manifest, directory."""
    if len(argv) != 2:
        print("usage: python -m pet_engine.models MANIFEST DIRECTORY", file=sys.stderr)
        return 2
    manifest, directory = Path(argv[0]), Path(argv[1])
    for model in read_manifest(manifest).values():
        path = download(model, directory)
        print(f"{model.role}: {model.id}, {path.stat().st_size} bytes, checked")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
