"""Tests of watching the image entities of the sources, and of analysing their pictures."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.image import Image
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics
from custom_components.pet_recognition.examples import Candidate, Origin, stored_vector
from custom_components.pet_recognition.files import new_id
from custom_components.pet_recognition.journal import (
    DATA_JOURNALS,
    SAVE_DELAY,
    Pending,
    Seen,
    Status,
)
from custom_components.pet_recognition.sources import FIRST_RETRY, LAST_RETRY, MAX_ATTEMPTS

from .answers import CROP, MODEL, URL, analysis, animal, error, unit
from .watching import (
    FEEDER,
    PET_A,
    PET_B,
    START,
    Shown,
    analyses,
    engine,
    journal,
    pet,
    set_up,
    settle,
    show,
    source,
    source_id,
    tick,
)

# The module, which the function `sources` of the integration hides.
sources_module = importlib.import_module("custom_components.pet_recognition.sources")


async def test_a_new_picture_is_analyzed_and_recorded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    # The picture shown when the source was added is not new.
    assert analyses(aioclient_mock) == []
    assert journal(entry).seen[FEEDER].state == START.isoformat()

    await show(hass, shown, b"a meal", 1)
    (call,) = analyses(aioclient_mock)
    assert call[1].query == {"min_area": "0.01", "limit": "5", "species": "cat"}
    assert call[2] == b"a meal"
    (record,) = journal(entry).of(source_id(entry))
    assert record.status is Status.ANALYZED
    assert record.taken == (START + timedelta(minutes=1)).timestamp()
    assert record.entity == FEEDER
    assert (record.pets, record.resembles) == ((PET_A,), PET_A)
    (detection,) = record.detections
    assert detection.vector is None
    assert detection.scores == {PET_A: pytest.approx(1)}
    assert journal(entry).crop_path(detection.crop).read_bytes() == CROP
    assert journal(entry).queue == []
    # The latest picture of the source is kept for its last picture entity.
    assert [path.name for path in journal(entry).frames.iterdir()] == [f"{record.id}.frame"]


async def test_only_new_pictures_are_taken(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    # Only the attributes change.
    hass.states.async_set(FEEDER, START.isoformat(), {"access_token": "new"})
    await settle(hass)
    # A new time, but the same picture.
    await show(hass, shown, b"first picture", 1)
    # Not available, then without a picture to take.
    hass.states.async_set(FEEDER, STATE_UNAVAILABLE)
    await settle(hass)
    del shown[FEEDER]
    hass.states.async_set(FEEDER, (START + timedelta(minutes=2)).isoformat())
    await settle(hass)
    assert analyses(aioclient_mock) == []
    assert journal(entry).records == []
    # The picture that could not be taken is taken at its next change.
    await show(hass, shown, b"a meal", 3)
    assert len(analyses(aioclient_mock)) == 1
    # A picture the source already has is not taken again.
    await show(hass, shown, b"another meal", 4)
    await show(hass, shown, b"a meal", 5)
    assert len(analyses(aioclient_mock)) == 2
    assert journal(entry).seen[FEEDER].state == (START + timedelta(minutes=5)).isoformat()


async def test_the_pictures_of_an_entity_are_taken_one_at_a_time(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    release = asyncio.Event()
    fetches: list[str] = []

    async def get(hass: HomeAssistant, entity_id: str, timeout: int = 10) -> Image:
        fetches.append(entity_id)
        await release.wait()
        return Image(content_type="image/jpeg", content=shown[entity_id])

    with patch("custom_components.pet_recognition.sources.async_get_image", get):
        shown[FEEDER] = b"a meal"
        hass.states.async_set(FEEDER, (START + timedelta(minutes=1)).isoformat())
        await hass.async_block_till_done()
        # Two more changes while the first picture is being taken.
        for minutes in (2, 3):
            hass.states.async_set(FEEDER, (START + timedelta(minutes=minutes)).isoformat())
            await hass.async_block_till_done()
        assert fetches == [FEEDER]
        release.set()
        await settle(hass)
    # The first picture, then the latest change, which shows it still.
    assert fetches == [FEEDER, FEEDER]
    (record,) = journal(entry).records
    assert record.taken == (START + timedelta(minutes=1)).timestamp()
    assert journal(entry).seen[FEEDER].state == (START + timedelta(minutes=3)).isoformat()


async def test_a_time_that_is_not_one_uses_the_change(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    shown[FEEDER] = b"a meal"
    hass.states.async_set(FEEDER, "not a time")
    await settle(hass)
    (record,) = journal(entry).records
    assert record.taken == hass.states.get(FEEDER).last_changed.timestamp()


async def test_nothing_is_taken_twice_across_restarts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    await show(hass, shown, b"a meal", 1)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert len(analyses(aioclient_mock)) == 1

    # A picture shown while the entry was not loaded is taken when it loads.
    assert await hass.config_entries.async_unload(entry.entry_id)
    shown[FEEDER] = b"another meal"
    hass.states.async_set(FEEDER, (START + timedelta(minutes=2)).isoformat())
    await settle(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert len(analyses(aioclient_mock)) == 2
    assert len(journal(entry).records) == 2


async def test_the_queue_and_what_was_seen_survive_a_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await set_up(hass, aioclient_mock, shown, pet(PET_A), source(FEEDER), connected=False)
    await show(hass, shown, b"a meal", 1)
    assert len(journal(entry).queue) == 1
    await tick(hass, freezer, SAVE_DELAY + 1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    # As after a restart: the journal is read from the store again.
    hass.data.pop(DATA_JOURNALS)
    engine(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert journal(entry).seen[FEEDER] == Seen(
        FEEDER, (START + timedelta(minutes=1)).isoformat(), journal(entry).records[0].sha256
    )
    assert len(analyses(aioclient_mock)) == 1
    assert journal(entry).queue == []


async def test_a_changed_source_watches_its_new_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    shown["image.bowl"] = b"an empty bowl"
    hass.states.async_set("image.bowl", START.isoformat())
    subentry = entry.subentries[source_id(entry)]
    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, "entities": ["image.bowl"]}
    )
    await settle(hass)
    await show(hass, shown, b"a meal", 1)
    assert analyses(aioclient_mock) == []
    await show(hass, shown, b"a meal at the bowl", 1, "image.bowl")
    (call,) = analyses(aioclient_mock)
    assert call[2] == b"a meal at the bowl"
    assert set(journal(entry).seen) == {"image.bowl"}


async def test_an_unknown_animal_keeps_its_embedding(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown, pet(PET_A), pet(PET_B), source(FEEDER))
    engine(aioclient_mock, json=analysis(animal("cat", unit(1, 1, 0))))
    await show(hass, shown, b"a meal", 1)
    (record,) = journal(entry).records
    # Pet B has no example yet: nothing can be decided.
    assert record.pets == ()
    assert record.resembles == PET_A
    (detection,) = record.detections
    assert detection.vector == stored_vector(unit(1, 1, 0))


async def test_several_animals_and_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown, pet(PET_A), source(FEEDER))
    store = entry.runtime_data.examples
    await store.async_add(OTHER_ANIMAL, Candidate(CROP, tuple(unit(0, 1, 0)), MODEL), Origin.ANSWER)
    engine(
        aioclient_mock,
        json=analysis(animal("cat", unit(1, 0, 0)), animal("cat", unit(0, 1, 0))),
    )
    await show(hass, shown, b"two cats", 1)
    engine(aioclient_mock, json=analysis())
    await show(hass, shown, b"an empty bowl", 2)
    two, none = journal(entry).of(source_id(entry))
    assert two.pets == (PET_A, OTHER_ANIMAL)
    assert two.resembles is None
    assert len(two.detections) == 2
    assert (none.pets, none.detections, none.resembles) == ((), [], None)
    assert len(list(journal(entry).crops.iterdir())) == 2


async def test_only_the_species_of_the_expected_pets_are_asked_for(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(
        hass,
        aioclient_mock,
        shown,
        pet(PET_A),
        pet(PET_B, "dog"),
        source(FEEDER, pets=[PET_B, "gone"]),
    )
    engine(aioclient_mock, json=analysis(animal("dog", unit(1, 0, 0))))
    await show(hass, shown, b"a dog", 1)
    (call,) = analyses(aioclient_mock)
    assert call[1].query["species"] == "dog"
    # Pet A is not expected here, and pet B has no example.
    (record,) = journal(entry).records
    assert (record.pets, record.resembles) == ((), None)


async def test_without_pets_every_species_is_asked_for(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    await set_up(hass, aioclient_mock, shown, source(FEEDER))
    await show(hass, shown, b"a cat", 1)
    (call,) = analyses(aioclient_mock)
    assert call[1].query["species"] == "bird,cat,dog"


@pytest.mark.parametrize(
    "analyze",
    [
        {"status": 422, "json": error("unreadable_image")},
        {"status": 413, "json": error("too_large")},
    ],
)
async def test_a_picture_the_engine_cannot_read_is_recorded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    analyze: dict[str, object],
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    engine(aioclient_mock, **analyze)
    await show(hass, shown, b"a meal", 1)
    (record,) = journal(entry).records
    assert record.status is Status.UNREADABLE
    assert journal(entry).queue == []


async def test_a_picture_too_large_for_the_engine_is_not_sent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    await show(hass, shown, b"x" * 1001, 1)
    assert analyses(aioclient_mock) == []
    (record,) = journal(entry).records
    assert record.status is Status.UNREADABLE


async def test_the_queue_waits_for_the_engine_longer_each_time(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sources_module, "MAX_ATTEMPTS", 100)
    entry = await set_up(hass, aioclient_mock, shown)
    # The engine answers its health check, but fails on the pictures.
    engine(aioclient_mock, status=500, json=error("internal_error"))
    await show(hass, shown, b"a meal", 1)
    await show(hass, shown, b"another meal", 2)
    assert len(analyses(aioclient_mock)) == 1
    assert len(journal(entry).queue) == 2
    await tick(hass, freezer, FIRST_RETRY - 1)
    assert len(analyses(aioclient_mock)) == 1
    await tick(hass, freezer, 1)
    assert len(analyses(aioclient_mock)) == 2
    # Twice as long the next time.
    await tick(hass, freezer, FIRST_RETRY * 2 - 1)
    assert len(analyses(aioclient_mock)) == 2
    await tick(hass, freezer, 1)
    assert len(analyses(aioclient_mock)) == 3
    # Not longer than the last delay.
    for _ in range(8):
        await tick(hass, freezer, LAST_RETRY)
    calls = len(analyses(aioclient_mock))
    await tick(hass, freezer, LAST_RETRY - 1)
    assert len(analyses(aioclient_mock)) == calls

    # The engine is back.
    engine(aioclient_mock)
    await tick(hass, freezer, LAST_RETRY)
    assert len(journal(entry).records) == 2
    assert journal(entry).queue == []


async def test_a_busy_engine_says_when_to_come_back(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    engine(aioclient_mock, status=503, json=error("busy"), headers={"Retry-After": "30"})
    await show(hass, shown, b"a meal", 1)
    engine(aioclient_mock)
    await tick(hass, freezer, 29)
    assert journal(entry).records == []
    await tick(hass, freezer, 1)
    assert len(journal(entry).records) == 1


@pytest.mark.parametrize(("retry_after", "delay"), [("3", 3), ("0.1", 1)])
async def test_a_busy_engine_does_not_make_the_queue_wait_longer(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    retry_after: str,
    delay: float,
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    engine(aioclient_mock, status=503, json=error("busy"), headers={"Retry-After": retry_after})
    delays: list[float] = []
    later = sources_module.async_call_later

    def recorded(
        hass: HomeAssistant, seconds: float, action: Callable[[datetime], None]
    ) -> CALLBACK_TYPE:
        delays.append(seconds)
        return later(hass, seconds, action)

    with patch.object(sources_module, "async_call_later", recorded):
        await show(hass, shown, b"a meal", 1)
        for _ in range(3):
            await tick(hass, freezer, delay)
    # At least a second, and no longer each time.
    assert delays == [delay] * 4
    assert len(analyses(aioclient_mock)) == 4
    assert journal(entry).queue[0].attempts == 0


async def test_the_health_check_wakes_the_queue_up(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    engine(aioclient_mock, status=500, json=error("internal_error"))
    await show(hass, shown, b"a meal", 1)
    coordinator = entry.runtime_data.coordinator
    # Still answering its health check: the picture waits its turn.
    await coordinator.async_refresh()
    await settle(hass)
    assert len(analyses(aioclient_mock)) == 1
    # Down when the picture is sent, then back.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", exc=ClientError("refused"))
    aioclient_mock.post(f"{URL}/v1/analyze", exc=ClientError("refused"))
    await show(hass, shown, b"another meal", 2)
    await coordinator.async_refresh()
    assert journal(entry).records == []
    engine(aioclient_mock)
    await coordinator.async_refresh()
    await settle(hass)
    assert len(journal(entry).records) == 2


async def test_a_picture_waits_while_the_engine_is_down(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", exc=ClientError("refused"))
    aioclient_mock.post(f"{URL}/v1/analyze", exc=ClientError("refused"))
    await show(hass, shown, b"a meal", 1)
    for _ in range(MAX_ATTEMPTS + 1):
        await tick(hass, freezer, LAST_RETRY)
    # Down is not a failure of the picture.
    assert journal(entry).queue[0].attempts == 0
    assert len(analyses(aioclient_mock)) == 1
    engine(aioclient_mock)
    await tick(hass, freezer, 60)
    assert journal(entry).records[0].status is Status.ANALYZED


async def test_the_queue_waits_while_the_engine_is_away(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown, source(FEEDER), connected=False)
    await show(hass, shown, b"a meal", 1)
    assert analyses(aioclient_mock) == []
    assert len(journal(entry).queue) == 1
    engine(aioclient_mock)
    await entry.runtime_data.coordinator.async_refresh()
    await settle(hass)
    assert len(journal(entry).records) == 1


@pytest.mark.parametrize(
    "analyze",
    [
        {"status": 400, "json": error("invalid_request")},
        {"json": {"animals": "none"}},
        {"status": 500, "json": error("internal_error")},
        {"exc": TimeoutError()},
    ],
)
async def test_a_picture_the_engine_keeps_failing_on_is_given_up(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    analyze: dict[str, object],
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    engine(aioclient_mock, **analyze)
    await show(hass, shown, b"a meal", 1)
    for attempt in range(1, MAX_ATTEMPTS):
        assert journal(entry).queue[0].attempts == attempt
        await tick(hass, freezer, LAST_RETRY)
    (record,) = journal(entry).records
    assert record.status is Status.FAILED
    assert "given up" in caplog.text


async def test_a_refused_key_makes_the_queue_wait(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", status=401, json=error("unauthorized"))
    aioclient_mock.post(f"{URL}/v1/analyze", status=401, json=error("unauthorized"))
    await show(hass, shown, b"a meal", 1)
    assert len(journal(entry).queue) == 1
    flows = hass.config_entries.flow.async_progress_by_handler("pet_recognition")
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_waiting_pictures_of_a_source_that_is_gone_are_dropped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    picture = Pending(new_id(), "gone", FEEDER, 1, "f" * 64)
    await journal(entry).async_add_pending(picture, b"a meal", Seen(FEEDER, "1", "f" * 64))
    entry.runtime_data.analyzer.async_wake()
    await settle(hass)
    assert journal(entry).queue == []
    assert journal(entry).records == []


async def test_a_waiting_picture_without_its_file_is_dropped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    engine(aioclient_mock, exc=ClientError("refused"))
    await show(hass, shown, b"a meal", 1)
    journal(entry).frame_path(journal(entry).queue[0].id).unlink()
    engine(aioclient_mock)
    await tick(hass, freezer, FIRST_RETRY)
    assert journal(entry).queue == []
    assert journal(entry).records == []
    assert "is missing" in caplog.text


async def test_old_pictures_are_purged_every_hour(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    entry = await set_up(hass, aioclient_mock, shown)
    await show(hass, shown, b"a meal", 1)
    (record,) = journal(entry).records
    crop = journal(entry).crop_path(record.detections[0].crop)
    await tick(hass, freezer, timedelta(days=7).total_seconds())
    assert journal(entry).records == [record]
    # The latest picture of a source or a pet stays past the limits.
    await tick(hass, freezer, timedelta(hours=1).total_seconds())
    assert journal(entry).records == [record]
    await show(hass, shown, b"another meal", timedelta(days=7, hours=1).total_seconds() / 60)
    (_, latest) = journal(entry).records
    await tick(hass, freezer, timedelta(hours=1).total_seconds())
    assert journal(entry).records == [latest]
    assert not crop.exists()
    assert not journal(entry).frame_path(record.id).exists()


async def test_a_source_removed_leaves_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    await show(hass, shown, b"a meal", 1)
    assert hass.config_entries.async_remove_subentry(entry, source_id(entry))
    await settle(hass)
    assert journal(entry).records == []
    assert journal(entry).seen == {}
    assert not any(journal(entry).crops.iterdir())
    # Its entity is not followed any more.
    await show(hass, shown, b"another meal", 2)
    assert len(analyses(aioclient_mock)) == 1


async def test_the_diagnostics_count_the_pictures_of_each_source(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    await show(hass, shown, b"a meal", 1)
    await show(hass, shown, b"x" * 1001, 2)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["sources"] == [
        {
            "source": "source_1",
            "kind": "feeder",
            "images": 1,
            "cameras": 0,
            "trigger": False,
            "pictures": {"analyzed": 1, "unreadable": 1},
            "pending": 0,
            "visits_today": 0,
        }
    ]
    assert diagnostics["pending"] == 0
    assert FEEDER not in str(diagnostics)


async def test_deleting_the_entry_deletes_its_journal(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    hass_storage: dict[str, Any],
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    await show(hass, shown, b"a meal", 1)
    directory = journal(entry).crops.parent
    assert await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    assert not directory.exists()
    assert f"pet_recognition.{entry.entry_id}.journal" not in hass_storage


@pytest.mark.parametrize("stop", [True, False])
async def test_waiting_to_try_again_ends_with_the_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, shown: Shown, stop: bool
) -> None:
    entry = await set_up(hass, aioclient_mock, shown)
    engine(aioclient_mock, exc=ClientError("refused"))
    await show(hass, shown, b"a meal", 1)
    waiting = journal(entry)
    # No timer is left behind, which the test harness would report.
    if stop:
        await hass.async_stop()
    else:
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert len(waiting.queue) == 1
