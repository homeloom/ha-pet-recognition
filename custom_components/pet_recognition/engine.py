"""Talk to the Pet Recognition Engine, and check everything it answers.

The engine is described in docs/api.md. Its answers are checked here before
the rest of the integration sees them, so a wrong engine, or a wrong address
that answers something else, gives an EngineResponseError instead of bad
data further on. Unknown keys are ignored, as the API only grows by
additions.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
from yarl import URL

API = 1
SPECIES = frozenset({"bird", "cat", "dog"})
# Analysing a picture takes seconds on small hardware; the rest is quick.
ANALYSIS_TIMEOUT = aiohttp.ClientTimeout(total=60)
TIMEOUT = aiohttp.ClientTimeout(total=10)
# Embeddings have a length of 1, up to the rounding of the engine.
NORM_TOLERANCE = 0.01
BOX_TOLERANCE = 0.5
# Twenty animals with their crops and embeddings take a few megabytes.
MAX_ANSWER = 32 * 1024 * 1024


class EngineError(Exception):
    """The engine could not give an answer."""


class EngineConnectionError(EngineError):
    """The engine cannot be reached, or failed."""


class EngineAuthError(EngineError):
    """The engine refused the API key."""


class EngineBusyError(EngineError):
    """The engine is analysing another picture."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"the engine is busy; retry after {retry_after:g} s")
        self.retry_after = retry_after


class EngineRejectedError(EngineError):
    """The engine refused the request, such as a picture it cannot read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class EngineResponseError(EngineError):
    """The answer is not one of a Pet Recognition Engine of version 1."""


@dataclass(frozen=True)
class EngineInfo:
    """What the engine is and accepts."""

    version: str
    model: str
    species: frozenset[str]
    max_bytes: int
    max_pixels: int


@dataclass(frozen=True)
class Animal:
    """An animal found in a picture."""

    species: str
    score: float
    box: tuple[float, float, float, float]
    area: float
    crop: bytes
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class Analysis:
    """The animals of a picture, the most certain first."""

    width: int
    height: int
    model: str
    animals: tuple[Animal, ...]


@dataclass(frozen=True)
class Embedding:
    """The embedding of a whole picture."""

    model: str
    vector: tuple[float, ...]


def engine_url(host: str, port: int) -> str:
    """The URL of an engine on the Supervisor's network."""
    return str(URL.build(scheme="http", host=host, port=port))


