"""Tests of the corrections of a visit to a meal, or to no meal."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN
from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.journal import SAVE_DELAY, Journal

from .answers import URL, animal, embedding
from .asking import Phones
from .test_correcting import assign, device
from .test_counts import (  # noqa: F401
    ENDS,
    MEALS_A,
    MEALS_B,
    VISITS,
    A,
    B,
    announced,
    come,
    eat,
    meal_feeder,
    until,
    value,
)
from .test_overview import Ask, ask, subentry_id, three_visits  # noqa: F401
from .watching import PET_A, PET_B, START, Shown, journal, settle, source_id


async def three_meals(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> MockConfigEntry:
    """Pet A eats, then comes by twice, at a feeder with meal pictures."""
    entry = await meal_feeder(hass, aioclient_mock, shown, freezer)
    await come(hass, aioclient_mock, shown, freezer, 1, animal(embedding=A))
    await eat(hass, aioclient_mock, shown, freezer, 2, animal(embedding=A))
    await come(hass, aioclient_mock, shown, freezer, 20, animal(embedding=A))
    await come(hass, aioclient_mock, shown, freezer, 40, animal(embedding=A))
    await until(hass, freezer, START + timedelta(minutes=40) + ENDS)
    assert value(hass, MEALS_A) == ("1", 0)
    return entry


def visit_ids(entry: MockConfigEntry) -> list[str]:
    """The ID of each visit of the feeder, as the overview gives it."""
    return [visit.id for visit in entry.runtime_data.history.visits(source_id(entry))]


async def test_a_visit_is_corrected_to_a_meal_or_not(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    announced: list[dict[str, Any]],  # noqa: F811
    ask: Ask,  # noqa: F811
    hass_storage: dict[str, Any],
) -> None:
    entry = await three_meals(hass, aioclient_mock, shown, freezer)
    meal, came_by, again = visit_ids(entry)
    announced.clear()

    # A meal only: the pets stay as they are, and are not corrected.
    answer = await ask("assign", visit_id=meal, meal=False)
    assert answer["success"]
    assert (answer["result"]["visit"]["meal"], answer["result"]["visit"]["pets"]) == (
        False,
        [subentry_id(entry, PET_A)],
    )
    records = journal(entry).of(source_id(entry))
    assert [(record.pets, record.corrected) for record in records[:2]] == [
        ((PET_A,), False),
        ((PET_A,), False),
    ]
    assert value(hass, MEALS_A) == ("0", 0)
    assert [(item["event_type"], item["meal"]) for item in announced] == [
        ("visit_corrected", False)
    ]

    answer = await ask("assign", visit_id=came_by, meal=True)
    assert answer["result"]["visit"]["meal"] is True
    assert value(hass, MEALS_A) == ("1", 0)

    # The pets and the meal together, which teaches Pet B.
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*B))
    answer = await ask("assign", visit_id=again, pets=[subentry_id(entry, PET_B)], meal=True)
    assert answer["result"]["visit"]["pets"] == [subentry_id(entry, PET_B)]
    await settle(hass)
    assert (value(hass, MEALS_A), value(hass, MEALS_B)) == (("1", 0), ("1", 0))
    assert value(hass, VISITS) == ("3", 0)

    # Saved, and read again as it was.
    await until(hass, freezer, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY + 1))
    rows = hass_storage[f"{DOMAIN}.{entry.entry_id}.journal"]["data"]["pictures"]
    assert [row["meal"] for row in rows] == [False, False, True, True]
    again_read = Journal(hass, entry.entry_id)
    await again_read.async_load()
    assert [record.meal for record in again_read.records] == [False, False, True, True]


async def test_a_meal_needs_a_source_that_counts_meals(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    ask: Ask,  # noqa: F811
) -> None:
    entry = await three_visits(hass, aioclient_mock, shown, freezer, phones)
    unsure = journal(entry).of(source_id(entry))[-3]
    answer = await ask("assign", visit_id=unsure.id, meal=True)
    assert (answer["error"]["code"], answer["error"]["translation_key"]) == (
        "home_assistant_error",
        "no_meals",
    )
    assert (unsure.corrected, unsure.meal) == (False, None)
    with pytest.raises(ServiceValidationError) as raised:
        await assign(hass, visit_id=unsure.id, meal=False)
    assert raised.value.translation_key == "no_meals"

    # Something to correct is needed.
    answer = await ask("assign", visit_id=unsure.id)
    assert answer["error"]["translation_key"] == "nothing_to_correct"
    with pytest.raises(vol.Invalid):
        await assign(hass, visit_id=unsure.id)
    assert not unsure.corrected

    # The other animal alone says who was there: no pet but it.
    answer = await ask("assign", visit_id=unsure.id, other_animal=True)
    assert answer["result"]["visit"]["pets"] == []
    assert answer["result"]["visit"]["other_animal"] is True
    await settle(hass)
    assert unsure.pets == (OTHER_ANIMAL,)


async def test_the_action_corrects_a_meal(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await three_meals(hass, aioclient_mock, shown, freezer)
    meal, came_by, again = visit_ids(entry)

    # A meal only: the pets stay as they are.
    response = await assign(hass, visit_id=meal, meal=False)
    assert response is not None
    assert (response["visit"]["meal"], response["visit"]["pets"]) == (False, ["Pet a"])
    assert value(hass, MEALS_A) == ("0", 0)
    assert not journal(entry).of(source_id(entry))[0].corrected

    # The other animal alone, or the pets and the meal together.
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*B))
    response = await assign(hass, visit_id=came_by, other_animal=True)
    assert response is not None
    assert (response["visit"]["pets"], response["visit"]["other_animal"]) == ([], True)
    response = await assign(hass, visit_id=again, pets=[device(hass, entry, PET_B)], meal=True)
    assert response is not None
    assert (response["visit"]["pets"], response["visit"]["meal"]) == (["Pet b"], True)
    assert value(hass, MEALS_B) == ("1", 0)
