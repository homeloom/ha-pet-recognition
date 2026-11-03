"""Tests of the picture that shows each pet: an entity's, or a photo kept."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics
from custom_components.pet_recognition.examples import Origin
from custom_components.pet_recognition.pets import async_candidate
from custom_components.pet_recognition.portraits import (
    async_portrait_saved,
    async_save_portrait,
    portrait_directory,
)

from .answers import CROP
from .common import Upload
from .test_pets import PICTURE, add_pet, cat, choose, engine, send, set_up

PROFILE = "sensor.pet_a_profile"
PROFILE_PICTURE = "/api/image_proxy/image.pet_a_profile?token=abc&size=1"


async def portrait_menu(hass: HomeAssistant, entry: MockConfigEntry, subentry_id: str) -> str:
    """Open the settings of a pet at the choice of its picture, and give the flow's ID."""
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await choose(hass, result["flow_id"], "portrait")
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "portrait")
    return result["flow_id"]


async def overview_pet(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> dict[str, Any]:
    """The one pet of the overview."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "pet_recognition/overview"})
    answer = await client.receive_json()
    (pet,) = answer["result"]["pets"]
    return pet


async def settled(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Wait for the reload that saving a pet starts."""
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


def kept(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    """The photos of pets on disk."""
    directory = portrait_directory(hass, entry.entry_id)
    return {path.name for path in directory.iterdir()} if directory.is_dir() else set()


async def test_a_pet_shows_the_picture_of_an_entity(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    hass_ws_client: WebSocketGenerator,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    assert (await overview_pet(hass, hass_ws_client))["picture"] is None
    flow_id = await portrait_menu(hass, entry, subentry_id)
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert result["menu_options"] == [
        "portrait_entity",
        "portrait_photo",
        "portrait_example",
        "change",
    ]
    assert result["description_placeholders"] == {"pet": "Pet A", "portrait": "-"}

    result = await choose(hass, flow_id, "portrait_entity")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "portrait_entity")
    # An entity without a picture, or without a state.
    hass.states.async_set(PROFILE, "Pet A", {"entity_picture": ""})
    for entity_id in (PROFILE, "sensor.unknown"):
        result = await send(hass, flow_id, {"entity_id": entity_id})
        assert result["errors"] == {"entity_id": "no_entity_picture"}
    # Back without one.
    result = await send(hass, flow_id, {})
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "portrait")

    hass.states.async_set(PROFILE, "Pet A", {"entity_picture": PROFILE_PICTURE})
    await choose(hass, flow_id, "portrait_entity")
    result = await send(hass, flow_id, {"entity_id": PROFILE})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await settled(hass, entry)
    assert entry.subentries[subentry_id].data == {"species": "cat", "portrait_entity": PROFILE}
    assert (await overview_pet(hass, hass_ws_client))["picture"] == PROFILE_PICTURE
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["pets"][0]["picture"] == "entity"
    assert "image_proxy" not in str(diagnostics)

    # The picture shown now, and the entity suggested again.
    flow_id = await portrait_menu(hass, entry, subentry_id)
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert "portrait_none" in result["menu_options"]
    assert result["description_placeholders"]["portrait"] == (
        '<img src="/api/image_proxy/image.pet_a_profile?token=abc&amp;size=1" alt="Pet A"'
        ' height="96">'
    )
    result = await choose(hass, flow_id, "portrait_entity")
    (field,) = result["data_schema"].schema
    assert field.description == {"suggested_value": PROFILE}

    # The picture follows the entity, as it changes.
    hass.states.async_set(PROFILE, "Pet A", {"entity_picture": "/local/pet_a.jpg"})
    assert (await overview_pet(hass, hass_ws_client))["picture"] == "/local/pet_a.jpg"
    hass.states.async_remove(PROFILE)
    assert (await overview_pet(hass, hass_ws_client))["picture"] is None


async def test_a_pet_follows_its_entity_renamed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    registry = er.async_get(hass)
    profile = registry.async_get_or_create("sensor", "test", "pet_a", suggested_object_id="pet_a")
    other = registry.async_get_or_create("sensor", "test", "pet_b", suggested_object_id="pet_b")
    hass.states.async_set(profile.entity_id, "Pet A", {"entity_picture": PROFILE_PICTURE})
    flow_id = await portrait_menu(hass, entry, subentry_id)
    await choose(hass, flow_id, "portrait_entity")
    await send(hass, flow_id, {"entity_id": profile.entity_id})
    await settled(hass, entry)

    registry.async_update_entity(other.entity_id, new_entity_id="sensor.pet_b_profile")
    registry.async_update_entity(profile.entity_id, new_entity_id=PROFILE)
    await settled(hass, entry)
    assert entry.subentries[subentry_id].data["portrait_entity"] == PROFILE


async def test_a_pet_shows_the_crop_of_an_uploaded_photo(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    flow_id = await portrait_menu(hass, entry, subentry_id)
    result = await choose(hass, flow_id, "portrait_photo")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "portrait_photo")
    # Checked as the photos of the examples are.
    engine(aioclient_mock, json=cat(), max_bytes=1000)
    result = await send(hass, flow_id, {"photo": upload(b"x" * 1001)})
    assert result["errors"] == {"base": "picture_too_large"}
    result = await send(hass, flow_id, {"photo": "0123456789abcdef0123456789abcdef"})
    assert result["errors"] == {"base": "upload_failed"}
    engine(aioclient_mock, json={"width": 1, "height": 1, "model": "m", "animals": []})
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "no_animal"}
    result = await send(hass, flow_id, {})
    assert result["step_id"] == "portrait"

    engine(aioclient_mock, json=cat(0, 1, 0))
    await choose(hass, flow_id, "portrait_photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await settled(hass, entry)
    file_id = entry.subentries[subentry_id].data["portrait_file"]
    assert kept(hass, entry) == {f"{file_id}.jpg"}
    # Not an example: the pet keeps the one it had.
    assert len(entry.runtime_data.examples.gallery.of(entry.subentries[subentry_id].unique_id)) == 1
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["pets"][0]["picture"] == "photo"
    assert file_id not in str(diagnostics)

    picture = (await overview_pet(hass, hass_ws_client))["picture"]
    assert picture.startswith(f"/api/pet_recognition/portrait/{entry.entry_id}/{file_id}?authSig=")
    client = await hass_client_no_auth()
    response = await client.get(picture)
    assert response.status == HTTPStatus.OK
    assert response.content_type == "image/jpeg"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert await response.read() == CROP
    unsigned = await client.get(picture.partition("?")[0])
    assert unsigned.status == HTTPStatus.UNAUTHORIZED

    # The photo shown in the settings, signed.
    flow_id = await portrait_menu(hass, entry, subentry_id)
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert result["description_placeholders"]["portrait"].startswith(
        f'<img src="/api/pet_recognition/portrait/{entry.entry_id}/{file_id}?authSig='
    )

    # Shown without a picture: the photo goes.
    result = await choose(hass, flow_id, "portrait_none")
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await settled(hass, entry)
    assert entry.subentries[subentry_id].data == {"species": "cat"}
    assert kept(hass, entry) == set()
    assert (await overview_pet(hass, hass_ws_client))["picture"] is None


async def test_a_pet_shows_the_crop_of_one_of_its_examples(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    store = entry.runtime_data.examples
    engine(aioclient_mock, json=cat(0, 1, 0))
    await store.async_add(
        entry.subentries[subentry_id].unique_id,
        await async_candidate(entry.runtime_data.coordinator, PICTURE, "cat"),
        Origin.UPLOAD,
    )
    first, second = store.gallery.of(entry.subentries[subentry_id].unique_id)

    flow_id = await portrait_menu(hass, entry, subentry_id)
    result = await choose(hass, flow_id, "portrait_example")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "portrait_example")
    pictures = result["description_placeholders"]["pictures"]
    assert pictures.startswith(
        f'1.&nbsp;<img src="/api/pet_recognition/example/{entry.entry_id}/{first.id}?authSig='
    )
    assert "2.&nbsp;<img src=" in pictures
    # Back without one.
    result = await send(hass, flow_id, {})
    assert result["step_id"] == "portrait"

    # An example removed meanwhile, or whose crop is gone.
    await choose(hass, flow_id, "portrait_example")
    await store.async_remove([first.id])
    result = await send(hass, flow_id, {"example": first.id})
    assert result["errors"] == {"base": "example_gone"}
    store.crop_path(second.id).rename(store.crop_path(second.id).with_suffix(".old"))
    result = await send(hass, flow_id, {"example": second.id})
    assert result["errors"] == {"base": "example_gone"}
    store.crop_path(second.id).with_suffix(".old").rename(store.crop_path(second.id))

    result = await send(hass, flow_id, {"example": second.id})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await settled(hass, entry)
    file_id = entry.subentries[subentry_id].data["portrait_file"]
    path = portrait_directory(hass, entry.entry_id) / f"{file_id}.jpg"
    assert path.read_bytes() == CROP

    # Kept even once its example is removed.
    await entry.runtime_data.examples.async_remove([second.id])
    assert path.is_file()


async def test_a_photo_replaced_or_of_a_pet_removed_is_deleted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    for _ in range(2):
        flow_id = await portrait_menu(hass, entry, subentry_id)
        await choose(hass, flow_id, "portrait_photo")
        await send(hass, flow_id, {"photo": upload(PICTURE)})
        await settled(hass, entry)
    file_id = entry.subentries[subentry_id].data["portrait_file"]
    assert kept(hass, entry) == {f"{file_id}.jpg"}

    # A photo being saved is kept until its pet names it.
    saving = await async_save_portrait(hass, entry.entry_id, CROP)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert f"{saving}.jpg" in kept(hass, entry)
    async_portrait_saved(hass, saving)

    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await settled(hass, entry)
    assert kept(hass, entry) == set()


async def test_the_picture_needs_a_loaded_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    hass_client: ClientSessionGenerator,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    flow_id = await portrait_menu(hass, entry, subentry_id)
    await choose(hass, flow_id, "portrait_photo")
    await send(hass, flow_id, {"photo": upload(PICTURE)})
    await settled(hass, entry)
    file_id = entry.subentries[subentry_id].data["portrait_file"]
    path = f"/api/pet_recognition/portrait/{entry.entry_id}/{file_id}"
    client = await hass_client()
    assert (await client.get(path)).status == HTTPStatus.OK
    assert (await client.get(path.replace(file_id, "0" * 32))).status == HTTPStatus.NOT_FOUND
    assert (await client.get(path.replace(file_id, ".."))).status == HTTPStatus.NOT_FOUND
    (portrait_directory(hass, entry.entry_id) / f"{file_id}.jpg").unlink()
    assert (await client.get(path)).status == HTTPStatus.NOT_FOUND

    flows = [await portrait_menu(hass, entry, subentry_id) for _ in range(3)]
    await choose(hass, flows[1], "portrait_photo")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert (await client.get(path)).status == HTTPStatus.NOT_FOUND
    result = await hass.config_entries.subentries.async_configure(flows[0])
    assert result["reason"] == "entry_not_loaded"
    result = await send(hass, flows[1], {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "cannot_connect"}
    result = await choose(hass, flows[2], "portrait_example")
    assert result["reason"] == "entry_not_loaded"


async def test_a_pet_without_examples_has_none_to_choose_from(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload, photos=0)
    flow_id = await portrait_menu(hass, entry, subentry_id)
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert result["menu_options"] == ["portrait_entity", "portrait_photo", "change"]
    result = await choose(hass, flow_id, "change")
    assert result["step_id"] == "change"

    # A photo needs the engine.
    await choose(hass, flow_id, "portrait")
    await choose(hass, flow_id, "portrait_photo")
    monkeypatch.setattr(entry.runtime_data.coordinator, "data", None)
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "cannot_connect"}