class EngineClient:
    """A client of one engine, on Home Assistant's shared session."""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        self._session = session
        self._url = URL(url)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    @property
    def url(self) -> str:
        """The engine's URL."""
        return str(self._url)

    async def info(self) -> EngineInfo:
        """What the engine is, checked to be an engine of version 1."""
        body = await self._request("GET", "v1/info", timeout=TIMEOUT)
        if _get(body, "api", int) != API:
            raise EngineResponseError(f"the engine speaks API {body.get('api')!r}, not {API}")
        # Species added by a newer engine are left out, as the API grows by additions.
        species = (
            frozenset(name for name in _get(body, "species", list) if isinstance(name, str))
            & SPECIES
        )
        if not species:
            raise EngineResponseError("the engine gave no species this integration knows")
        return EngineInfo(
            version=_text(body, "version"),
            model=_text(body, "model"),
            species=species,
            max_bytes=_positive(body, "max_bytes"),
            max_pixels=_positive(body, "max_pixels"),
        )

    async def analyze(
        self,
        picture: bytes,
        *,
        min_area: float | None = None,
        limit: int | None = None,
        species: frozenset[str] | None = None,
    ) -> Analysis:
        """The animals of `picture`, of the species this integration knows."""
        params: dict[str, str] = {}
        if min_area is not None:
            params["min_area"] = repr(float(min_area))
        if limit is not None:
            params["limit"] = str(int(limit))
        if species is not None:
            params["species"] = ",".join(sorted(species))
        body = await self._request(
            "POST", "v1/analyze", data=picture, params=params, timeout=ANALYSIS_TIMEOUT
        )
        width, height = _positive(body, "width"), _positive(body, "height")
        animals = tuple(
            animal
            for value in _get(body, "animals", list)
            if (animal := _animal(value, width, height)) is not None
        )
        if len({len(animal.embedding) for animal in animals}) > 1:
            raise EngineResponseError("the embeddings of one picture differ in length")
        return Analysis(width, height, _text(body, "model"), animals)

    async def embed(self, picture: bytes) -> Embedding:
        """The embedding of `picture`, taken as a whole."""
        body = await self._request("POST", "v1/embed", data=picture, timeout=ANALYSIS_TIMEOUT)
        return Embedding(model=_text(body, "model"), vector=_embedding(body))

    async def _request(
        self, method: str, path: str, *, timeout: aiohttp.ClientTimeout, **kwargs: object
    ) -> Mapping[str, Any]:
        try:
            async with self._session.request(
                method,
                self._url / path,
                headers=self._headers,
                timeout=timeout,
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status != 200:
                    raise await _failure(response)
                body = _decode(await _read(response))
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EngineConnectionError(f"cannot reach the engine: {err}") from err
        except ValueError as err:
            # aiohttp refuses some requests before sending them, such as a URL
            # with credentials next to the key.
            raise EngineConnectionError(f"cannot send to the engine: {err}") from err
        if not isinstance(body, dict):
            raise EngineResponseError("the engine did not answer a JSON object")
        return body


async def _read(response: aiohttp.ClientResponse) -> bytes:
    """The body of `response`, up to MAX_ANSWER bytes."""
    data = bytearray()
    async for chunk in response.content.iter_chunked(2**16):
        data += chunk
        if len(data) > MAX_ANSWER:
            raise EngineResponseError("the engine's answer is too large")
    return bytes(data)


def _decode(data: bytes) -> object:
    """The JSON value of `data`, without the NaN and Infinity JSON does not have."""

    def constant(name: str) -> object:
        raise ValueError(f"{name} is not JSON")

    try:
        return json.loads(data, parse_constant=constant)
    except (ValueError, RecursionError) as err:
        raise EngineResponseError(f"the engine did not answer JSON: {err}") from err


async def _failure(response: aiohttp.ClientResponse) -> EngineError:
    """The error for an answer other than 200."""
    if response.status == 401:
        return EngineAuthError("the engine refused the API key")
    try:
        error = _decode(await _read(response))["error"]
        code, message = str(error["code"]), str(error["message"])
    except (EngineResponseError, KeyError, TypeError):
        code, message = "", ""
    if response.status == 503 and code == "busy":
        return EngineBusyError(_retry_after(response.headers.get("Retry-After")))
    if 400 <= response.status < 500 and code:
        return EngineRejectedError(code, message)
    if response.status >= 500:
        return EngineConnectionError(f"the engine failed with status {response.status}")
    return EngineResponseError(f"the engine answered status {response.status}")


def _retry_after(value: str | None) -> float:
    try:
        seconds = float(value or 1)
    except ValueError:
        return 1.0
    return min(max(seconds, 0.1), 60.0) if math.isfinite(seconds) else 1.0


def _get(body: Mapping[str, Any], key: str, kind: type) -> Any:  # noqa: ANN401
    value = body.get(key)
    # A boolean is an int to Python, but never a valid count.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise EngineResponseError(f"the engine gave an invalid {key!r}")
    return value


def _text(body: Mapping[str, Any], key: str) -> str:
    value = _get(body, key, str)
    if not value:
        raise EngineResponseError(f"the engine gave an empty {key!r}")
    return value


def _positive(body: Mapping[str, Any], key: str) -> int:
    value = _get(body, key, int)
    if value < 1:
        raise EngineResponseError(f"the engine gave an invalid {key!r}")
    return value


def _number(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EngineResponseError("the engine gave a value that is not a number")
    try:
        number = float(value)
    except OverflowError as err:
        raise EngineResponseError("the engine gave a number too large") from err
    if not low <= number <= high:
        raise EngineResponseError(f"the engine gave {number}, outside {low} to {high}")
    return number


def _embedding(body: Mapping[str, Any]) -> tuple[float, ...]:
    values = _get(body, "embedding", list)
    vector = tuple(_number(value, -1.0, 1.0) for value in values)
    if not vector or abs(math.fsum(x * x for x in vector) - 1) > NORM_TOLERANCE:
        raise EngineResponseError("the engine gave an embedding whose length is not 1")
    return vector


def _animal(value: object, width: int, height: int) -> Animal | None:
    """The animal of `value`, or None for a species added by a newer engine."""
    if not isinstance(value, dict):
        raise EngineResponseError("the engine gave an animal that is not an object")
    species = _get(value, "species", str)
    if species not in SPECIES:
        return None
    box = _get(value, "box", list)
    if len(box) != 4:
        raise EngineResponseError("the engine gave a box without four values")
    # The engine rounds the box to a tenth of a pixel.
    x0, x1 = (_number(side, 0.0, width + BOX_TOLERANCE) for side in box[::2])
    y0, y1 = (_number(side, 0.0, height + BOX_TOLERANCE) for side in box[1::2])
    if x0 > x1 or y0 > y1:
        raise EngineResponseError("the engine gave a box whose corners are reversed")
    try:
        crop = base64.b64decode(_text(value, "crop"), validate=True)
    except binascii.Error as err:
        raise EngineResponseError("the engine gave a crop that is not base64") from err
    return Animal(
        species=species,
        score=_number(value.get("score"), 0.0, 1.0),
        box=(x0, y0, x1, y1),
        area=_number(value.get("area"), 0.0, 1.0),
        crop=crop,
        embedding=_embedding(value),
    )
