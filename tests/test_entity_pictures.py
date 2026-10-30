"""Tests of the pictures of the entities of a source, and of the pictures kept."""

from __future__ import annotations

from http import HTTPStatus

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_PICTURE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.pet_recognition.views import FRAME_URL, signed_picture_url

from .asking import Phones
from .test_correcting import assign
from .test_overview import three_visits
from .watching import FEEDER, Shown, journal, source_id, tick

LAST_VISITOR = "sensor.feeder_last_visitor"
CORRECT = "select.feeder_correct_last_visit"
TO_REVIEW = "sensor.feeder_visits_to_review"


def picture(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.attributes.get(ATTR_ENTITY_PICTURE)


async def test_the_entities_of_a_source_show_the_picture_of_their_visit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    unsure, _of_b, going_on = journal(entry).of(source_id(entry))[-3:]
    client = await hass_client_no_auth()

    # The latest visit has no animal in sight: the feeder keeps its picture.
    frame = f"/api/pet_recognition/frame/{entry.entry_id}/{going_on.id}?authSig="
    for entity_id in (LAST_VISITOR, CORRECT):
        url = picture(hass, entity_id)
        assert url is not None
        assert url.startswith(frame)
        response = await client.get(url)
        assert response.status == HTTPStatus.OK
        assert response.headers["Cache-Control"] == "private, no-store"
        assert await response.read() == shown[FEEDER]
    # The visit to review is the unsure one, shown by the crop of its animal.
    url = picture(hass, TO_REVIEW)
    assert url is not None
    crop = unsure.detections[0].crop
    assert url.startswith(f"/api/pet_recognition/picture/{entry.entry_id}/{crop}?authSig=")
    assert (await client.get(url)).status == HTTPStatus.OK

    # Left out of the recorder.
    for entity_id in (LAST_VISITOR, CORRECT, TO_REVIEW):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state_info is not None
        assert ATTR_ENTITY_PICTURE in state.state_info["unrecorded_attributes"]

    # Signed again each hour, before it expires.
    before = picture(hass, LAST_VISITOR)
    await tick(hass, freezer, 3600)
    again = picture(hass, LAST_VISITOR)
    assert again is not None
    assert again != before
    assert (await client.get(again)).status == HTTPStatus.OK

    # The visit without an animal has ended, to review too: once it is
    # reviewed, the unsure one shows again; once both are, nothing.
    assert picture(hass, TO_REVIEW).startswith(frame)
    await assign(hass, source=TO_REVIEW, pets=[])
    assert picture(hass, TO_REVIEW).startswith(url.split("?")[0])
    await assign(hass, visit_id=unsure.id, pets=[])
    assert picture(hass, TO_REVIEW) is None


async def test_a_picture_without_an_animal_that_is_not_kept_shows_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    # As if the feeder's pictures without an animal were not kept.
    journal(entry).every_picture = frozenset()
    await tick(hass, freezer, 3600)
    assert picture(hass, LAST_VISITOR) is None
    assert picture(hass, CORRECT) is None


async def test_only_the_pictures_kept_are_served(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_client_no_auth: ClientSessionGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    unsure, _of_b, going_on = journal(entry).of(source_id(entry))[-3:]
    client = await hass_client_no_auth()

    def signed(entry_id: str, picture_id: str) -> str:
        return signed_picture_url(hass, FRAME_URL.format(entry_id=entry_id, picture_id=picture_id))

    unsigned = FRAME_URL.format(entry_id=entry.entry_id, picture_id=going_on.id)
    assert (await client.get(unsigned)).status == HTTPStatus.UNAUTHORIZED
    # A picture with an animal that is not the latest, a name that is no
    # ID, another entry.
    for entry_id, picture_id in (
        (entry.entry_id, unsure.id),
        (entry.entry_id, "F" * 32),
        ("e" * 26, going_on.id),
    ):
        assert (await client.get(signed(entry_id, picture_id))).status == HTTPStatus.NOT_FOUND
    url = signed(entry.entry_id, going_on.id)
    assert (await client.get(url)).status == HTTPStatus.OK

    # A companion app adds the URL to the server's as a path, escaped, and
    # sends the phone's token.
    user = await hass.auth.async_create_user("Phone", group_ids=["system-users"])
    token = await hass.auth.async_create_refresh_token(user, "https://example.com/app")
    phone = await hass_client(hass.auth.async_create_access_token(token))
    response = await phone.get(url.replace("?", "%3F"))
    assert response.status == HTTPStatus.OK
    assert response.content_type == "image/jpeg"

    journal(entry).frame_path(going_on.id).unlink()
    assert (await client.get(url)).status == HTTPStatus.NOT_FOUND
