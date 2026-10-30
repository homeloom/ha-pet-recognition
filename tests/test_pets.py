"""Tests of adding pets, teaching them from photos, and changing them."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yarl
from aiohttp import ClientError
from homeassistant.components.image import Image
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.pet_recognition.const import DOMAIN
from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.examples import Candidate, ExampleStore, Origin

from .answers import CROP, MODEL, URL, analysis, animal, error, info, unit
from .common import Upload, configured

PICTURE = b"\xff\xd8\xff\xe0 a JPEG photo"
SPECIES = ("cat", "dog", "bird")


def engine(
    aioclient_mock: AiohttpClientMocker, *, max_bytes: int = 1000, **analyze: object
) -> None:
    """Make the engine answer /v1/info, and /v1/analyze with `analyze`."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info(max_bytes=max_bytes))
    aioclient_mock.post(f"{URL}/v1/analyze", **(analyze or {"json": cat()}))


def cat(*values: float) -> dict[str, Any]:
    """An analysis with one cat."""
    return analysis(animal("cat", unit(*(values or (1, 0, 0)))))


async def set_up(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> MockConfigEntry:
    engine(aioclient_mock)
    entry = configured(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def start(hass: HomeAssistant, entry: MockConfigEntry, species: str = "cat") -> str:
    """Start adding a pet named Pet A, and give the flow's ID."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "pet"), context={"source": SOURCE_USER}
    )
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": " Pet A ", "species": species}
    )
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "photos")
    assert result["description_placeholders"] == {"pet": "Pet A", "count": "0"}
    return result["flow_id"]


async def choose(hass: HomeAssistant, flow_id: str, step: str) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_configure(flow_id, {"next_step_id": step})


async def send(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_configure(flow_id, user_input)


async def add_pet(
    hass: HomeAssistant, entry: MockConfigEntry, upload: Upload, photos: int = 1
) -> str:
    """Add Pet A with `photos` photos, and give the ID of its subentry."""
    flow_id = await start(hass, entry)
    for _ in range(photos):
        await choose(hass, flow_id, "photo")
        result = await send(hass, flow_id, {"photo": upload(PICTURE)})
        assert result["type"] is FlowResultType.MENU
    result = await choose(hass, flow_id, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry_id,) = (
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == "pet"
    )
    return subentry_id


async def test_a_pet_is_added_with_its_photos(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["description_placeholders"] == {"pet": "Pet A", "count": "1"}
    ((_method, url, data, _headers),) = [
        call for call in aioclient_mock.mock_calls if call[0] == "POST"
    ]
    assert data == PICTURE
    assert dict(url.query) == {"min_area": "0.01", "limit": "5", "species": "cat"}
    # Nothing is stored before the pet is.
    assert not entry.runtime_data.examples.gallery.pets()

    engine(aioclient_mock, json=cat(0, 1, 0))
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["description_placeholders"]["count"] == "2"

    result = await choose(hass, flow_id, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Pet A"
    assert result["data"] == {"species": "cat"}
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    (subentry,) = (s for s in entry.subentries.values() if s.subentry_type == "pet")
    store = entry.runtime_data.examples
    examples = store.gallery.of(subentry.unique_id)
    assert len(examples) == 2
    assert all(example.uploaded and example.model == MODEL for example in examples)
    assert all(store.origin(example.id) is Origin.UPLOAD for example in examples)
    assert await store.async_read_crop(examples[0].id) == CROP

    (device,) = (
        device
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        if (DOMAIN, f"{entry.entry_id}_{subentry.subentry_id}") in device.identifiers
    )
    assert device.name == "Pet A"
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{subentry.subentry_id}_examples"
    )
    assert entity_id == "sensor.pet_a_examples"
    registered = entity_registry.async_get(entity_id)
    assert registered.config_subentry_id == subentry.subentry_id
    assert registered.device_id == device.id
    assert hass.states.get(entity_id).state == "2"
    assert not issue_registry.issues


async def test_a_photo_is_taken_from_a_camera_or_an_image(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    image = Image(content_type="image/jpeg", content=PICTURE)
    with (
        patch(
            "custom_components.pet_recognition.pets.async_get_camera_image",
            AsyncMock(return_value=image),
        ) as camera,
        patch(
            "custom_components.pet_recognition.pets.async_get_entity_image",
            AsyncMock(side_effect=HomeAssistantError("gone")),
        ) as entity_image,
    ):
        result = await choose(hass, flow_id, "capture")
        assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "capture")
        result = await send(hass, flow_id, {"entity_id": "camera.garden"})
        assert result["description_placeholders"]["count"] == "1"
        camera.assert_awaited_once_with(hass, "camera.garden")

        await choose(hass, flow_id, "capture")
        result = await send(hass, flow_id, {"entity_id": "image.feeder"})
        assert result["errors"] == {"base": "cannot_capture"}
        entity_image.assert_awaited_once_with(hass, "image.feeder")
        # Back to the menu without a photo.
        result = await send(hass, flow_id, {})
        assert result["type"] is FlowResultType.MENU

    await choose(hass, flow_id, "finish")
    await hass.async_block_till_done()
    store = entry.runtime_data.examples
    (example,) = store.gallery
    assert store.origin(example.id) is Origin.CAPTURE
    assert example.uploaded


@pytest.mark.parametrize(
    ("analyze", "reason"),
    [
        ({"json": analysis()}, "no_animal"),
        ({"json": analysis(animal("dog"))}, "no_animal"),
        (
            {"json": analysis(animal("cat"), animal("cat", unit(0, 1, 0)))},
            "several_animals",
        ),
        ({"status": 422, "json": error("unreadable_image")}, "unreadable_picture"),
        ({"status": 413, "json": error("too_large")}, "picture_too_large"),
        ({"status": 400, "json": error("invalid_parameter")}, "cannot_analyze"),
        ({"json": {"animals": "none"}}, "cannot_analyze"),
        ({"exc": ClientError("refused")}, "cannot_connect"),
        ({"status": 503, "json": error("busy")}, "engine_busy"),
    ],
)
async def test_a_photo_that_cannot_be_an_example_is_explained(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    analyze: dict[str, Any],
    reason: str,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    engine(aioclient_mock, **analyze)
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "photo")
    assert result["errors"] == {"base": reason}

    # Another photo works.
    engine(aioclient_mock)
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["description_placeholders"]["count"] == "1"


async def test_the_species_of_the_pet_is_asked_for(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry, species="dog")
    engine(aioclient_mock, json=analysis(animal("dog"), animal("cat", unit(0, 1, 0))))
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["type"] is FlowResultType.MENU
    (call,) = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert call[1].query["species"] == "dog"


async def test_a_photo_too_large_for_the_engine_is_not_sent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(b"x" * 1001)})
    assert result["errors"] == {"base": "picture_too_large"}
    assert not [call for call in aioclient_mock.mock_calls if call[0] == "POST"]


async def test_a_lost_upload_is_explained(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": "0123456789abcdef0123456789abcdef"})
    assert result["errors"] == {"base": "upload_failed"}


async def test_photos_need_a_connected_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", exc=ClientError("refused"))
    entry = configured(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_same_photo_twice_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    await send(hass, flow_id, {"photo": upload(PICTURE)})
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "duplicate_picture"}


async def test_a_new_pet_takes_at_most_the_limit_of_examples(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.pet_recognition.config_flow.EXAMPLE_LIMIT", 1)
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    await send(hass, flow_id, {"photo": upload(PICTURE)})
    engine(aioclient_mock, json=cat(0, 1, 0))
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "too_many_examples"}


async def test_a_name_is_needed(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await set_up(hass, aioclient_mock)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "pet"), context={"source": SOURCE_USER}
    )
    result = await send(hass, result["flow_id"], {"name": "  ", "species": "cat"})
    assert result["errors"] == {"name": "empty_name"}


async def test_an_abandoned_pet_leaves_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    await send(hass, flow_id, {"photo": upload(PICTURE)})
    hass.config_entries.subentries.async_abort(flow_id)
    assert not entry.runtime_data.examples.gallery.pets()
    assert not entry.runtime_data.examples.directory.exists()


async def test_a_pet_without_photos_raises_a_repair(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    issue_registry: ir.IssueRegistry,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload, photos=0)
    issue = issue_registry.async_get_issue(DOMAIN, f"pet_without_examples_{subentry_id}")
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"pet": "Pet A"}

    # A photo added in the pet's settings clears it at once.
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "change")
    assert result["description_placeholders"] == {"pet": "Pet A", "count": "0"}
    await choose(hass, result["flow_id"], "photo")
    result = await send(hass, result["flow_id"], {"photo": upload(PICTURE)})
    assert result["description_placeholders"]["count"] == "1"
    assert not issue_registry.issues
    assert hass.states.get("sensor.pet_a_examples").state == "1"


async def test_the_settings_of_a_pet_add_and_remove_examples(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    key = entry.subentries[subentry_id].unique_id
    store = entry.runtime_data.examples

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    flow_id = result["flow_id"]
    engine(aioclient_mock, json=cat(0, 1, 0))
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["description_placeholders"]["count"] == "2"
    # Stored at once.
    first, second = store.gallery.of(key)

    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "duplicate_picture"}
    result = await send(hass, flow_id, {})
    assert result["description_placeholders"]["count"] == "2"

    result = await choose(hass, flow_id, "remove")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "remove")
    pictures = result["description_placeholders"]["pictures"]
    assert pictures.startswith(
        f'1.&nbsp;<img src="/api/pet_recognition/example/{entry.entry_id}/{first.id}?authSig='
    )
    assert (
        f' 2.&nbsp;<img src="/api/pet_recognition/example/{entry.entry_id}/{second.id}?' in pictures
    )
    assert pictures.count('height="96">') == 2
    result = await send(hass, flow_id, {"examples": [first.id]})
    assert result["type"] is FlowResultType.MENU
    assert [example.id for example in store.gallery.of(key)] == [second.id]
    assert hass.states.get("sensor.pet_a_examples").state == "1"

    # Nothing chosen removes nothing.
    await choose(hass, flow_id, "remove")
    await send(hass, flow_id, {})
    assert len(store.gallery.of(key)) == 1

    result = await choose(hass, flow_id, "done")
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")


async def test_a_full_pet_refuses_more_photos_in_its_settings(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    monkeypatch.setattr(entry.runtime_data.examples.gallery, "limit", 1)
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    engine(aioclient_mock, json=cat(0, 1, 0))
    await choose(hass, result["flow_id"], "photo")
    result = await send(hass, result["flow_id"], {"photo": upload(PICTURE)})
    assert result["errors"] == {"base": "too_many_examples"}


async def test_a_pet_is_renamed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    flow_id = result["flow_id"]
    await choose(hass, flow_id, "rename")
    result = await send(hass, flow_id, {"name": " "})
    assert result["errors"] == {"name": "empty_name"}
    result = await send(hass, flow_id, {"name": "Pet B"})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await hass.async_block_till_done()
    assert entry.subentries[subentry_id].title == "Pet B"
    assert entry.state is ConfigEntryState.LOADED
    # The examples stay with the pet.
    assert len(entry.runtime_data.examples.gallery.of(entry.subentries[subentry_id].unique_id)) == 1


async def test_a_removed_pet_loses_its_examples_and_repair(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    issue_registry: ir.IssueRegistry,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    with_photo = await add_pet(hass, entry, upload)
    without_photo = await add_pet_named(hass, entry, "Pet B")
    assert issue_registry.async_get_issue(DOMAIN, f"pet_without_examples_{without_photo}")
    directory = entry.runtime_data.examples.directory
    assert len(list(directory.iterdir())) == 1

    for subentry_id in (with_photo, without_photo):
        assert hass.config_entries.async_remove_subentry(entry, subentry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data.examples.gallery.pets()
    assert not list(directory.iterdir())
    assert not issue_registry.issues


async def add_pet_named(hass: HomeAssistant, entry: MockConfigEntry, name: str) -> str:
    """Add a pet without photos, and give the ID of its subentry."""
    before = set(entry.subentries)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "pet"), context={"source": SOURCE_USER}
    )
    await send(hass, result["flow_id"], {"name": name, "species": "dog"})
    await choose(hass, result["flow_id"], "finish")
    await hass.async_block_till_done()
    (subentry_id,) = set(entry.subentries) - before
    return subentry_id


async def test_pets_need_a_loaded_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    reconfigure = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    adding = await start(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "pet"), context={"source": SOURCE_USER}
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "entry_not_loaded")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "pet"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["reason"] == "entry_not_loaded"

    # Flows that were open meanwhile.
    result = await choose(hass, adding, "finish")
    assert result["reason"] == "entry_not_loaded"
    result = await choose(hass, reconfigure["flow_id"], "remove")
    assert result["reason"] == "entry_not_loaded"


async def test_examples_of_an_older_model_are_embedded_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    await add_pet(hass, entry, upload)
    store = entry.runtime_data.examples
    (old,) = store.gallery
    await store.async_add(old.pet, Candidate(CROP, tuple(unit(0, 1, 0)), MODEL), Origin.ANSWER)

    # The engine is updated with another model.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info(model="next-model@1"))
    aioclient_mock.post(
        f"{URL}/v1/embed",
        json={"model": "next-model@1", "embedding": unit(1, 1, 0)},
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    store = entry.runtime_data.examples
    assert {example.model for example in store.gallery} == {"next-model@1"}
    # Both crops gave the same embedding: one example is left, the given one.
    (example,) = store.gallery
    assert example.id == old.id
    embeds = [call for call in aioclient_mock.mock_calls if call[1].path == "/v1/embed"]
    assert len(embeds) == 2
    assert embeds[0][2] == CROP


async def test_embedding_again_waits_for_the_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    await add_pet(hass, entry, upload)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info(model="next-model@1"))
    aioclient_mock.post(f"{URL}/v1/embed", status=503, json=error("busy"))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    store = entry.runtime_data.examples
    assert {example.model for example in store.gallery} == {MODEL}

    # The engine changed its model again while embedding.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info(model="next-model@1"))
    aioclient_mock.post(f"{URL}/v1/embed", json={"model": "third@1", "embedding": unit(1, 0)})
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert {example.model for example in store.gallery} == {MODEL}


async def test_a_menu_shown_again_keeps_its_choices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "photos")
    assert result["menu_options"] == ["photo", "folder", "capture", "finish"]
    subentry_id = await add_pet(hass, entry, upload)
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"])
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "change")
    assert result["menu_options"] == ["photo", "folder", "capture", "remove", "rename", "done"]


@pytest.mark.parametrize(
    ("picture", "reason"), [(PICTURE, "cannot_connect"), (b"x" * 1001, "picture_too_large")]
)
async def test_a_photo_taken_is_checked_like_an_upload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, picture: bytes, reason: str
) -> None:
    if reason == "cannot_connect":
        # The engine never answered since Home Assistant started.
        aioclient_mock.get(f"{URL}/v1/info", exc=ClientError("refused"))
        entry = configured(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    else:
        entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    image = Image(content_type="image/jpeg", content=picture)
    with patch(
        "custom_components.pet_recognition.pets.async_get_entity_image",
        AsyncMock(return_value=image),
    ):
        await choose(hass, flow_id, "capture")
        result = await send(hass, flow_id, {"entity_id": "image.feeder"})
    assert result["errors"] == {"base": reason}
    assert not [call for call in aioclient_mock.mock_calls if call[0] == "POST"]


async def test_deleting_the_entry_deletes_its_examples(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    await add_pet(hass, entry, upload)
    directory = entry.runtime_data.examples.directory.parent
    assert directory.is_dir()
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not directory.exists()


def new_model(aioclient_mock: AiohttpClientMocker, **embed: object) -> None:
    """Make the engine answer with another model, and /v1/embed with `embed`."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info(model="next-model@1"))
    aioclient_mock.post(
        f"{URL}/v1/embed",
        **(embed or {"json": {"model": "next-model@1", "embedding": unit(1, 1, 0)}}),
    )


async def reload(hass: HomeAssistant, entry: MockConfigEntry) -> ExampleStore:
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data.examples


async def on_disk(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[set[str], set[str]]:
    """The examples a restart would read, and the crops on disk."""
    store = ExampleStore(hass, entry.entry_id)
    await store.async_load()
    names = {path.stem for path in store.directory.iterdir()}
    return {example.id for example in store.gallery}, names


async def test_the_given_examples_are_embedded_again_first(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    await add_pet(hass, entry, upload)
    store = entry.runtime_data.examples
    (first,) = store.gallery
    # A learned example comes before the photo the user gave.
    await store.async_add(first.pet, Candidate(CROP, tuple(unit(0, 1, 0)), MODEL), Origin.ANSWER)
    await store.async_remove([first.id])
    await store.async_add(first.pet, Candidate(CROP, tuple(unit(0, 0, 1)), MODEL), Origin.UPLOAD)
    _, given = store.gallery

    new_model(aioclient_mock)
    store = await reload(hass, entry)
    # Both crops gave the same embedding: the given one stays.
    assert [example.id for example in store.gallery] == [given.id]
    assert store.origin(given.id) is Origin.UPLOAD
    assert await on_disk(hass, entry) == ({given.id}, {given.id})


async def test_an_example_without_its_crop_is_removed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    for values in ((1, 0, 0), (0, 1, 0)):
        engine(aioclient_mock, json=cat(*values))
        await choose(hass, flow_id, "photo")
        await send(hass, flow_id, {"photo": upload(PICTURE)})
    await choose(hass, flow_id, "finish")
    await hass.async_block_till_done()
    store = entry.runtime_data.examples
    lost, kept = store.gallery
    store.crop_path(lost.id).unlink()

    new_model(aioclient_mock)
    store = await reload(hass, entry)
    assert [(example.id, example.model) for example in store.gallery] == [(kept.id, "next-model@1")]
    assert "crop cannot be used" in caplog.text


async def test_an_example_removed_while_its_crop_is_read_is_skipped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    await add_pet(hass, entry, upload)
    store = entry.runtime_data.examples

    async def removed_meanwhile(example_id: str) -> bytes:
        await store.async_remove([example_id])
        raise FileNotFoundError(example_id)

    new_model(aioclient_mock)
    with patch.object(store, "async_read_crop", removed_meanwhile):
        await reload(hass, entry)
    assert len(store.gallery) == 0
    assert "crop cannot be used" not in caplog.text


@pytest.mark.parametrize(
    ("embed", "left"),
    [
        ({"status": 422, "json": error("unreadable_image")}, 0),
        ({"status": 413, "json": error("too_large")}, 0),
        ({"status": 400, "json": error("invalid_request")}, 1),
    ],
)
async def test_a_crop_the_engine_refuses(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    embed: dict[str, Any],
    left: int,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    await add_pet(hass, entry, upload)
    new_model(aioclient_mock, **embed)
    store = await reload(hass, entry)
    # A crop it cannot read can never match again; another refusal waits.
    assert len(store.gallery) == left


async def test_a_photo_analyzed_during_a_reload_is_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    store = entry.runtime_data.examples
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    flow_id = result["flow_id"]

    async def analyze_while_reloading(
        method: str, url: yarl.URL, data: object
    ) -> AiohttpClientMockResponse:
        # Such as another pet added or removed meanwhile.
        await hass.config_entries.async_reload(entry.entry_id)
        return AiohttpClientMockResponse(method, url, json=cat(0, 1, 0))

    engine(aioclient_mock, side_effect=analyze_while_reloading)
    await choose(hass, flow_id, "photo")
    result = await send(hass, flow_id, {"photo": upload(PICTURE)})
    assert result["description_placeholders"]["count"] == "2"
    assert entry.runtime_data.examples is store
    # The next change saves every example.
    pet = entry.subentries[subentry_id].unique_id
    await store.async_add(pet, Candidate(CROP, tuple(unit(0, 0, 1)), MODEL), Origin.ANSWER)
    ids = {example.id for example in store.gallery}
    assert len(ids) == 3
    assert await on_disk(hass, entry) == (ids, ids)


async def test_a_pet_finished_during_a_reload_keeps_its_examples(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    store = entry.runtime_data.examples
    flow_id = await start(hass, entry)
    for values in ((1, 0, 0), (0, 1, 0)):
        engine(aioclient_mock, json=cat(*values))
        await choose(hass, flow_id, "photo")
        await send(hass, flow_id, {"photo": upload(PICTURE)})
    add_many = store.async_add_many

    async def add_while_reloading(pet: str, candidates: list[Any]) -> list[Any]:
        outcomes = await add_many(pet, candidates)
        # Stored, but not yet a subentry.
        await hass.config_entries.async_reload(entry.entry_id)
        return outcomes

    with patch.object(store, "async_add_many", add_while_reloading):
        result = await choose(hass, flow_id, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    ids = {example.id for example in store.gallery}
    assert len(ids) == 2
    assert await on_disk(hass, entry) == (ids, ids)


async def test_menus_need_a_loaded_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    adding = await start(hass, entry)
    await choose(hass, adding, "photo")
    reconfigure = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    # Going back from a photo, or showing a menu again.
    result = await send(hass, adding, {})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "entry_not_loaded")
    result = await hass.config_entries.subentries.async_configure(reconfigure["flow_id"])
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "entry_not_loaded")


async def test_the_examples_of_the_other_animal_are_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up(hass, aioclient_mock)
    store = entry.runtime_data.examples
    await store.async_add(OTHER_ANIMAL, Candidate(CROP, tuple(unit(1, 0, 0)), MODEL), Origin.ANSWER)
    store = await reload(hass, entry)
    assert store.gallery.pets() == {OTHER_ANIMAL}
