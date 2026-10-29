"""Tests of the answers to the questions, from the buttons of the notifications."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.core.doubts import State
from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics
from custom_components.pet_recognition.journal import Journal
from custom_components.pet_recognition.phones import phone_users

from .asking import PET_C, A, Phones, asking, only_question, visit
from .test_counts import until
from .watching import PET_A, PET_B, Shown, journal, settle, source_id

ACTION = "mobile_app_notification_action"
MEALS_B = "sensor.feeder_pet_b_meals_today"


async def press(
    hass: HomeAssistant, entry: MockConfigEntry, choice: str, user: str | None = "user_1"
) -> None:
    """Press a button of the only question, as the companion app of `user` says."""
    action = f"PET_RECOGNITION|{only_question(entry).doubt.id}|{choice}"
    hass.bus.async_fire(ACTION, {"action": action}, context=Context(user_id=user))
    await settle(hass)


def rows(hass_storage: dict[str, Any], entry: MockConfigEntry) -> list[dict[str, Any]]:
    return hass_storage[f"pet_recognition.{entry.entry_id}.journal"]["data"]["pictures"]


async def unsure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> MockConfigEntry:
    """Pets A, B and C, and an unknown meal at the feeder, asked about."""
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    await visit(hass, aioclient_mock, shown, freezer, 1)
    assert only_question(entry).choices == (PET_B, PET_C)
    return entry


def first_record(entry: MockConfigEntry) -> tuple[tuple[str, ...], bool, str]:
    record = journal(entry).of(source_id(entry))[0]
    return record.pets, record.corrected, record.group


async def test_a_pet_corrects_the_visit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_storage: dict[str, Any],
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    # A meal of Pet A, then one nobody can tell.
    await visit(hass, aioclient_mock, shown, freezer, 1, A)
    await visit(hass, aioclient_mock, shown, freezer, 20)
    assert hass.states.get(MEALS_B).state == "0"
    asked = only_question(entry)
    await press(hass, entry, PET_B)

    record_id = asked.doubt.pictures[0]
    meal, answered = journal(entry).of(source_id(entry))
    assert (meal.pets, meal.corrected) == ((PET_A,), False)
    assert answered.id == record_id
    assert (answered.pets, answered.corrected, answered.group) == ((PET_B,), True, record_id)
    assert asked.doubt.state is State.SETTLED
    # Removed from every phone, the one that answered too.
    tag = f"pet_recognition_{asked.doubt.id}"
    assert phones.cleared() == [("Phone 1", tag), ("Phone 2", tag)]
    assert hass.states.get(MEALS_B).state == "1"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["sources"][0]["questions"] == {"settled": 1}

    # Saved, and read again.
    await until(hass, freezer, dt_util.utcnow() + timedelta(seconds=5))
    (row,) = (row for row in rows(hass_storage, entry) if row["id"] == record_id)
    assert (row["pets"], row["corrected"], row["group"]) == ([PET_B], True, record_id)
    again = Journal(hass, entry.entry_id)
    await again.async_load()
    (record,) = (record for record in again.records if record.id == record_id)
    assert (record.pets, record.corrected, record.group) == ((PET_B,), True, record_id)

    # A second answer, from the other phone, changes nothing.
    await press(hass, entry, PET_C, "user_2")
    assert answered.pets == (PET_B,)
    assert len(phones.cleared()) == 2


async def test_other_leaves_the_visit_to_review(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await unsure(hass, aioclient_mock, shown, freezer, phones)
    asked = only_question(entry)
    await press(hass, entry, "other", "user_2")
    assert asked.doubt.state is State.DISMISSED
    assert first_record(entry) == ((), False, "")
    tag = f"pet_recognition_{asked.doubt.id}"
    assert phones.cleared() == [("Phone 1", tag), ("Phone 2", tag)]
    assert hass.states.get(MEALS_B).state == "0"
    # Not asked again.
    await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert journal(entry).asked == [asked]
    assert len(phones.questions()) == 2


@pytest.mark.parametrize(
    ("event", "user", "warning"),
    [
        # The users of the phones asked only.
        ({"action": "PET_RECOGNITION|{id}|" + PET_B}, "user_3", "not asked"),
        ({"action": "PET_RECOGNITION|{id}|" + PET_B}, None, "not asked"),
        # The pets of the buttons only.
        ({"action": "PET_RECOGNITION|{id}|" + PET_A}, "user_1", "not offered"),
        ({"action": "PET_RECOGNITION|{id}|"}, "user_1", "not offered"),
        # Questions that are not open, or another entry's.
        ({"action": "PET_RECOGNITION|" + "f" * 32 + "|" + PET_B}, "user_1", None),
        ({"action": "PET_RECOGNITION|"}, "user_1", None),
        # The actions of other notifications.
        ({"action": "PET_RECOGNITION"}, "user_1", None),
        ({"action": "ANOTHER|{id}|" + PET_B}, "user_1", None),
        ({"action": 7}, "user_1", None),
        ({}, "user_1", None),
    ],
)
async def test_answers_that_do_not_count_are_ignored(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    caplog: pytest.LogCaptureFixture,
    event: dict[str, Any],
    user: str | None,
    warning: str | None,
) -> None:
    entry = await unsure(hass, aioclient_mock, shown, freezer, phones)
    asked = only_question(entry)
    data = {
        key: value.format(id=asked.doubt.id) if isinstance(value, str) else value
        for key, value in event.items()
    }
    hass.bus.async_fire(ACTION, data, context=Context(user_id=user))
    await settle(hass)
    assert asked.open
    assert first_record(entry) == ((), False, "")
    assert phones.cleared() == []
    if warning is not None:
        assert "is ignored" in caplog.text
        assert warning in caplog.text


async def test_an_answer_the_question_no_longer_needs_changes_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await unsure(hass, aioclient_mock, shown, freezer, phones)
    # As when its pictures were told since, before the question is settled.
    monkeypatch.setattr("custom_components.pet_recognition.questions.answer", lambda *_args: None)
    await press(hass, entry, PET_B)
    assert only_question(entry).open
    assert first_record(entry) == ((), False, "")


async def test_the_users_of_the_phones(hass: HomeAssistant, phones: Phones) -> None:
    registry = dr.async_get(hass)
    other = MockConfigEntry(domain="other", data={"user_id": "user_3"})
    other.add_to_hass(hass)
    unregistered = MockConfigEntry(domain="mobile_app", data={})
    unregistered.add_to_hass(hass)
    # A device of another integration, one of the mobile app without a user,
    # and one that is gone.
    devices = [
        registry.async_get_or_create(
            config_entry_id=config_entry.entry_id, identifiers={(config_entry.domain, "device")}
        ).id
        for config_entry in (other, unregistered)
    ]
    assert phone_users(hass, [*phones.ids.values(), *devices, "0" * 32]) == {"user_1", "user_2"}
    assert phone_users(hass, devices) == set()
