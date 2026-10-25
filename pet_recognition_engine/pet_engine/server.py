"""Serve the engine over HTTP: version 1 of its API, described in docs/api.md.

Every route but /health needs the API key as a bearer token. Pictures are
sent as the raw body of the request and never stored. One picture is
analysed at a time, in a worker thread so that /health keeps answering; a
request that comes while the engine is busy gets 503 with Retry-After, and
the integration sends it again. Errors are JSON, with a stable code.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
from aiohttp import web

from .analysis import Analysis, AnalysisSettings, analyse
from .detector import SPECIES, Detector
from .embedder import Embedder
from .images import MAX_PIXELS, UnreadableImageError, open_image

API = 1
MAX_BYTES = 20 * 1024 * 1024
# The most animals a client may ask for in one picture.
MAX_LIMIT = 20
MIN_KEY_LENGTH = 16
PUBLIC = frozenset({"/health"})

_LOGGER = logging.getLogger(__name__)
_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    404: "not_found",
    405: "method_not_allowed",
    413: "too_large",
    503: "busy",
}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass(frozen=True)
class Engine:
    """The models the server runs, and the version of the app."""

    version: str
    detector: Detector
    embedder: Embedder


class ApiError(Exception):
    """An error answered as JSON, with its code."""

    def __init__(
        self, status: int, code: str, message: str, headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


ENGINE = web.AppKey("engine", Engine)
KEY = web.AppKey("key", bytes)
MAX = web.AppKey("max_bytes", int)
LOCK = web.AppKey("lock", asyncio.Lock)


def create_app(engine: Engine, api_key: str, *, max_bytes: int = MAX_BYTES) -> web.Application:
    """The web application that serves `engine`, protected by `api_key`."""
    if len(api_key) < MIN_KEY_LENGTH:
        raise ValueError(f"the API key needs at least {MIN_KEY_LENGTH} characters")
    app = web.Application(client_max_size=max_bytes, middlewares=[_errors, _authenticate])
    app[ENGINE] = engine
    app[KEY] = api_key.encode()
    app[MAX] = max_bytes
    app[LOCK] = asyncio.Lock()
    app.router.add_get("/health", _health)
    app.router.add_get("/v1/info", _info)
    app.router.add_post("/v1/analyze", _analyze)
    app.router.add_post("/v1/embed", _embed)
    return app


@web.middleware
async def _errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as err:
        return _error(err.status, err.code, err.message, err.headers)
    except web.HTTPException as err:
        if err.status < 400:
            raise
        code = _CODES.get(err.status, "invalid_request")
        return _error(err.status, code, err.reason, err.headers)
    except Exception:
        # Never the picture nor the key: only what failed.
        _LOGGER.exception("Request to %s failed", request.path)
        return _error(500, "internal_error", "The engine failed on this request.")


@web.middleware
async def _authenticate(request: web.Request, handler: Handler) -> web.StreamResponse:
    if request.path not in PUBLIC:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode(), request.app[KEY]
        ):
            raise ApiError(
                401, "unauthorized", "Missing or wrong API key.", {"WWW-Authenticate": "Bearer"}
            )
    return await handler(request)


def _error(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> web.Response:
    kept = {
        name: value
        for name, value in (headers or {}).items()
        if name in ("Retry-After", "WWW-Authenticate", "Allow")
    }
    return web.json_response(
        {"error": {"code": code, "message": message}}, status=status, headers=kept
    )


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _info(request: web.Request) -> web.Response:
    engine = request.app[ENGINE]
    return web.json_response(
        {
            "api": API,
            "version": engine.version,
            "model": engine.embedder.model,
            "species": sorted(SPECIES.values()),
            "max_bytes": request.app[MAX],
            "max_pixels": MAX_PIXELS,
        }
    )


async def _analyze(request: web.Request) -> web.Response:
    settings = _settings(request.query)
    data = await _picture(request)
    engine = request.app[ENGINE]
    result = await _run(
        request, lambda: analyse(open_image(data), engine.detector, engine.embedder, settings)
    )
    return web.json_response(_analysis(result, engine.embedder.model))


async def _embed(request: web.Request) -> web.Response:
    data = await _picture(request)
    embedder = request.app[ENGINE].embedder
    vector = await _run(request, lambda: embedder.embed(open_image(data)))
    return web.json_response({"model": embedder.model, "embedding": _floats(vector)})


async def _picture(request: web.Request) -> bytes:
    data = await request.read()
    if not data:
        raise ApiError(400, "invalid_request", "The body must be a picture.")
    return data


def _settings(query: Mapping[str, str]) -> AnalysisSettings:
    unknown = set(query) - {"min_area", "limit", "species"}
    if unknown:
        raise ApiError(400, "invalid_parameter", f"Unknown parameter: {sorted(unknown)[0]}.")
    options: dict[str, Any] = {}
    try:
        if "min_area" in query:
            options["min_area"] = float(query["min_area"])
        if "limit" in query:
            options["limit"] = int(query["limit"])
            if options["limit"] > MAX_LIMIT:
                raise ValueError(f"the limit must be at most {MAX_LIMIT}")
        if "species" in query:
            options["species"] = frozenset(query["species"].split(","))
        return AnalysisSettings(**options)
    except ValueError as err:
        raise ApiError(400, "invalid_parameter", str(err)) from err


async def _run[T](request: web.Request, work: Callable[[], T]) -> T:
    lock = request.app[LOCK]
    if lock.locked():
        raise ApiError(503, "busy", "The engine is busy.", {"Retry-After": "1"})
    async with lock:
        try:
            return await asyncio.get_running_loop().run_in_executor(None, work)
        except UnreadableImageError as err:
            raise ApiError(422, "unreadable_image", str(err)) from err


def _analysis(result: Analysis, model: str) -> dict[str, Any]:
    return {
        "width": result.width,
        "height": result.height,
        "model": model,
        "animals": [
            {
                "species": animal.species,
                "score": round(animal.score, 4),
                "box": [
                    round(value, 1)
                    for value in (animal.box.x0, animal.box.y0, animal.box.x1, animal.box.y1)
                ],
                "area": round(animal.area, 6),
                "crop": base64.b64encode(animal.crop).decode(),
                "embedding": _floats(animal.embedding),
            }
            for animal in result.animals
        ],
    }


def _floats(vector: np.ndarray) -> list[float]:
    return [round(float(value), 6) for value in vector]
