"""Tests of the questions asked on the phones when unsure."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.pet_recognition.const import DOMAIN
from custom_components.pet_recognition.core.doubts import Kind, State
from custom_components.pet_recognition.diagnostics import async_get_config_entry_diagnostics
from custom_components.pet_recognition.views import PICTURE_URL, signed_picture_url

from .answers import CROP, animal, unit
from .asking import (
    GONE_PHONE,
    LEARNING_EXPIRES,
    PET_C,
    UNSURE,
    A,
    B,
    Phones,
    asking,
    local_time,
    only_question,
    visit,
)
from .test_counts import ENDS, come, until
from .watching import (
    FEEDER,
    PET_A,
    PET_B,
    START,
    Shown,
    journal,
    pet,
    settle,
    source,
    source_id,
)


async def test_an_unknown_visit_is_asked_on_each_phone(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    await come(hass, aioclient_mock, shown, freezer, 1, animal(embedding=UNSURE))
    # Not while the visit goes on.
    await until(hass, freezer, START + timedelta(minutes=7))
    assert phones.sent == []

    await until(hass, freezer, START + timedelta(minutes=1) + ENDS)
    asked = only_question(entry)
    assert (asked.doubt.kind, asked.doubt.state) == (Kind.UNSURE, State.OPEN)
    # The two likeliest pets, best first.
    assert asked.choices == (PET_B, PET_C)
    question = f"PET_RECOGNITION|{asked.doubt.id}|"
    data = phones.sent[0][2]
    image = data["image"]
    assert data == {
        "tag": f"pet_recognition_{asked.doubt.id}",
        "group": "pet_recognition",
        "actions": [
            {"action": f"{question}{PET_B}", "title": "Pet b"},
            {"action": f"{question}{PET_C}", "title": "Pet c"},
            {"action": f"{question}other", "title": "Other"},
        ],
        "image": image,
    }
    message = f"Who was at Feeder at {local_time(1)}?"
    assert phones.sent == [("Phone 1", message, data), ("Phone 2", message, data)]
    assert asked.sent == tuple(phones.ids.values())
    assert image.startswith(f"/api/pet_recognition/picture/{entry.entry_id}/{asked.crop}?authSig=")

    # The phone shows the crop without a header.
    client = await hass_client_no_auth()
    response = await client.get(image)
    assert response.status == HTTPStatus.OK
    assert response.headers["Cache-Control"] == "private, no-store"
    assert await response.read() == CROP

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["options"]["phones"] == 2
    assert diagnostics["sources"][0]["questions"] == {"open": 1}

    # Asked once, even when the entry loads again.
    await hass.config_entries.async_reload(entry.entry_id)
    await until(hass, freezer, START + timedelta(hours=1))
    assert len(phones.sent) == 2


async def test_names_are_sent_as_they_are(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    named = [
        ConfigSubentryData(data={"species": "cat"}, subentry_type="pet", title=title, unique_id=key)
        for key, title in ((PET_A, "{{ 1 }}"), (PET_B, "42"))
    ]
    entry = await asking(
        hass,
        aioclient_mock,
        shown,
        freezer,
        phones,
        *named,
        source(FEEDER, title="{{ states('sun.sun') }}"),
        examples=((PET_B, B),),
    )
    await visit(hass, aioclient_mock, shown, freezer, 1, unit(1, 1, 0))
    asked = only_question(entry)
    question = f"PET_RECOGNITION|{asked.doubt.id}|"
    for _name, message, data in phones.sent:
        assert message == f"Who was at {{{{ states('sun.sun') }}}} at {local_time(1)}?"
        assert data["actions"] == [
            {"action": f"{question}{PET_A}", "title": "{{ 1 }}"},
            {"action": f"{question}{PET_B}", "title": "42"},
            {"action": f"{question}other", "title": "Other"},
        ]


async def test_one_learning_question_at_a_time(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    # Pet B has no example yet.
    entry = await asking(
        hass,
        aioclient_mock,
        shown,
        freezer,
        phones,
        pet(PET_A),
        pet(PET_B),
        source(FEEDER),
        examples=(),
    )
    await visit(hass, aioclient_mock, shown, freezer, 1, A)
    first = only_question(entry)
    assert first.doubt.kind is Kind.LEARNING
    # Pet A resembles, Pet B has no example to compare.
    assert first.choices == (PET_A, PET_B)
    learning = f"Who was at Feeder at {local_time(1)}? The answer helps tell the pets apart."
    assert [message for _name, message, _data in phones.questions()] == [learning] * 2

    # Another visit waits for the first question.
    await visit(hass, aioclient_mock, shown, freezer, 30, A)
    assert journal(entry).asked == [first]

    # Unanswered, the first question expires and leaves the phones.
    await until(hass, freezer, START + timedelta(minutes=1) + ENDS + LEARNING_EXPIRES)
    assert first.doubt.state is State.EXPIRED
    tag = f"pet_recognition_{first.doubt.id}"
    assert phones.cleared() == [("Phone 1", tag), ("Phone 2", tag)]
    # The visit that waited is asked now.
    second = journal(entry).asked[-1]
    assert second is not first
    assert second.doubt.kind is Kind.LEARNING
    assert len(phones.questions()) == 4
    expected = f"Who was at Feeder at {local_time(30)}? The answer helps tell the pets apart."
    assert phones.questions()[-1][1] == expected

    # Deleting the entry removes the open question only.
    assert await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    tag = f"pet_recognition_{second.doubt.id}"
    assert phones.cleared()[2:] == [("Phone 1", tag), ("Phone 2", tag)]


async def test_a_visit_without_an_animal_in_sight_shows_its_picture(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    # The feeder takes pictures only of a pet, here with its head in the bowl.
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    await come(hass, aioclient_mock, shown, freezer, 1)
    await until(hass, freezer, START + timedelta(minutes=1) + ENDS)
    asked = only_question(entry)
    assert (asked.crop, asked.choices) == (None, (PET_A, PET_B))
    # No crop, so the picture itself, which the feeder keeps.
    images = {data["image"] for _name, _message, data in phones.sent}
    (image,) = images
    path = f"/api/pet_recognition/frame/{entry.entry_id}/{asked.doubt.shown}"
    assert image.startswith(f"{path}?authSig=")
    client = await hass_client_no_auth()
    response = await client.get(image)
    assert response.status == HTTPStatus.OK
    assert response.headers["Cache-Control"] == "private, no-store"
    assert await response.read() == b"picture 1"


async def test_a_question_without_a_picture_kept_has_none(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    # As if the feeder's pictures without an animal were not kept.
    journal(entry).every_picture = frozenset()
    await come(hass, aioclient_mock, shown, freezer, 1)
    await until(hass, freezer, START + timedelta(minutes=1) + ENDS)
    assert only_question(entry).crop is None
    assert phones.sent
    for _name, _message, data in phones.sent:
        assert "image" not in data


@pytest.mark.parametrize(
    ("subentries", "options"),
    [
        # Each pet has examples, and the source does not ask when unsure.
        ((pet(PET_A), pet(PET_B), source(FEEDER, ask_when_unsure=False)), {}),
        # A pet has no example, and the options do not ask while learning.
        ((pet(PET_A), pet(PET_C), source(FEEDER)), {"ask_while_learning": False}),
        # No phone.
        ((pet(PET_A), pet(PET_B), source(FEEDER)), {"phone_ids": []}),
    ],
)
async def test_nothing_is_asked_when_the_settings_say_so(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    subentries: tuple[ConfigSubentryData, ...],
    options: dict[str, Any],
) -> None:
    entry = await asking(
        hass,
        aioclient_mock,
        shown,
        freezer,
        phones,
        *subentries,
        examples=((PET_B, B),),
        **options,
    )
    await visit(hass, aioclient_mock, shown, freezer, 1, unit(1, 1, 0))
    assert journal(entry).asked == []
    assert phones.tried == []


async def test_a_source_without_pets_asks_nothing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, source(FEEDER), examples=())
    await visit(hass, aioclient_mock, shown, freezer, 1)
    assert journal(entry).asked == []


@pytest.mark.parametrize(("later", "asked"), [(timedelta(hours=1), 1), (timedelta(hours=3), 0)])
async def test_a_visit_is_asked_about_until_two_hours_after_it_ended(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    later: timedelta,
    asked: int,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones, phone_ids=[])
    await visit(hass, aioclient_mock, shown, freezer, 1)
    # The phones come later, such as after Home Assistant was stopped.
    await until(hass, freezer, START + later)
    hass.config_entries.async_update_entry(entry, options={"phones": list(phones.ids.values())})
    await settle(hass)
    assert len(journal(entry).asked) == asked
    assert len(phones.sent) == 2 * asked


async def test_a_phone_that_fails_is_tried_again_later(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    phones.failing.add("Phone 2")
    await visit(hass, aioclient_mock, shown, freezer, 1)
    asked = only_question(entry)
    assert asked.sent == (phones.ids["Phone 1"],)
    sent = START + timedelta(minutes=1) + ENDS
    await until(hass, freezer, sent + timedelta(seconds=59))
    assert len(phones.tried) == 2
    # A minute, then two.
    await until(hass, freezer, sent + timedelta(seconds=60))
    assert len(phones.tried) == 3
    phones.failing.clear()
    await until(hass, freezer, sent + timedelta(seconds=179))
    assert len(phones.tried) == 3
    await until(hass, freezer, sent + timedelta(seconds=180))
    assert [name for name, _message, _data in phones.sent] == ["Phone 1", "Phone 2"]
    assert asked.sent == tuple(phones.ids.values())
    await until(hass, freezer, sent + timedelta(hours=1))
    assert len(phones.tried) == 4


async def test_a_phone_is_tried_again_only_while_the_question_can_be_asked(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    await asking(hass, aioclient_mock, shown, freezer, phones)
    phones.failing.add("Phone 2")
    await visit(hass, aioclient_mock, shown, freezer, 1)
    sent = dt_util.utcnow()
    for _ in range(18):
        await until(hass, freezer, dt_util.utcnow() + timedelta(minutes=10))
    tries = [moment - sent for name, moment in phones.tried if name == "Phone 2"]
    # After a minute, then two, four, eight, sixteen, then each half an hour,
    # checked every ten minutes, and no more two hours after it was asked.
    assert tries == [timedelta(minutes=minutes) for minutes in (0, 10, 20, 30, 40, 60, 90, 120)]


async def test_a_restart_sends_what_was_not_sent(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    phones.failing.add("Phone 2")
    await visit(hass, aioclient_mock, shown, freezer, 1)
    phones.failing.clear()
    # Removing Pet C loads the entry again, and its button goes.
    (pet_c,) = (key for key, item in entry.subentries.items() if item.unique_id == PET_C)
    hass.config_entries.async_remove_subentry(entry, pet_c)
    await settle(hass)
    asked = only_question(entry)
    assert asked.sent == tuple(phones.ids.values())
    (first, second) = phones.questions()
    assert (first[0], second[0]) == ("Phone 1", "Phone 2")
    assert [action["title"] for action in second[2]["actions"]] == ["Pet b", "Other"]
    # Then, without Pet C, the examples tell Pet B, which settles the question.
    assert asked.doubt.state is State.SETTLED
    assert len(phones.cleared()) == 2


async def test_a_phone_that_is_gone_raises_a_repair(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    phones.disconnected.add("Phone 2")
    entry = await asking(
        hass,
        aioclient_mock,
        shown,
        freezer,
        phones,
        phone_ids=[*phones.ids.values(), GONE_PHONE],
    )
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"notify_device_missing_{GONE_PHONE}") is not None
    await visit(hass, aioclient_mock, shown, freezer, 1)
    assert only_question(entry).sent == (phones.ids["Phone 1"],)
    issue = issues.async_get_issue(DOMAIN, f"notify_device_missing_{phones.ids['Phone 2']}")
    assert issue is not None
    assert issue.translation_key == "notify_device_missing"
    tried = len(phones.tried)
    # Neither is tried again.
    await until(hass, freezer, dt_util.utcnow() + timedelta(minutes=30))
    assert len(phones.tried) == tried

    await hass.config_entries.async_unload(entry.entry_id)
    assert not [issue for domain, issue in issues.issues if domain == DOMAIN]


async def test_questions_of_a_removed_source_leave_the_phones(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    await visit(hass, aioclient_mock, shown, freezer, 1)
    tag = f"pet_recognition_{only_question(entry).doubt.id}"
    hass.config_entries.async_remove_subentry(entry, source_id(entry))
    await settle(hass)
    assert phones.cleared() == [("Phone 1", tag), ("Phone 2", tag)]
    assert journal(entry).asked == []


async def test_questions_of_a_removed_entry_leave_the_phones(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    await visit(hass, aioclient_mock, shown, freezer, 1)
    tag = f"pet_recognition_{only_question(entry).doubt.id}"
    phones.disconnected.add("Phone 2")
    assert await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    assert phones.cleared() == [("Phone 1", tag)]
    # No repair for a phone that cannot be cleared.
    assert not [issue for domain, issue in ir.async_get(hass).issues if domain == DOMAIN]


async def test_the_picture_of_a_question_only(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    hass_client_no_auth: ClientSessionGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    entry = await asking(hass, aioclient_mock, shown, freezer, phones)
    await visit(hass, aioclient_mock, shown, freezer, 1)
    asked = only_question(entry)
    assert asked.crop is not None
    client = await hass_client_no_auth()
    unsigned = f"/api/pet_recognition/picture/{entry.entry_id}/{asked.crop}"
    assert (await client.get(unsigned)).status == HTTPStatus.UNAUTHORIZED
    # Another crop, a name that is no ID, another entry.
    for entry_id, crop in (
        (entry.entry_id, "f" * 32),
        (entry.entry_id, "F" * 32),
        ("e" * 26, asked.crop),
    ):
        signed = signed_picture_url(hass, PICTURE_URL.format(entry_id=entry_id, crop_id=crop))
        assert (await client.get(signed)).status == HTTPStatus.NOT_FOUND
    signed = signed_picture_url(
        hass, PICTURE_URL.format(entry_id=entry.entry_id, crop_id=asked.crop)
    )
    assert (await client.get(signed)).status == HTTPStatus.OK
    # A companion app adds the URL to the server's as a path, escaped, and
    # sends the phone's token.
    user = await hass.auth.async_create_user("Phone", group_ids=["system-users"])
    token = await hass.auth.async_create_refresh_token(user, "https://example.com/app")
    phone = await hass_client(hass.auth.async_create_access_token(token))
    assert (await phone.get(signed.replace("?", "%3F"))).status == HTTPStatus.OK
    assert (await client.get(signed.replace("?", "%3F"))).status == HTTPStatus.UNAUTHORIZED
    journal(entry).crop_path(asked.crop).unlink()
    assert (await client.get(signed)).status == HTTPStatus.NOT_FOUND
