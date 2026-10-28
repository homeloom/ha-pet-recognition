"""Answers of a fake engine, each checked against the API's schema first.

The integration's tests use these, so that they never rely on an answer the
real engine could not give.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA = json.loads(
    (Path(__file__).parents[1] / "docs" / "api-v1.schema.json").read_text(encoding="utf-8")
)
MODEL = "test-model@1"
URL = "http://engine.test:8765"
KEY = "k" * 32
CROP = b"\xff\xd8\xff\xe0 a JPEG crop"


def checked(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """`body`, once it is shown to be a valid answer of `kind`."""
    validator = Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{kind}"})
    validator.validate(body)
    return body


def unit(*values: float) -> list[float]:
    """`values` scaled to a length of 1."""
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def info(**changes: object) -> dict[str, Any]:
    """The answer of /v1/info."""
    body = {
        "api": 1,
        "version": "0.1.0",
        "model": MODEL,
        "species": ["bird", "cat", "dog"],
        "max_bytes": 20 * 1024 * 1024,
        "max_pixels": 40_000_000,
    }
    return checked("info", {**body, **changes})


def animal(species: str = "cat", embedding: list[float] | None = None, **changes: object) -> dict:
    """One animal of an analysis."""
    body = {
        "species": species,
        "score": 0.9,
        "box": [10.0, 20.0, 110.0, 220.0],
        "area": 0.25,
        "crop": base64.b64encode(CROP).decode(),
        "embedding": embedding or unit(1, 2, 2),
    }
    return checked("animal", {**body, **changes})


def analysis(*animals: dict[str, Any], **changes: object) -> dict[str, Any]:
    """The answer of /v1/analyze."""
    body = {"width": 400, "height": 300, "model": MODEL, "animals": list(animals)}
    return checked("analysis", {**body, **changes})


def embedding(*values: float, model: str = MODEL) -> dict[str, Any]:
    """The answer of /v1/embed."""
    return checked("embedding", {"model": model, "embedding": unit(*(values or (3, 4)))})


def error(code: str, message: str = "it failed") -> dict[str, Any]:
    """An error of the engine."""
    return checked("error", {"error": {"code": code, "message": message}})
