"""Tests of the journal of the pictures, and of their files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pet_recognition.const import PICTURE_LIMIT, QUEUE_LIMIT
from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.examples import encode_vector, stored_vector
from custom_components.pet_recognition.files import new_id
from custom_components.pet_recognition.journal import (
    SAVE_DELAY,
    VECTOR_LIMIT,
    Detection,
    Journal,
    Pending,
    Record,
    Seen,
    Status,
    async_get_journal,
    async_remove_journal,
)

from .answers import CROP, MODEL, unit

ENTRY = "entry1"
KEY = f"pet_recognition.{ENTRY}.journal"
PICTURE = b"a picture"


async def loaded(hass: HomeAssistant) -> Journal:
    journal = Journal(hass, ENTRY)
    await journal.async_load()
    return journal


def pending(source: str = "source", taken: float = 100, sha256: str = "f" * 64) -> Pending:
    return Pending(new_id(), source, "image.feeder", taken, sha256)


async def add(journal: Journal, item: Pending, picture: bytes = PICTURE) -> None:
    await journal.async_add_pending(item, picture, Seen(item.entity, str(item.taken), item.sha256))


async def analyzed(
    journal: Journal, item: Pending, done: Record, crops: list[tuple[str, bytes]] | None = None
) -> None:
    """Queue a picture, then record it."""
    await journal.async_add_pending(item, PICTURE)
    await journal.async_record(item, done, crops or [])


def record(item: Pending, *pets: str, vector: bool = False) -> Record:
    return Record(
        id=item.id,
        source=item.source,
        entity=item.entity,
        taken=item.taken,
        sha256=item.sha256,
        status=Status.ANALYZED,
        detections=[
            Detection(
                species="cat",
                confidence=0.9,
                area=0.2,
                crop=new_id(),
                model=MODEL,
                scores={"pet_a": 0.9, "pet_b": 0.2},
                vector=stored_vector(unit(1, 0)) if vector else None,
            )
        ],
        pets=pets,
        resembles="pet_a",
    )


def names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()} if directory.is_dir() else set()


async def saved(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let the delayed save happen."""
    freezer.tick(SAVE_DELAY + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_pictures_wait_and_become_records_across_restarts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    journal = await loaded(hass)
    journal.async_see(Seen("image.feeder", "2026-10-25T08:00:00+00:00", "f" * 64))
    first, second = pending(taken=200), pending(taken=100, sha256="e" * 64)
    await add(journal, first)
    await add(journal, second)
    assert journal.seen["image.feeder"] == Seen("image.feeder", "100", "e" * 64)
    assert journal.known("source", "e" * 64)
    assert not journal.known("other", "e" * 64)
    assert await journal.async_read_frame(first.id) == PICTURE
    journal.async_retry(first)
    assert first.attempts == 1

    done = record(first, "pet_a")
    await journal.async_record(first, done, [(done.detections[0].crop, CROP)])
    await journal.async_record(second, record(second, vector=True))
    assert journal.queue == []
    # Only the latest analyzed picture of the source keeps its file.
    assert names(journal.frames) == {f"{first.id}.frame"}
    assert journal.last_picture("source") == done
    assert journal.last_of_pet("pet_a") == done
    assert journal.last_of_pet("pet_b") is None
    assert len(names(journal.crops)) == 1
    assert [item.id for item in journal.of("source")] == [second.id, first.id]
    assert journal.pending() == journal.pending("source") == []
    assert journal.known("source", "e" * 64)

    await saved(hass, freezer)
    again = await loaded(hass)
    assert again.seen == journal.seen
    assert again.records == journal.records
    assert again.of("source")[0].detections[0].vector == stored_vector(unit(1, 0))
    assert again.of("source")[1].pets == ("pet_a",)


async def test_a_picture_not_written_is_not_taken(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = await loaded(hass)

    def fail(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("custom_components.pet_recognition.journal.write_files", fail)
    with pytest.raises(OSError, match="disk full"):
        await add(journal, pending())
    assert (journal.seen, journal.queue) == ({}, [])


async def test_files_are_deleted_once_the_journal_names_them_no_more(
    hass: HomeAssistant, hass_storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = await loaded(hass)
    first, second = pending(taken=100), pending(taken=200, sha256="e" * 64)
    await add(journal, first)
    await add(journal, second)
    # The latest picture of the source keeps its file: nothing to delete yet.
    await journal.async_record(first, record(first))
    assert KEY not in hass_storage

    def deleted(paths: list[Path]) -> None:
        # Saved already, with no delay.
        assert [row["id"] for row in hass_storage[KEY]["data"]["pictures"]] == [
            first.id,
            second.id,
        ]
        assert hass_storage[KEY]["data"]["queue"] == []
        calls.append(paths)

    calls: list[list[Path]] = []
    monkeypatch.setattr("custom_components.pet_recognition.journal.delete_files", deleted)
    await journal.async_record(second, record(second))
    assert calls == [[journal.frame_path(first.id)]]


async def test_the_queue_survives_a_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    journal = await loaded(hass)
    item = pending()
    await add(journal, item)
    journal.async_retry(item)
    await saved(hass, freezer)
    again = await loaded(hass)
    assert again.queue == [item]
    assert await again.async_read_frame(item.id) == PICTURE


async def test_a_full_queue_drops_its_oldest_pictures(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    journal = await loaded(hass)
    items = [pending(taken=index) for index in range(QUEUE_LIMIT + 2)]
    for item in items:
        await add(journal, item)
    assert journal.queue == items[2:]
    assert names(journal.frames) == {f"{item.id}.frame" for item in items[2:]}
    assert "The queue of pictures for the engine is full" in caplog.text


async def test_a_picture_dropped_while_analyzed_is_not_recorded(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.pet_recognition.journal.QUEUE_LIMIT", 1)
    journal = await loaded(hass)
    last = pending(taken=1)
    done = record(last)
    await analyzed(journal, last, done, [(done.detections[0].crop, CROP)])
    analysed, newer = pending(taken=2), pending(taken=3)
    await add(journal, analysed)
    # A new picture comes while the engine has the oldest, from a full queue.
    await add(journal, newer)
    late = record(analysed)
    await journal.async_record(analysed, late, [(late.detections[0].crop, CROP)])
    assert journal.records == [done]
    assert journal.queue == [newer]
    assert journal.last_picture("source") == done
    assert names(journal.frames) == {f"{last.id}.frame", f"{newer.id}.frame"}
    assert names(journal.crops) == {f"{done.detections[0].crop}.jpg"}


async def test_a_dropped_picture_loses_its_file(hass: HomeAssistant) -> None:
    journal = await loaded(hass)
    item = pending()
    await add(journal, item)
    await journal.async_drop(item)
    await journal.async_drop(item)
    assert journal.queue == []
    assert names(journal.frames) == set()


async def test_each_source_keeps_a_limited_number_of_pictures(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.pet_recognition.journal.PICTURE_LIMIT", 2)
    journal = await loaded(hass)
    kept: list[Record] = []
    for taken in range(3):
        item = pending(taken=taken)
        done = record(item, "pet_a")
        await analyzed(journal, item, done, [(done.detections[0].crop, CROP)])
        kept.append(done)
    other = pending("other")
    await analyzed(journal, other, record(other))
    assert journal.of("source") == kept[1:]
    assert len(journal.of("other")) == 1
    assert names(journal.crops) == {f"{done.detections[0].crop}.jpg" for done in kept[1:]}
    assert PICTURE_LIMIT >= 1000


async def test_only_the_latest_unknown_pictures_keep_their_embeddings(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.pet_recognition.journal.VECTOR_LIMIT", 2)
    journal = await loaded(hass)
    items = [pending(taken=taken) for taken in range(3)]
    for item in items:
        await analyzed(journal, item, record(item, vector=True))
    known = pending(taken=4)
    await analyzed(journal, known, record(known, "pet_a"))
    other = pending("other")
    await analyzed(journal, other, record(other, vector=True))
    await saved(hass, freezer)
    vectors = {
        row["id"]: row["detections"][0]["vector"] is not None
        for row in hass_storage[KEY]["data"]["pictures"]
    }
    assert vectors == {
        items[0].id: False,
        items[1].id: True,
        items[2].id: True,
        known.id: False,
        other.id: True,
    }
    assert VECTOR_LIMIT >= 100


async def test_old_pictures_are_purged(hass: HomeAssistant) -> None:
    journal = await loaded(hass)
    old, new = pending(taken=10), pending(taken=20)
    for item in (old, new):
        done = record(item)
        await analyzed(journal, item, done, [(done.detections[0].crop, CROP)])
    await journal.async_purge(15)
    await journal.async_purge(15)
    assert [item.id for item in journal.records] == [new.id]
    assert len(names(journal.crops)) == 1


async def test_what_the_history_protects_stays(hass: HomeAssistant) -> None:
    journal = await loaded(hass)
    items = [pending(taken=taken) for taken in (10, 11, 12, 20)]
    for item in items:
        await analyzed(journal, item, record(item))
    asked: list[float | None] = []

    def protect(before: float | None) -> set[str]:
        asked.append(before)
        return {items[0].id}

    stop = journal.async_protect(protect)
    await journal.async_purge(15)
    assert asked == [15]
    assert [item.id for item in journal.records] == [items[0].id, items[3].id]
    stop()
    await journal.async_purge(15)
    assert [item.id for item in journal.records] == [items[3].id]


async def test_what_is_gone_is_forgotten(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = await loaded(hass)
    journal.async_see(Seen("image.kept", "1", "a" * 64))
    journal.async_see(Seen("image.gone", "1", "b" * 64))
    waiting, gone = pending(), pending("gone")
    await add(journal, waiting)
    await add(journal, gone)
    seen = pending(taken=1)
    await analyzed(journal, seen, record(seen, "pet_a", "pet_b"))
    lost = pending("gone")
    await analyzed(journal, lost, record(lost))
    other = pending(taken=2)
    other_record = record(other, OTHER_ANIMAL)
    await analyzed(journal, other, other_record)

    await journal.async_keep({"source": ["image.kept"]}, {"pet_a"})
    assert set(journal.seen) == {"image.kept"}
    assert journal.queue == [waiting]
    # The last picture of the source left keeps its frame.
    assert names(journal.frames) == {f"{waiting.id}.frame", f"{other.id}.frame"}
    (kept, kept_other) = journal.of("source")
    assert kept.pets == ("pet_a",)
    assert kept.detections[0].scores == {"pet_a": 0.9}
    assert kept_other.pets == (OTHER_ANIMAL,)
    assert journal.of("gone") == []
    await saved(hass, freezer)
    rows = hass_storage[KEY]["data"]["pictures"]
    assert [row["detections"][0]["scores"] for row in rows] == [{"pet_a": 0.9}] * 2

    # The pet that a picture resembles is gone.
    await journal.async_keep({"source": ["image.kept"]}, set())
    assert kept.resembles is None
    assert kept.pets == ()

    calls: list[None] = []
    monkeypatch.setattr(journal, "_save", lambda: calls.append(None))
    await journal.async_keep({"source": ["image.kept"]}, set())
    assert calls == []


async def test_files_named_by_nothing_are_deleted_each_time_the_journal_is_got(
    hass: HomeAssistant,
) -> None:
    journal = await async_get_journal(hass, ENTRY)
    item = pending()
    await add(journal, item)
    done_item = pending()
    done = record(done_item)
    await analyzed(journal, done_item, done, [(done.detections[0].crop, CROP)])
    for _ in range(2):
        (journal.frames / f"{new_id()}.frame").write_bytes(PICTURE)
        (journal.frames / "left.tmp").write_bytes(PICTURE)
        (journal.crops / f"{new_id()}.jpg").write_bytes(CROP)
        assert await async_get_journal(hass, ENTRY) is journal
        # The source's last picture keeps its frame.
        assert names(journal.frames) == {f"{item.id}.frame", f"{done_item.id}.frame"}
        assert names(journal.crops) == {f"{done.detections[0].crop}.jpg"}


def stored(data: object) -> dict[str, object]:
    return {"version": 1, "minor_version": 1, "key": KEY, "data": data}


def good_record(**changes: object) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "a" * 32,
        "source": "source",
        "entity": "image.feeder",
        "taken": 1.5,
        "sha256": "f" * 64,
        "status": "analyzed",
        "pets": ["pet_a"],
        "resembles": None,
        "detections": [
            {
                "species": "cat",
                "confidence": 0.5,
                "area": 0.1,
                "crop": "b" * 32,
                "model": MODEL,
                "scores": {"pet_a": 0.7},
                "vector": encode_vector(unit(1, 0)),
            }
        ],
    }
    value.update(changes)
    return value


def bad_detection(**changes: object) -> dict[str, Any]:
    detection = {**good_record()["detections"][0], **changes}
    return good_record(id="c" * 32, detections=[detection])


async def test_unreadable_records_are_ignored(
    hass: HomeAssistant, hass_storage: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    good_pending = {
        "id": "d" * 32,
        "source": "source",
        "entity": "image.feeder",
        "taken": 3,
        "sha256": "f" * 64,
        "attempts": 1,
    }
    hass_storage[KEY] = stored(
        {
            "seen": [
                {"entity": "image.feeder", "state": "1", "sha256": "f" * 64},
                {"entity": "image.other", "state": "", "sha256": "f" * 64},
                "not a record",
            ],
            "queue": [
                good_pending,
                {**good_pending, "id": "../x"},
                {**good_pending, "attempts": -1},
                {**good_pending, "attempts": True},
                {**good_pending, "taken": "now"},
                {**good_pending, "taken": True},
            ],
            "pictures": [
                good_record(),
                good_record(resembles="pet_a", id="e" * 32),
                good_record(pets="pet_a"),
                good_record(detections={}),
                good_record(resembles=7),
                good_record(status="lost"),
                bad_detection(scores=[]),
                bad_detection(scores={"pet_a": "high"}),
                bad_detection(vector="not base64!"),
                bad_detection(crop="../../x"),
                bad_detection(species=None),
            ],
        }
    )
    journal = await loaded(hass)
    assert set(journal.seen) == {"image.feeder"}
    assert [item.id for item in journal.queue] == ["d" * 32]
    assert [item.id for item in journal.records] == ["a" * 32, "e" * 32]
    assert journal.records[1].resembles == "pet_a"
    assert caplog.text.count("cannot be read, and is ignored") == 2 + 5 + 9


@pytest.mark.parametrize("data", [["a list"], {"seen": {}, "queue": "none", "pictures": 7}])
async def test_an_unreadable_journal_is_ignored(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    data: object,
) -> None:
    hass_storage[KEY] = stored(data)
    journal = await loaded(hass)
    assert (journal.seen, journal.queue, journal.records) == ({}, [], [])
    assert "cannot be read, and" in caplog.text


@pytest.mark.parametrize("file_id", ["../../secrets", "A" * 32, "0" * 31])
def test_file_paths_are_only_for_ids(hass: HomeAssistant, file_id: str) -> None:
    journal = Journal(hass, ENTRY)
    with pytest.raises(ValueError, match="not the ID of a file"):
        journal.frame_path(file_id)
    with pytest.raises(ValueError, match="not the ID of a file"):
        journal.crop_path(file_id)


async def test_one_journal_per_entry_until_it_is_removed(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    journal = await async_get_journal(hass, ENTRY)
    assert await async_get_journal(hass, ENTRY) is journal
    journal.async_see(Seen("image.feeder", "1", "f" * 64))
    await saved(hass, freezer)
    assert KEY in hass_storage
    await async_remove_journal(hass, ENTRY)
    assert KEY not in hass_storage
    assert await async_get_journal(hass, ENTRY) is not journal
    await async_remove_journal(hass, "never-set-up")


async def test_the_visits_announced_are_kept_across_restarts(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    journal = await loaded(hass)
    journal.async_announce("source", 100)
    journal.async_announce("gone", 50)
    await saved(hass, freezer)
    assert hass_storage[KEY]["data"]["announced"] == {"source": 100, "gone": 50}
    assert (await loaded(hass)).announced == {"source": 100, "gone": 50}

    await journal.async_keep({"source": ["image.feeder"]}, set())
    assert journal.announced == {"source": 100}

    hass_storage[KEY] = stored({"announced": {"source": 7, "late": "now", "never": True}})
    assert (await loaded(hass)).announced == {"source": 7}
    hass_storage[KEY] = stored({"announced": ["source"]})
    assert (await loaded(hass)).announced == {}
    assert caplog.text.count("cannot be read, and") == 3
