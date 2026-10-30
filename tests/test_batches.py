"""Tests of adding several photos of a pet at once: a folder of the media, or a
zip archive uploaded in the photo field."""

from __future__ import annotations

import asyncio
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yarl
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.pet_recognition import batches
from custom_components.pet_recognition.batches import Summary, folder_batch, media_folders
from custom_components.pet_recognition.examples import Origin
from custom_components.pet_recognition.pets import PictureError

from .answers import analysis
from .common import Upload
from .test_pets import add_pet, cat, choose, engine, send, set_up, start

PNG = b"\x89PNG\r\n\x1a\n"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
NO_ANIMAL = b"\xff\xd8\xff no animal"


def jpeg(name: str) -> bytes:
    return b"\xff\xd8\xff " + name.encode()


def photos(aioclient_mock: AiohttpClientMocker, *, max_bytes: int = 1000) -> list[bytes]:
    """Make the engine see a different cat in each photo, the same cat in
    photos that end alike, and no animal in `NO_ANIMAL`. Give the photos
    analyzed."""
    seen: list[bytes] = []

    async def analyze(method: str, url: yarl.URL, data: bytes) -> AiohttpClientMockResponse:
        seen.append(data)
        if data == NO_ANIMAL:
            return AiohttpClientMockResponse(method, url, json=analysis())
        number = data[-1]
        return AiohttpClientMockResponse(method, url, json=cat(1, number % 7, number % 5))

    engine(aioclient_mock, max_bytes=max_bytes, side_effect=analyze)
    return seen


