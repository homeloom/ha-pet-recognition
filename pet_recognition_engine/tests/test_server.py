"""Tests of the engine's HTTP API, checked against its schema."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from helpers import dinov2, picture, rows_for, yolox
from jsonschema import Draft202012Validator
from PIL import Image

from pet_engine import server
from pet_engine.analysis import Analysis
from pet_engine.detector import Detector
from pet_engine.embedder import Embedder
from pet_engine.server import Engine, create_app
from pet_engine.sessions import load

SCHEMA = json.loads(
    (Path(__file__).parents[2] / "docs" / "api-v1.schema.json").read_text(encoding="utf-8")
)
KEY = "k" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}
CAT, DOG = 15, 16

Client = Callable[..., Awaitable[TestClient]]


def check(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """`body`, after checking it against the definition `name` of the schema."""
    Draft202012Validator(
        {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{name}"}
    ).validate(body)
    return body


def engine(tmp_path: Path, *boxes: tuple[float, float, float, float, int, float]) -> Engine:
    return Engine(
        "1.2.3",
        Detector(load(yolox(tmp_path / "yolox.onnx", rows_for(*boxes)))),
        Embedder(load(dinov2(tmp_path / "dinov2.onnx")), "test-model"),
    )


@pytest.fixture
async def client(aiohttp_client: Client, tmp_path: Path) -> TestClient:
    return await aiohttp_client(
        create_app(engine(tmp_path, (2, 2, 30, 30, CAT, 0.9), (34, 34, 62, 62, DOG, 0.8)), KEY)
    )


def square() -> bytes:
    return picture(64, 64, (90, 90, 90), image_format="PNG")


async def test_health_needs_no_key(client: TestClient) -> None:
    response = await client.get("/health")
    assert response.status == 200
    check("health", await response.json())


async def test_info_gives_the_versions_and_limits(client: TestClient) -> None:
    response = await client.get("/v1/info", headers=AUTH)
    info = check("info", await response.json())
    assert info == {
        "api": 1,
        "version": "1.2.3",
        "model": "test-model",
        "species": ["bird", "cat", "dog"],
        "max_bytes": server.MAX_BYTES,
        "max_pixels": 40_000_000,
    }


async def test_analyze_returns_each_animal(client: TestClient) -> None:
    response = await client.post("/v1/analyze", data=square(), headers=AUTH)
    assert response.status == 200
    result = check("analysis", await response.json())
    assert (result["width"], result["height"], result["model"]) == (64, 64, "test-model")
    cat, dog = result["animals"]
    assert (cat["species"], cat["score"], cat["box"]) == ("cat", 0.9, [2.0, 2.0, 30.0, 30.0])
    assert dog["species"] == "dog"
    assert round(cat["area"], 4) == round(28 * 28 / 64**2, 4)
    assert Image.open(io.BytesIO(base64.b64decode(cat["crop"]))).format == "JPEG"
    assert np.isclose(np.linalg.norm(cat["embedding"]), 1, atol=1e-5)


async def test_analyze_takes_its_settings_from_the_query(client: TestClient) -> None:
    for query, species in [
        ({"species": "dog"}, ["dog"]),
        ({"limit": "1"}, ["cat"]),
        ({"min_area": "0.5"}, []),
        ({"species": "bird,cat", "limit": "20", "min_area": "0"}, ["cat"]),
    ]:
        response = await client.post("/v1/analyze", data=square(), headers=AUTH, params=query)
        result = check("analysis", await response.json())
        assert [animal["species"] for animal in result["animals"]] == species, query


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "0"},
        {"limit": "21"},
        {"limit": "two"},
        {"min_area": "1"},
        {"min_area": "nan"},
        {"species": "horse"},
        {"species": ""},
        {"colour": "red"},
    ],
)
async def test_bad_settings_are_refused(client: TestClient, query: dict[str, str]) -> None:
    response = await client.post("/v1/analyze", data=square(), headers=AUTH, params=query)
    assert response.status == 400
    assert check("error", await response.json())["error"]["code"] == "invalid_parameter"


async def test_embed_embeds_the_whole_picture(client: TestClient) -> None:
    response = await client.post("/v1/embed", data=picture(), headers=AUTH)
    body = check("embedding", await response.json())
    assert body["model"] == "test-model"
    assert np.isclose(np.linalg.norm(body["embedding"]), 1, atol=1e-5)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": KEY},
        {"Authorization": f"Basic {KEY}"},
        {"Authorization": f"Bearer {KEY}x"},
        {"Authorization": f"Bearer  {KEY}"},
    ],
)
async def test_a_wrong_key_is_refused(client: TestClient, headers: dict[str, str]) -> None:
    for method, path in [("GET", "/v1/info"), ("POST", "/v1/analyze"), ("POST", "/v1/embed")]:
        response = await client.request(method, path, headers=headers, data=square())
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert check("error", await response.json())["error"]["code"] == "unauthorized"


async def test_the_scheme_of_the_key_ignores_case(client: TestClient) -> None:
    response = await client.get("/v1/info", headers={"Authorization": f"bearer {KEY}"})
    assert response.status == 200


@pytest.mark.parametrize(
    ("data", "status", "code"),
    [
        (b"", 400, "invalid_request"),
        (b"not a picture", 422, "unreadable_image"),
        (picture(image_format="GIF"), 422, "unreadable_image"),
    ],
)
async def test_bad_pictures_are_refused(
    client: TestClient, data: bytes, status: int, code: str
) -> None:
    for path in ("/v1/analyze", "/v1/embed"):
        response = await client.post(path, data=data, headers=AUTH)
        assert response.status == status
        assert check("error", await response.json())["error"]["code"] == code


async def test_a_body_too_large_is_refused(aiohttp_client: Client, tmp_path: Path) -> None:
    client = await aiohttp_client(create_app(engine(tmp_path), KEY, max_bytes=1000))
    response = await client.post("/v1/analyze", data=b"x" * 1001, headers=AUTH)
    assert response.status == 413
    assert check("error", await response.json())["error"]["code"] == "too_large"
    info = await (await client.get("/v1/info", headers=AUTH)).json()
    assert info["max_bytes"] == 1000


async def test_unknown_routes_and_methods_are_json(client: TestClient) -> None:
    response = await client.get("/v1/missing", headers=AUTH)
    assert response.status == 404
    assert check("error", await response.json())["error"]["code"] == "not_found"
    response = await client.get("/v1/analyze", headers=AUTH)
    assert response.status == 405
    assert response.headers["Allow"] == "POST"
    assert check("error", await response.json())["error"]["code"] == "method_not_allowed"


async def test_one_picture_is_analysed_at_a_time(
    aiohttp_client: Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()
    real = server.analyse

    def slow(*args: object) -> Analysis:
        started.set()
        release.wait(5)
        return real(*args)

    monkeypatch.setattr(server, "analyse", slow)
    client = await aiohttp_client(create_app(engine(tmp_path), KEY))
    first = asyncio.create_task(client.post("/v1/analyze", data=square(), headers=AUTH))
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    # The server still answers while it works.
    assert (await client.get("/health")).status == 200
    busy = await client.post("/v1/embed", data=square(), headers=AUTH)
    assert busy.status == 503
    assert busy.headers["Retry-After"] == "1"
    assert check("error", await busy.json())["error"]["code"] == "busy"
    # Refused before its body is read: an empty one would be a 400.
    for path in ("/v1/analyze", "/v1/embed"):
        assert (await client.post(path, data=b"", headers=AUTH)).status == 503
    release.set()
    assert (await first).status == 200
    assert (await client.post("/v1/embed", data=square(), headers=AUTH)).status == 200


async def test_a_failure_is_an_internal_error_without_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: object) -> Analysis:
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "analyse", broken)
    response = await client.post("/v1/analyze", data=square(), headers=AUTH)
    assert response.status == 500
    body = check("error", await response.json())
    assert body["error"] == {
        "code": "internal_error",
        "message": "The engine failed on this request.",
    }
    assert KEY not in caplog.text
    assert "/v1/analyze" in caplog.text


def test_a_short_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 16"):
        create_app(engine(tmp_path), "short")


async def test_redirects_are_not_turned_into_errors(aiohttp_client: Client, tmp_path: Path) -> None:
    app = create_app(engine(tmp_path), KEY)

    async def moved(_request: web.Request) -> web.Response:
        raise web.HTTPFound("/health")

    app.router.add_get("/moved", moved)
    client = await aiohttp_client(app)
    response = await client.get("/moved", headers=AUTH, allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/health"
