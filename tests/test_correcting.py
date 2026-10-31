"""Tests of the corrections by hand: the select of the latest run, the visits to
review, the `assign_visit` action, and the visits announced again."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN
from custom_components.pet_recognition.core.matching import OTHER_ANIMAL
from custom_components.pet_recognition.examples import Origin

from .answers import URL, animal, embedding
from .asking import PET_C, UNSURE, Phones, asking, visit
from .test_counts import ENDS, announced, come, until  # noqa: F401
from .watching import FEEDER, PET_A, PET_B, START, Shown, journal, pet, settle, source, source_id

SELECT = "select.feeder_correct_last_visit"
TO_REVIEW = "sensor.feeder_visits_to_review"
MEALS_B = "sensor.feeder_pet_b_meals_today"


def option(hass: HomeAssistant) -> str:
    state = hass.states.get(SELECT)
    assert state is not None
    return state.state


def to_review(hass: HomeAssistant) -> tuple[str, str | None]:
    state = hass.states.get(TO_REVIEW)
    assert state is not None
    return state.state, state.attributes["latest_visit_id"]


async def choose(hass: HomeAssistant, choice: str) -> None:
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": choice}, blocking=True
    )
    await settle(hass)


def device(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """The device of the pet or source of `key`, a pet key or a subentry ID."""
    (subentry,) = (
        subentry
        for subentry_id, subentry in entry.subentries.items()
        if key in (subentry.unique_id, subentry_id)
    )
    identifier = (DOMAIN, f"{entry.entry_id}_{subentry.subentry_id}")
    (found,) = (
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if identifier in device.identifiers
    )
    return found.id


async def assign(
    hass: HomeAssistant, user: User | None = None, **data: object
) -> dict[str, Any] | None:
    response = await hass.services.async_call(
        DOMAIN,
        "assign_visit",
        data,
        blocking=True,
        return_response=True,
        context=Context(user_id=None if user is None else user.id),
    )
    await settle(hass)
    return response


def learned(entry: MockConfigEntry) -> list[tuple[str, Origin]]:
    store = entry.runtime_data.examples
    return [
        (example.pet, store.origin(example.id)) for example in store.gallery if not example.uploaded
    ]


async def test_the_select_corrects_the_latest_run(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    announced: list[dict[str, Any]],  # noqa: F811
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    state = hass.states.get(SELECT)
    assert state is not None
    assert state.attributes["options"] == [
        "Pet a",
        "Pet b",
        "Pet c",
        "unknown_animal",
        "other_animal",
    ]
    # The first picture of the feeder, without an animal, is no visit.
    assert option(hass) == "unknown"
    assert to_review(hass) == ("0", None)
    with pytest.raises(ServiceValidationError) as raised:
        await choose(hass, "Pet b")
    assert raised.value.translation_key == "no_visit"

    await visit(hass, aioclient_mock, shown, freezer, 1)
    (record,) = journal(entry).of(source_id(entry))
    assert option(hass) == "unknown_animal"
    assert to_review(hass) == ("1", record.id)
    assert hass.states.get(MEALS_B).state == "0"

    await choose(hass, "Pet b")
    # Corrected, the picture keeps no embedding, so the next corrections embed its crop.
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*UNSURE))
    assert (record.pets, record.corrected) == ((PET_B,), True)
    assert learned(entry) == [(PET_B, Origin.CORRECTION)]
    assert option(hass) == "Pet b"
    assert to_review(hass) == ("0", None)
    assert hass.states.get(MEALS_B).state == "1"
    # Announced when it ended, then once corrected.
    assert [event["event_type"] for event in announced] == ["visit_ended", "visit_corrected"]
    assert (announced[0]["pets"], announced[0]["unsure"]) == ([], True)
    assert (announced[1]["pets"], announced[1]["unsure"]) == (["Pet b"], False)
    assert announced[1]["visit_id"] == record.id
    state = hass.states.get("event.feeder_visit")
    assert state is not None
    assert state.attributes["event_type"] == "visit_corrected"

    await choose(hass, "other_animal")
    assert record.pets == (OTHER_ANIMAL,)
    assert learned(entry) == [(OTHER_ANIMAL, Origin.CORRECTION)]
    assert option(hass) == "other_animal"
    assert announced[-1]["other_animal"] is True

    # Corrected to unknown, it is no longer to review.
    await choose(hass, "unknown_animal")
    assert (record.pets, record.corrected) == ((), True)
    assert learned(entry) == []
    assert option(hass) == "unknown_animal"
    assert to_review(hass) == ("0", None)
    assert len(announced) == 4


async def test_a_visit_corrected_before_it_ends_is_announced_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    announced: list[dict[str, Any]],  # noqa: F811
) -> None:
    await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    # A new visit, still going on.
    await come(hass, aioclient_mock, shown, freezer, 20, animal(embedding=UNSURE))
    await choose(hass, "Pet c")
    assert [event["event_type"] for event in announced] == ["visit_ended"]
    await until(hass, freezer, START + timedelta(minutes=20) + ENDS)
    assert [event["event_type"] for event in announced] == ["visit_ended", "visit_ended"]
    assert announced[-1]["pets"] == ["Pet c"]


async def test_the_select_offers_the_pets_of_its_source(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(
        hass,
        aioclient_mock,
        shown,
        freezer,
        phones,
        pet(PET_A),
        pet(PET_B),
        pet(PET_C, species="dog"),
        source(FEEDER, pets=[PET_A, PET_B]),
        phone_ids=[],
    )
    state = hass.states.get(SELECT)
    assert state is not None
    assert state.attributes["options"] == ["Pet a", "Pet b", "unknown_animal", "other_animal"]
    await visit(hass, aioclient_mock, shown, freezer, 1)
    (record,) = journal(entry).of(source_id(entry))
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*UNSURE))

    # A pet not expected there, or several pets, show no option.
    await assign(hass, visit_id=record.id, pets=[device(hass, entry, PET_C)])
    assert option(hass) == "unknown"
    await assign(hass, source=SELECT, pets=[device(hass, entry, PET_A), device(hass, entry, PET_B)])
    assert record.pets == (PET_A, PET_B)
    assert option(hass) == "unknown"


async def test_assign_visit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_admin_user: User,
    announced: list[dict[str, Any]],  # noqa: F811
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    await visit(hass, aioclient_mock, shown, freezer, 20)
    first, second = journal(entry).of(source_id(entry))
    assert to_review(hass) == ("2", second.id)
    aioclient_mock.post(f"{URL}/v1/embed", json=embedding(*UNSURE))

    # The first visit, by its ID, to Pet B and the other animal.
    response = await assign(
        hass,
        hass_admin_user,
        visit_id=first.id,
        pets=device(hass, entry, PET_B),
        other_animal=True,
    )
    assert response == {
        "visit": {
            "visit_id": first.id,
            "pets": ["Pet b"],
            "other_animal": True,
            "unsure": False,
            "meal": None,
            "start": (START + timedelta(minutes=1)).isoformat(),
            "end": (START + timedelta(minutes=1)).isoformat(),
            "pictures": 1,
        }
    }
    assert first.pets == (PET_B, OTHER_ANIMAL)
    # Several pets teach nothing.
    assert learned(entry) == []
    assert to_review(hass) == ("1", second.id)
    assert announced[-1]["event_type"] == "visit_corrected"

    # The latest visit, by an entity of its source, to Pet C, without an answer.
    assert (
        await hass.services.async_call(
            DOMAIN,
            "assign_visit",
            {"source": TO_REVIEW, "pets": [device(hass, entry, PET_C)]},
            blocking=True,
        )
        is None
    )
    await settle(hass)
    assert second.pets == (PET_C,)
    assert learned(entry) == [(PET_C, Origin.CORRECTION)]
    assert to_review(hass) == ("0", None)

    # By its ID and its source, to unknown.
    response = await assign(hass, visit_id=second.id, source=SELECT)
    assert (response["visit"]["pets"], response["visit"]["unsure"]) == ([], False)
    assert (second.pets, second.corrected) == ((), True)
    assert learned(entry) == []


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"visit_id": "f" * 32}, "unknown_visit"),
        ({"source": "sensor.pet_recognition_pending_pictures"}, "unknown_source"),
        ({"source": "image.not_registered"}, "unknown_source"),
        ({"source": "sensor.feeder_visits_to_review", "visit_id": "f" * 32}, "unknown_visit"),
        ({"source": "sensor.feeder_visits_to_review", "pets": ["not a device"]}, "unknown_pet"),
        ({"source": "sensor.feeder_visits_to_review", "pets": ["source"]}, "unknown_pet"),
    ],
)
async def test_assign_visit_refuses_what_it_cannot_find(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    data: dict[str, Any],
    key: str,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    if data.get("pets") == ["source"]:
        data = {**data, "pets": [device(hass, entry, source_id(entry))]}
    with pytest.raises(ServiceValidationError) as raised:
        await assign(hass, **data)
    assert raised.value.translation_key == key
    (record,) = journal(entry).of(source_id(entry))
    assert not record.corrected


async def test_assign_visit_needs_a_visit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    with pytest.raises(ServiceValidationError) as raised:
        await assign(hass, source=TO_REVIEW)
    assert raised.value.translation_key == "no_visit"
    # Neither a visit nor a source.
    with pytest.raises(vol.Invalid):
        await assign(hass, pets=[])
    # An entry that is not loaded has no visits to find.
    await visit(hass, aioclient_mock, shown, freezer, 1)
    (record,) = journal(entry).of(source_id(entry))
    await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    with pytest.raises(ServiceValidationError) as raised:
        await assign(hass, visit_id=record.id)
    assert raised.value.translation_key == "unknown_visit"


async def test_assign_visit_is_for_administrators(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_read_only_user: User,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    (record,) = journal(entry).of(source_id(entry))
    with pytest.raises(Unauthorized):
        await assign(hass, hass_read_only_user, visit_id=record.id)
    assert not record.corrected