def media(hass: HomeAssistant, tmp_path: Path) -> Path:
    """A media directory, with the photos of three folders."""
    root = tmp_path / "media"
    pets = root / "pets" / "a"
    (pets / "sub").mkdir(parents=True)
    (pets / "01.jpg").write_bytes(jpeg("1"))
    (pets / "02.PNG").write_bytes(PNG + b"2")
    # The same cat as 01.jpg.
    (pets / "03.webp").write_bytes(WEBP + b"1")
    (pets / "04.jpg").write_bytes(NO_ANIMAL)
    (pets / "05.jpg").write_bytes(b"not a photo")
    (pets / "06.jpg").write_bytes(jpeg("6") + b"x" * 1000)
    (pets / ".hidden.jpg").write_bytes(jpeg("7"))
    (pets / "notes.txt").write_text("notes")
    (pets / "sub" / "08.jpg").write_bytes(jpeg("8"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "09.jpg").write_bytes(jpeg("9"))
    (pets / "link.jpg").symlink_to(outside / "09.jpg")
    (root / "link").symlink_to(outside)
    (root / "empty").mkdir()
    deep = root / "deep" / "1" / "2"
    (deep / "3").mkdir(parents=True)
    (deep / "10.jpg").write_bytes(jpeg("10"))
    (deep / "3" / "11.jpg").write_bytes(jpeg("11"))
    hass.config.media_dirs = {"local": str(root)}
    return root


def options(result: dict[str, Any]) -> dict[str, str]:
    (selector,) = result["data_schema"].schema.values()
    return {option["value"]: option["label"] for option in selector.config["options"]}


async def learn(hass: HomeAssistant, flow_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Wait for a batch shown learning, and give its summary."""
    assert (result["type"], result["step_id"]) == (FlowResultType.SHOW_PROGRESS, "learning")
    assert result["progress_action"] == "learning"
    assert result["description_placeholders"] == {"pet": "Pet A"}
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "learned")
    return result["description_placeholders"]


def summary(added: list[str], **skipped: list[str]) -> dict[str, str]:
    found = {"pet": "Pet A", "added": str(len(added)), "added_names": ", ".join(added) or "-"}
    for group in ("duplicate", "no_animal", "unreadable", "not_added"):
        names = skipped.get(group, [])
        found[group] = str(len(names))
        found[f"{group}_names"] = ", ".join(names) or "-"
    return found


def archive(*entries: tuple[str, bytes]) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in entries:
            if name.endswith("/"):
                zipped.mkdir(name)
            else:
                zipped.writestr(name, data)
    return content.getvalue()


async def test_the_photos_of_a_media_folder_are_added(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    tmp_path: Path,
) -> None:
    root = media(hass, tmp_path)
    entry = await set_up(hass, aioclient_mock)
    seen = photos(aioclient_mock)
    flow_id = await start(hass, entry)
    result = await choose(hass, flow_id, "folder")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "folder")
    assert result["errors"] == {}
    # Not empty folders, nor deeper ones, nor those of a link.
    assert options(result) == {
        "local/deep/1/2": "deep/1/2 (1)",
        "local/pets/a": "pets/a (6)",
        "local/pets/a/sub": "pets/a/sub (1)",
    }

    result = await send(hass, flow_id, {"folder": "local/pets/a"})
    placeholders = await learn(hass, flow_id, result)
    assert placeholders == summary(
        ["01.jpg", "02.PNG"],
        duplicate=["03.webp"],
        no_animal=["04.jpg"],
        unreadable=["05.jpg", "06.jpg"],
    )
    # Neither the photo not recognized as one nor the one too large reached the engine.
    assert seen == [jpeg("1"), PNG + b"2", WEBP + b"1", NO_ANIMAL]
    result = await send(hass, flow_id, {})
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "photos")
    assert result["description_placeholders"]["count"] == "2"

    result = await choose(hass, flow_id, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    store = entry.runtime_data.examples
    (pet,) = store.gallery.pets()
    examples = store.gallery.of(pet)
    assert len(examples) == 2
    assert all(store.origin(example.id) is Origin.UPLOAD for example in examples)
    # The files stay where they are.
    assert (root / "pets" / "a" / "01.jpg").read_bytes() == jpeg("1")


async def test_the_settings_of_a_pet_add_a_folder_at_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    tmp_path: Path,
) -> None:
    media(hass, tmp_path)
    entry = await set_up(hass, aioclient_mock)
    subentry_id = await add_pet(hass, entry, upload)
    store = entry.runtime_data.examples
    photos(aioclient_mock)
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    flow_id = result["flow_id"]
    await choose(hass, flow_id, "folder")
    result = await send(hass, flow_id, {"folder": "local/pets/a/sub"})
    assert await learn(hass, flow_id, result) == summary(["08.jpg"])
    assert len(store.gallery.of(entry.subentries[subentry_id].unique_id)) == 2
    result = await send(hass, flow_id, {})
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "change")
    assert result["description_placeholders"]["count"] == "2"


async def test_without_a_folder_of_photos_the_media_are_explained(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    (tmp_path / "media").mkdir()
    hass.config.media_dirs = {"local": str(tmp_path / "media"), "gone": str(tmp_path / "gone")}
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    result = await choose(hass, flow_id, "folder")
    assert result["errors"] == {"base": "no_media_folder"}
    assert not result["data_schema"].schema
    result = await send(hass, flow_id, {})
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "photos")


async def test_a_folder_removed_meanwhile_is_explained(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    root = media(hass, tmp_path)
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "folder")
    for photo in (root / "deep" / "1" / "2").glob("*.jpg"):
        photo.unlink()
    (root / "deep" / "1" / "2" / "3" / "11.jpg").unlink()
    for folder in ("3", ""):
        (root / "deep" / "1" / "2" / folder).rmdir()
    result = await send(hass, flow_id, {"folder": "local/deep/1/2"})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "folder")
    assert result["errors"] == {"base": "folder_not_found"}
    assert "local/deep/1/2" not in options(result)


async def test_the_last_folder_removed_meanwhile_is_explained(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    (tmp_path / "media" / "a").mkdir(parents=True)
    (tmp_path / "media" / "a" / "01.jpg").write_bytes(jpeg("1"))
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "folder")
    (tmp_path / "media" / "a" / "01.jpg").unlink()
    (tmp_path / "media" / "a").rmdir()
    result = await send(hass, flow_id, {"folder": "local/a"})
    assert result["errors"] == {"base": "folder_not_found"}
    assert not result["data_schema"].schema


async def test_a_folder_needs_a_connected_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, tmp_path: Path
) -> None:
    media(hass, tmp_path)
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "folder")
    entry.runtime_data.coordinator.data = None
    result = await send(hass, flow_id, {"folder": "local/pets/a"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_photos_of_a_zip_archive_are_added(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    seen = photos(aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    zipped = archive(
        ("pets/", b""),
        ("pets/01.jpg", jpeg("1")),
        ("pets/02.png", PNG + b"2"),
        ("pets/03.jpg", jpeg("1")),
        ("04.jpg", NO_ANIMAL),
        ("read_me.txt", b"photos"),
        ("inner.zip", archive(("05.jpg", jpeg("5")))),
        ("06.webp", WEBP + b"x" * 1000),
        ("__MACOSX/pets/._01.jpg", jpeg("7")),
        (".DS_Store", b"metadata"),
        ("pets/.hidden.jpg", jpeg("8")),
    )
    result = await send(hass, flow_id, {"photo": upload(zipped)})
    assert await learn(hass, flow_id, result) == summary(
        ["01.jpg", "02.png"],
        duplicate=["03.jpg"],
        no_animal=["04.jpg"],
        unreadable=["read\\_me.txt", "inner.zip", "06.webp"],
    )
    assert seen == [jpeg("1"), PNG + b"2", jpeg("1"), NO_ANIMAL]
    result = await send(hass, flow_id, {})
    assert result["description_placeholders"]["count"] == "2"


async def test_a_zip_archive_is_bounded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    photos(aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    zipped = archive(("01.jpg", jpeg("1")), ("02.jpg", jpeg("2")), ("03.jpg", jpeg("3")))
    monkeypatch.setattr(batches, "ARCHIVE_ENTRIES", 2)
    result = await send(hass, flow_id, {"photo": upload(zipped)})
    assert result["errors"] == {"base": "archive_too_large"}

    # What is read of it in all.
    monkeypatch.setattr(batches, "ARCHIVE_ENTRIES", 3)
    monkeypatch.setattr(batches, "ARCHIVE_BYTES", len(jpeg("1")) * 2)
    result = await send(hass, flow_id, {"photo": upload(zipped)})
    assert await learn(hass, flow_id, result) == summary(
        ["01.jpg", "02.jpg"], unreadable=["03.jpg"]
    )


async def test_a_damaged_entry_of_a_zip_archive_is_skipped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    photos(aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_STORED) as zipped:
        zipped.writestr("01.jpg", jpeg("1"))
        zipped.writestr("02.jpg", jpeg("damaged"))
    damaged = content.getvalue().replace(b"damaged", b"DAMAGED", 1)
    result = await send(hass, flow_id, {"photo": upload(damaged)})
    assert await learn(hass, flow_id, result) == summary(["01.jpg"], unreadable=["02.jpg"])


async def test_an_unreachable_engine_stops_the_batch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    engine(aioclient_mock, exc=ClientError("refused"))
    zipped = archive(("01.jpg", jpeg("1")), ("02.jpg", jpeg("2")), ("03.jpg", jpeg("3")))
    result = await send(hass, flow_id, {"photo": upload(zipped)})
    assert await learn(hass, flow_id, result) == summary(
        [], not_added=["01.jpg", "02.jpg", "03.jpg"]
    )
    analyzed = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert len(analyzed) == 1


async def test_a_full_pet_takes_no_more_of_a_batch(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.pet_recognition.config_flow.EXAMPLE_LIMIT", 1)
    entry = await set_up(hass, aioclient_mock)
    seen = photos(aioclient_mock)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    zipped = archive(("01.jpg", jpeg("1")), ("02.jpg", jpeg("2")))
    result = await send(hass, flow_id, {"photo": upload(zipped)})
    assert await learn(hass, flow_id, result) == summary(["01.jpg"], not_added=["02.jpg"])
    assert seen == [jpeg("1")]


async def test_closing_the_flow_stops_the_batch(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    upload: Upload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await set_up(hass, aioclient_mock)
    started = asyncio.Event()

    async def analyze(method: str, url: yarl.URL, data: bytes) -> AiohttpClientMockResponse:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover

    engine(aioclient_mock, side_effect=analyze)
    closed: list[bool] = []
    close = zipfile.ZipFile.close

    def recorded(self: zipfile.ZipFile) -> None:
        if self.mode == "r":
            closed.append(True)
        close(self)

    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    monkeypatch.setattr(zipfile.ZipFile, "close", recorded)
    result = await send(hass, flow_id, {"photo": upload(archive(("01.jpg", jpeg("1"))))})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await started.wait()
    assert not closed
    hass.config_entries.subentries.async_abort(flow_id)
    await hass.async_block_till_done()
    assert closed
    assert not entry.runtime_data.examples.gallery.pets()


def test_the_folders_offered_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "01.jpg").write_bytes(jpeg("1"))
    (tmp_path / "02.jpg").write_bytes(jpeg("2"))
    dirs = {"local": str(tmp_path), "other": str(tmp_path / "b")}
    assert media_folders(dirs) == {
        "local/.": "local: / (1)",
        "local/a": "local: a (1)",
        "local/b": "local: b (1)",
        "other/.": "other: / (1)",
    }
    monkeypatch.setattr(batches, "FOLDER_LIMIT", 2)
    assert media_folders(dirs) == {"local/.": "local: / (1)", "local/a": "local: a (1)"}


def test_a_folder_stays_in_the_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "media" / "a").mkdir(parents=True)
    (tmp_path / "media" / "a" / "01.jpg").write_bytes(jpeg("1"))
    (tmp_path / "02.jpg").write_bytes(jpeg("2"))
    dirs = {"local": str(tmp_path / "media")}
    assert [item.name for item in folder_batch(dirs, "local/a", 1000).items] == ["01.jpg"]
    for value in ("local/..", "local/a/../..", "other/a", "local/a/01.jpg"):
        with pytest.raises(PictureError) as raised:
            folder_batch(dirs, value, 1000)
        assert raised.value.reason == "folder_not_found"

    # A folder or a photo that cannot be read.
    def refused(self: Path, *args: object) -> None:
        raise PermissionError(self)

    batch = folder_batch(dirs, "local/a", 1000)
    monkeypatch.setattr(Path, "iterdir", refused)
    assert media_folders(dirs) == {}
    with pytest.raises(PictureError) as raised:
        folder_batch(dirs, "local/a", 1000)
    assert raised.value.reason == "folder_not_found"
    monkeypatch.setattr(Path, "open", refused)
    with pytest.raises(PictureError) as raised:
        batch.items[0].read()
    assert raised.value.reason == "unreadable_picture"


def test_a_summary_shows_some_names_of_each_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batches, "SHOWN_NAMES", 2)
    found = Summary(added=["a.jpg", "b.jpg", "c.jpg"])
    found.skip("<d>.jpg", "engine_busy")
    placeholders = found.placeholders()
    assert placeholders["added"] == "3"
    assert placeholders["added_names"] == "a.jpg, b.jpg, …"
    assert placeholders["not_added_names"] == "\\<d\\>.jpg"


@pytest.mark.parametrize(
    ("data", "photo"),
    [
        (jpeg("1"), True),
        (PNG, True),
        (WEBP, True),
        (b"RIFF\x00\x00\x00\x00WAVE", False),
        (b"GIF89a", False),
        (b"", False),
    ],
)
def test_a_photo_is_told_by_its_content(data: bytes, photo: bool) -> None:
    assert batches.is_image(data) is photo


async def test_a_photo_too_large_is_refused_before_it_is_read(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, upload: Upload
) -> None:
    entry = await set_up(hass, aioclient_mock)
    photos(aioclient_mock)
    coordinator = entry.runtime_data.coordinator
    coordinator.data = replace(coordinator.data, max_bytes=10)
    flow_id = await start(hass, entry)
    await choose(hass, flow_id, "photo")
    zipped = archive(("01.jpg", jpeg("1")), ("02.jpg", jpeg("2") * 3))
    result = await send(hass, flow_id, {"photo": upload(zipped)})
    assert await learn(hass, flow_id, result) == summary(["01.jpg"], unreadable=["02.jpg"])
