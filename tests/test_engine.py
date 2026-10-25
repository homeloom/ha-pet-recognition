"""Tests of the engine client and of the checks of its answers."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition import engine
from custom_components.pet_recognition.engine import (
    Analysis,
    EngineAuthError,
    EngineBusyError,
    EngineClient,
    EngineConnectionError,
    EngineIncompatibleError,
    EngineRejectedError,
    EngineResponseError,
    engine_url,
)

from .answers import CROP, KEY, MODEL, URL, analysis, animal, embedding, error, info, unit


@pytest.fixture
def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> EngineClient:
    return EngineClient(async_get_clientsession(hass), URL, KEY)


async def test_info_gives_what_the_engine_is(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", json=info(future_key="ignored"))
    found = await client.info()
    assert (found.version, found.model, found.species) == (
        "0.1.0",
        MODEL,
        frozenset({"bird", "cat", "dog"}),
    )
    assert (found.max_bytes, found.max_pixels) == (20 * 1024 * 1024, 40_000_000)
    ((method, url, _data, headers),) = aioclient_mock.mock_calls
    assert (method, str(url), headers) == (
        "GET",
        f"{URL}/v1/info",
        {"Authorization": f"Bearer {KEY}"},
    )
    assert client.url == URL


async def test_an_engine_behind_a_path_is_reached_there(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get("https://proxy.test/engine/v1/info", json=info())
    client = EngineClient(async_get_clientsession(hass), "https://proxy.test/engine", KEY)
    assert (await client.info()).model == MODEL


@pytest.mark.parametrize(
    "body",
    [
        {**info(), "api": 2},
        {**info(), "api": True},
        {**info(), "species": []},
        {**info(), "species": ["horse"]},
        {**info(), "species": [{}, ["cat"]]},
        {**info(), "species": "cat"},
        {**info(), "version": ""},
        {**info(), "model": 3},
        {**info(), "max_bytes": 0},
        {**info(), "max_pixels": True},
        {key: value for key, value in info().items() if key != "model"},
        [info()],
    ],
)
async def test_info_that_is_not_of_an_engine_is_refused(
    client: EngineClient, aioclient_mock: AiohttpClientMocker, body: object
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", json=body)
    with pytest.raises(EngineResponseError):
        await client.info()


async def test_an_engine_without_this_api_is_told_apart(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    # An engine that dropped version 1 answers that /v1/info is not found.
    aioclient_mock.get(f"{URL}/v1/info", status=404, json=error("not_found", "Not Found"))
    with pytest.raises(EngineIncompatibleError, match="does not serve API 1"):
        await client.info()


async def test_another_refusal_of_info_stays_a_refusal(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", status=405, json=error("method_not_allowed"))
    with pytest.raises(EngineRejectedError) as raised:
        await client.info()
    assert not isinstance(raised.value, EngineIncompatibleError)


async def test_an_answer_that_is_not_json_is_refused(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", text="<html>a router's page</html>")
    with pytest.raises(EngineResponseError, match="JSON"):
        await client.info()


async def test_the_analysis_gives_the_animals(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{URL}/v1/analyze", json=analysis(animal(), animal("dog", unit(0, 1, 0))))
    result = await client.analyze(
        b"picture", min_area=0.05, limit=3, species=frozenset({"dog", "cat"})
    )
    assert isinstance(result, Analysis)
    assert (result.width, result.height, result.model) == (400, 300, MODEL)
    cat, dog = result.animals
    assert (cat.species, cat.score, cat.box, cat.area, cat.crop) == (
        "cat",
        0.9,
        (10.0, 20.0, 110.0, 220.0),
        0.25,
        CROP,
    )
    assert cat.embedding == tuple(unit(1, 2, 2))
    assert dog.embedding == (0.0, 1.0, 0.0)
    ((_method, url, data, _headers),) = aioclient_mock.mock_calls
    assert data == b"picture"
    assert dict(url.query) == {"min_area": "0.05", "limit": "3", "species": "cat,dog"}


async def test_the_analysis_sends_no_parameter_unless_asked(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{URL}/v1/analyze", json=analysis())
    assert (await client.analyze(b"picture")).animals == ()
    ((_method, url, _data, _headers),) = aioclient_mock.mock_calls
    assert not url.query


@pytest.mark.parametrize(
    "changes",
    [
        {"species": None},
        {"score": 1.5},
        {"score": "0.9"},
        {"score": True},
        {"box": [1.0, 2.0, 3.0]},
        {"box": [-1.0, 2.0, 3.0, 4.0]},
        {"box": [10.0, 20.0, 401.0, 220.0]},
        {"box": [10.0, 20.0, 110.0, 301.0]},
        {"box": [110.0, 20.0, 10.0, 220.0]},
        {"box": [10.0, 220.0, 110.0, 20.0]},
        {"box": "1,2,3,4"},
        {"area": -0.1},
        {"crop": ""},
        {"crop": "not base64!"},
        {"embedding": []},
        {"embedding": [0.5, 0.5]},
        {"embedding": [2.0]},
        {"embedding": ["1"]},
    ],
)
async def test_an_animal_that_cannot_be_used_is_refused(
    client: EngineClient, aioclient_mock: AiohttpClientMocker, changes: dict[str, Any]
) -> None:
    aioclient_mock.post(f"{URL}/v1/analyze", json={**analysis(), "animals": [animal() | changes]})
    with pytest.raises(EngineResponseError):
        await client.analyze(b"picture")


@pytest.mark.parametrize(
    "body",
    [
        {**analysis(), "animals": "none"},
        {**analysis(), "animals": ["cat"]},
        {**analysis(), "width": 0},
        analysis(animal(), animal(embedding=unit(1, 1))),
    ],
)
async def test_an_analysis_that_cannot_be_used_is_refused(
    client: EngineClient, aioclient_mock: AiohttpClientMocker, body: dict[str, Any]
) -> None:
    aioclient_mock.post(f"{URL}/v1/analyze", json=body)
    with pytest.raises(EngineResponseError):
        await client.analyze(b"picture")


async def test_a_picture_is_embedded_whole(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(3, 4))
    result = await client.embed(b"crop")
    assert (result.model, result.vector) == (MODEL, (0.6, 0.8))
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/embed", json={"model": MODEL, "embedding": [0.6]})
    with pytest.raises(EngineResponseError, match="length"):
        await client.embed(b"crop")


async def test_a_refused_key_is_an_authentication_error(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", status=401, json=error("unauthorized"))
    with pytest.raises(EngineAuthError):
        await client.info()


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("2", 2.0), (None, 1.0), ("soon", 1.0), ("0", 0.1), ("3600", 60.0), ("nan", 1.0)],
)
async def test_a_busy_engine_says_when_to_retry(
    client: EngineClient,
    aioclient_mock: AiohttpClientMocker,
    retry_after: str | None,
    expected: float,
) -> None:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    aioclient_mock.post(f"{URL}/v1/analyze", status=503, json=error("busy"), headers=headers)
    with pytest.raises(EngineBusyError) as raised:
        await client.analyze(b"picture")
    assert raised.value.retry_after == expected


async def test_a_refused_picture_gives_its_code(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{URL}/v1/analyze", status=422, json=error("unreadable_image", "damaged"))
    with pytest.raises(EngineRejectedError, match="unreadable_image: damaged") as raised:
        await client.analyze(b"picture")
    assert raised.value.code == "unreadable_image"


@pytest.mark.parametrize(
    ("status", "kwargs", "expected"),
    [
        (500, {"json": error("internal_error")}, EngineConnectionError),
        (502, {"text": "Bad Gateway"}, EngineConnectionError),
        (503, {"text": "Service Unavailable"}, EngineConnectionError),
        (404, {"text": "Not Found"}, EngineResponseError),
        (302, {"text": ""}, EngineResponseError),
        (400, {"json": {"error": "no code"}}, EngineResponseError),
    ],
)
async def test_other_failures_are_told_apart(
    client: EngineClient,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    kwargs: dict[str, Any],
    expected: type[Exception],
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", status=status, **kwargs)
    with pytest.raises(expected):
        await client.info()


@pytest.mark.parametrize("exc", [ClientError("refused"), TimeoutError()])
async def test_an_unreachable_engine_is_a_connection_error(
    client: EngineClient, aioclient_mock: AiohttpClientMocker, exc: Exception
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", exc=exc)
    with pytest.raises(EngineConnectionError):
        await client.info()


def test_the_url_of_an_app_is_built_from_its_host_and_port() -> None:
    assert engine_url("abc123-pet-recognition-engine", 8765) == (
        "http://abc123-pet-recognition-engine:8765"
    )


async def test_species_of_a_newer_engine_are_left_out(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", json={**info(), "species": ["cat", "horse", 3]})
    assert (await client.info()).species == frozenset({"cat"})
    horse = animal() | {"species": "horse"}
    aioclient_mock.post(f"{URL}/v1/analyze", json={**analysis(), "animals": [horse, animal()]})
    (cat,) = (await client.analyze(b"picture")).animals
    assert cat.species == "cat"


async def test_a_box_on_the_edge_of_the_picture_is_kept(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    edge = animal(box=[0.0, 0.0, 400.4, 300.0])
    aioclient_mock.post(f"{URL}/v1/analyze", json=analysis(edge))
    (box_animal,) = (await client.analyze(b"picture")).animals
    assert box_animal.box == (0.0, 0.0, 400.4, 300.0)


@pytest.mark.parametrize(
    "text",
    [
        '{"animals": [], "width": Infinity, "height": 1, "model": "m"}',
        '{"embedding": [NaN], "model": "m"}',
        '{"embedding": [1' + "0" * 400 + '], "model": "m"}',
        "[" * 100_000 + "]" * 100_000,
    ],
)
async def test_numbers_json_does_not_have_are_refused(
    client: EngineClient, aioclient_mock: AiohttpClientMocker, text: str
) -> None:
    aioclient_mock.post(f"{URL}/v1/embed", text=text)
    with pytest.raises(EngineResponseError):
        await client.embed(b"crop")


async def test_an_answer_too_large_is_refused(
    client: EngineClient, aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "MAX_ANSWER", 100)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    with pytest.raises(EngineResponseError, match="too large"):
        await client.info()
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/analyze", status=422, text=" " * 200)
    with pytest.raises(EngineResponseError, match="status 422"):
        await client.analyze(b"picture")


async def test_a_request_aiohttp_refuses_is_a_connection_error(
    client: EngineClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", exc=ValueError("credentials and an Authorization header"))
    with pytest.raises(EngineConnectionError, match="cannot send"):
        await client.info()
