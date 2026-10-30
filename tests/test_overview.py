"""Tests of the websocket commands of the dashboards: `pet_recognition/overview`,
`pet_recognition/assign`, and the crops of the visits they link to."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from http import HTTPStatus
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.examples import Origin

from .answers import URL, animal, embedding
from .asking import PET_C, UNSURE, B, Phones, asking, visit
from .test_correcting import SELECT, device, learned
from .test_counts import come
from .watching import FEEDER, PET_A, PET_B, START, Shown, journal, pet, settle, source, source_id

Ask = Callable[..., Awaitable[dict[str, Any]]]


def subentry_id(entry: MockConfigEntry, key: str) -> str:
    """The subentry ID of the pet of `key`."""
    (found,) = (
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.unique_id == key
    )
    return found


def at(minutes: float) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
async def ask(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> Ask:
    """Send a command as an administrator, and return its answer.

    The websocket opens at the first command, once the integration is set up
    and the time frozen.
    """
    clients: list[MockHAClientWebSocket] = []

    async def command(name: str, **data: object) -> dict[str, Any]:
        if not clients:
            clients.append(await user_client(hass, hass_ws_client, "system-admin"))
        return await send(clients[0], name, **data)

    return command


async def send(client: MockHAClientWebSocket, command: str, **data: object) -> dict[str, Any]:
    await client.send_json_auto_id({"type": f"pet_recognition/{command}", **data})
    return await client.receive_json()


async def user_client(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, group: str
) -> MockHAClientWebSocket:
    """A websocket of a new user of `group`, with a token of the time frozen."""
    user = await hass.auth.async_create_user("Phone", group_ids=[group])
    token = await hass.auth.async_create_refresh_token(user, "https://example.com/app")
    return await hass_ws_client(hass, hass.auth.async_create_access_token(token))


async def three_visits(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    **options: object,
) -> MockConfigEntry:
    """An unsure visit at 1 minute, Pet B at 20, and a picture without an
    animal at 40, still going on."""
    entry = await asking(
        hass,
        aioclient_mock,
        shown,
        freezer,
        phones,
        pet(PET_A),
        pet(PET_B),
        pet(PET_C, species="dog"),
        source(FEEDER, pets=[PET_B, PET_C]),
        phone_ids=[],
        **options,
    )
    await visit(hass, aioclient_mock, shown, freezer, 1)
    await visit(hass, aioclient_mock, shown, freezer, 20, embedding=B)
    await come(hass, aioclient_mock, shown, freezer, 40)
    return entry


async def test_the_overview(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    ask: Ask,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    feeder = source_id(entry)
    unsure, of_b, going_on = journal(entry).of(feeder)[-3:]
    answer = await ask("overview")
    assert answer["success"]
    result = answer["result"]
    pets = {key: subentry_id(entry, key) for key in (PET_A, PET_B, PET_C)}
    assert result["pets"] == [
        {
            "id": pets[PET_A],
            "name": "Pet a",
            "species": "cat",
            "device_id": device(hass, entry, PET_A),
        },
        {
            "id": pets[PET_B],
            "name": "Pet b",
            "species": "cat",
            "device_id": device(hass, entry, PET_B),
        },
        {
            "id": pets[PET_C],
            "name": "Pet c",
            "species": "dog",
            "device_id": device(hass, entry, PET_C),
        },
    ]
    assert result["sources"] == [
        {
            "id": feeder,
            "name": "Feeder",
            "kind": "feeder",
            "pets": [pets[PET_B], pets[PET_C]],
            "device_id": device(hass, entry, feeder),
            "visit_entity": "event.feeder_visit",
        }
    ]
    visits = result["visits"]
    pictures = [found.pop("picture") for found in visits]
    assert visits == [
        {
            "id": unsure.id,
            "source": feeder,
            "start": at(1),
            "end": at(1),
            "ended": True,
            "pets": [],
            "other_animal": False,
            "unsure": True,
        },
        {
            "id": of_b.id,
            "source": feeder,
            "start": at(20),
            "end": at(20),
            "ended": True,
            "pets": [pets[PET_B]],
            "other_animal": False,
            "unsure": False,
        },
        {
            "id": going_on.id,
            "source": feeder,
            "start": at(40),
            "end": at(40),
            "ended": False,
            "pets": [],
            "other_animal": False,
            "unsure": True,
        },
    ]

    # The crops of the visits, whether a question showed them or not, are
    # served without a header; a picture without an animal, which the
    # feeder keeps, is served itself.
    client = await hass_client_no_auth()
    assert pictures[2].startswith(
        f"/api/pet_recognition/frame/{entry.entry_id}/{going_on.id}?authSig="
    )
    response = await client.get(pictures[2])
    assert response.status == HTTPStatus.OK
    assert await response.read() == shown[FEEDER]
    for picture, record in zip(pictures[:2], (unsure, of_b), strict=True):
        assert picture.startswith(
            f"/api/pet_recognition/picture/{entry.entry_id}/{record.detections[0].crop}?authSig="
        )
        response = await client.get(picture)
        assert response.status == HTTPStatus.OK
        assert response.headers["Cache-Control"] == "private, no-store"
    unsigned = pictures[1].split("?")[0]
    assert (await client.get(unsigned)).status == HTTPStatus.UNAUTHORIZED
    journal(entry).crop_path(of_b.detections[0].crop).unlink()
    assert (await client.get(pictures[1])).status == HTTPStatus.NOT_FOUND


async def test_the_crop_of_the_largest_animal_stands_for_a_visit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    ask: Ask,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await come(hass, aioclient_mock, shown, freezer, 1, animal(area=0.1), animal(area=0.4))
    (record,) = journal(entry).of(source_id(entry))
    largest = max(record.detections, key=lambda detection: detection.area)
    assert largest.area == 0.4
    (found,) = (await ask("overview"))["result"]["visits"]
    assert f"/{largest.crop}?authSig=" in found["picture"]


async def test_the_period_of_the_overview(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    ask: Ask,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones, retention=2)
    unsure, of_b, going_on = journal(entry).of(source_id(entry))[-3:]

    async def visits(**period: object) -> list[str]:
        answer = await ask("overview", **period)
        assert answer["success"]
        return [found["id"] for found in answer["result"]["visits"]]

    # A visit that goes on at some time of the period, the ends included.
    assert await visits(start=at(10), end=at(30)) == [of_b.id]
    assert await visits(start=at(20)) == [of_b.id, going_on.id]
    assert await visits(end=at(20)) == [unsure.id, of_b.id]
    assert await visits(start=at(2), end=at(19)) == []
    # A local time is the time it says.
    local = dt_util.as_local(START + timedelta(minutes=20)).replace(tzinfo=None)
    assert await visits(start=local.isoformat(), end=at(20)) == [of_b.id]

    # Yesterday and today by default.
    freezer.move_to(START + timedelta(days=1))
    assert await visits() == [unsure.id, of_b.id, going_on.id]
    freezer.move_to(START + timedelta(days=2))
    assert await visits() == []
    # Never before the days the pictures are kept, even when they still are.
    assert await visits(start=at(-60)) == [unsure.id, of_b.id, going_on.id]
    freezer.move_to(START + timedelta(days=2, minutes=10))
    assert await visits(start=at(-60)) == [of_b.id, going_on.id]

    # An entry that is not loaded shows nothing.
    await hass.config_entries.async_unload(entry.entry_id)
    answer = await ask("overview")
    assert answer["result"] == {"pets": [], "sources": [], "visits": []}

    answer = await ask("overview", start="yesterday")
    assert (answer["success"], answer["error"]["code"]) == (False, "invalid_format")


async def test_the_overview_is_for_every_user(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await three_visits(hass, aioclient_mock, shown, freezer, phones)
    client = await user_client(hass, hass_ws_client, "system-read-only")
    answer = await send(client, "overview")
    assert answer["success"]
    picture = answer["result"]["visits"][0]["picture"]
    assert (await (await hass_client_no_auth()).get(picture)).status == HTTPStatus.OK


async def test_assign(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_ws_client: WebSocketGenerator,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    unsure, of_b, going_on = journal(entry).of(source_id(entry))[-3:]
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*UNSURE))
    pets = {key: subentry_id(entry, key) for key in (PET_A, PET_B, PET_C)}
    client = await user_client(hass, hass_ws_client, "system-users")

    # A user who controls the source, to a pet not expected there.
    answer = await send(client, "assign", visit_id=unsure.id, pets=[pets[PET_A]])
    assert answer["success"]
    visit_now = answer["result"]["visit"]
    assert visit_now.pop("picture").startswith("/api/pet_recognition/picture/")
    assert visit_now == {
        "id": unsure.id,
        "source": source_id(entry),
        "start": at(1),
        "end": at(1),
        "ended": True,
        "pets": [pets[PET_A]],
        "other_animal": False,
        "unsure": False,
    }
    await settle(hass)
    assert (unsure.pets, unsure.corrected) == ((PET_A,), True)
    assert learned(entry) == [(PET_A, Origin.CORRECTION)]

    # Several pets and the other animal, to a visit going on.
    answer = await send(
        client, "assign", visit_id=going_on.id, pets=[pets[PET_C]], other_animal=True
    )
    visit_now = answer["result"]["visit"]
    assert (visit_now["pets"], visit_now["other_animal"]) == ([pets[PET_C]], True)
    assert visit_now["ended"] is False
    assert visit_now["picture"].startswith(f"/api/pet_recognition/frame/{entry.entry_id}/")
    await settle(hass)
    assert going_on.pets == (PET_C, OTHER_ANIMAL)

    # Nobody: unknown, and no longer unsure.
    answer = await send(client, "assign", visit_id=of_b.id)
    visit_now = answer["result"]["visit"]
    assert (visit_now["pets"], visit_now["unsure"]) == ([], False)
    await settle(hass)
    assert (of_b.pets, of_b.corrected) == ((), True)


@pytest.mark.parametrize(
    ("data", "translation_key"),
    [
        ({"visit_id": "f" * 32}, "unknown_visit"),
        ({"pets": ["not a pet"]}, "unknown_pet_id"),
        ({"pets": ["source"]}, "unknown_pet_id"),
    ],
)
async def test_assign_refuses_what_it_cannot_find(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    ask: Ask,
    data: dict[str, Any],
    translation_key: str,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    unsure = journal(entry).of(source_id(entry))[-3]
    if data.get("pets") == ["source"]:
        data = {"pets": [source_id(entry)]}
    answer = await ask("assign", **{"visit_id": unsure.id, **data})
    assert not answer["success"]
    error = answer["error"]
    assert (error["code"], error["translation_key"]) == ("home_assistant_error", translation_key)
    assert not unsure.corrected
    answer = await ask("assign", pets=[])
    assert answer["error"]["code"] == "invalid_format"


async def test_assign_is_for_who_controls_the_source(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_ws_client: WebSocketGenerator,
    ask: Ask,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    unsure = journal(entry).of(source_id(entry))[-3]
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*UNSURE))
    read_only = await user_client(hass, hass_ws_client, "system-read-only")
    answer = await send(read_only, "assign", visit_id=unsure.id)
    assert (answer["success"], answer["error"]["code"]) == (False, "unauthorized")

    # Without the select of the source, only administrators may.
    er.async_get(hass).async_remove(SELECT)
    user = await user_client(hass, hass_ws_client, "system-users")
    answer = await send(user, "assign", visit_id=unsure.id)
    assert (answer["success"], answer["error"]["code"]) == (False, "unauthorized")
    assert not unsure.corrected
    answer = await ask("assign", visit_id=unsure.id)
    assert answer["success"]
    await settle(hass)
    assert unsure.corrected
