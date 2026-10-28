"""Tests of the view that serves the crops of the examples."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from homeassistant.components.http import KEY_HASS_REFRESH_TOKEN_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import current_request
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.pet_recognition.examples import Candidate, Origin
from custom_components.pet_recognition.views import signed_example_url

from .answers import CROP, MODEL, URL, info, unit
from .common import configured


async def with_example(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> tuple[str, str]:
    """A loaded entry with one example, and their IDs."""
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    entry = configured(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    store = entry.runtime_data.examples
    await store.async_add("pet", Candidate(CROP, tuple(unit(1, 0)), MODEL), Origin.UPLOAD)
    (example,) = store.gallery
    return entry.entry_id, example.id


async def test_an_administrator_gets_the_crop(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client: ClientSessionGenerator
) -> None:
    entry_id, example_id = await with_example(hass, aioclient_mock)
    client = await hass_client()
    response = await client.get(f"/api/pet_recognition/example/{entry_id}/{example_id}")
    assert response.status == HTTPStatus.OK
    assert response.content_type == "image/jpeg"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert await response.read() == CROP


async def test_a_signed_url_works_without_a_header(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client_no_auth: ClientSessionGenerator,
    hass_access_token: str,
) -> None:
    entry_id, example_id = await with_example(hass, aioclient_mock)
    client = await hass_client_no_auth()
    unsigned = await client.get(f"/api/pet_recognition/example/{entry_id}/{example_id}")
    assert unsigned.status == HTTPStatus.UNAUTHORIZED
    # Signed while an administrator's request runs, as the pet's settings are.
    token = hass.auth.async_validate_access_token(hass_access_token)
    context = current_request.set({KEY_HASS_REFRESH_TOKEN_ID: token.id})
    try:
        url = signed_example_url(hass, entry_id, example_id)
    finally:
        current_request.reset(context)
    response = await client.get(url)
    assert response.status == HTTPStatus.OK
    assert await response.read() == CROP


async def test_a_url_signed_outside_a_request_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry_id, example_id = await with_example(hass, aioclient_mock)
    # Home Assistant then signs as its read-only content user.
    url = signed_example_url(hass, entry_id, example_id)
    client = await hass_client_no_auth()
    response = await client.get(url)
    assert response.status == HTTPStatus.UNAUTHORIZED


async def test_other_users_get_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator,
    hass_read_only_access_token: str,
) -> None:
    entry_id, example_id = await with_example(hass, aioclient_mock)
    client = await hass_client(hass_read_only_access_token)
    response = await client.get(f"/api/pet_recognition/example/{entry_id}/{example_id}")
    assert response.status == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("entry", "example"),
    [
        ("{entry_id}", "0" * 32),
        ("{entry_id}", "%2E%2E"),
        ("{entry_id}", "A" * 32),
        ("{entry_id}", "{example_id}.jpg"),
        ("unknown", "{example_id}"),
    ],
)
async def test_anything_else_is_not_found(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator,
    entry: str,
    example: str,
) -> None:
    entry_id, example_id = await with_example(hass, aioclient_mock)
    client = await hass_client()
    path = f"/api/pet_recognition/example/{entry}/{example}".format(
        entry_id=entry_id, example_id=example_id
    )
    response = await client.get(path)
    assert response.status == HTTPStatus.NOT_FOUND


async def test_an_entry_of_another_integration_or_not_loaded_is_not_found(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client: ClientSessionGenerator
) -> None:
    entry_id, example_id = await with_example(hass, aioclient_mock)
    other = configured(hass)
    object.__setattr__(other, "domain", "other")
    client = await hass_client()
    response = await client.get(f"/api/pet_recognition/example/{other.entry_id}/{example_id}")
    assert response.status == HTTPStatus.NOT_FOUND
    assert await hass.config_entries.async_unload(entry_id)
    response = await client.get(f"/api/pet_recognition/example/{entry_id}/{example_id}")
    assert response.status == HTTPStatus.NOT_FOUND


async def test_a_crop_deleted_meanwhile_is_not_found(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client: ClientSessionGenerator
) -> None:
    entry_id, example_id = await with_example(hass, aioclient_mock)
    entry = hass.config_entries.async_get_entry(entry_id)
    entry.runtime_data.examples.crop_path(example_id).unlink()
    client = await hass_client()
    response = await client.get(f"/api/pet_recognition/example/{entry_id}/{example_id}")
    assert response.status == HTTPStatus.NOT_FOUND
