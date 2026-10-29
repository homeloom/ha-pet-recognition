"""Helpers of the tests of questions: two phones, three pets and a feeder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.device_automation import (
    DeviceAutomationType,
    InvalidDeviceAutomationConfig,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import template
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.examples import Candidate, Origin
from custom_components.pet_recognition.journal import Asked

from .answers import CROP, MODEL, animal, unit
from .test_counts import ENDS, come, until
from .watching import (
    FEEDER,
    PET_A,
    PET_B,
    START,
    Shown,
    journal,
    pet,
    set_up,
    settle,
    source,
)

PET_C = "c" * 32
A = unit(1, 0, 0)
B = unit(0, 1, 0)
C = unit(0, 0, 1)
# Closest to Pet B, then Pet C, then Pet A, without being sure.
UNSURE = unit(1, 1.1, 1.05)
GONE_PHONE = "0" * 32
# A learning question expires two hours after it was asked.
LEARNING_EXPIRES = timedelta(hours=2, seconds=2)


@dataclass
class Phones:
    """Two phones of the companion app, and what the integration sends them."""

    ids: dict[str, str]
    sent: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    tried: list[tuple[str, datetime]] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)
    disconnected: set[str] = field(default_factory=set)

    @property
    def names(self) -> dict[str, str]:
        return {device_id: name for name, device_id in self.ids.items()}

    def questions(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [sent for sent in self.sent if sent[1] != "clear_notification"]

    def cleared(self) -> list[tuple[str, str]]:
        return [
            (name, data["tag"])
            for name, message, data in self.sent
            if message == "clear_notification"
        ]


def register_phones(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> Phones:
    """Phone 1 and Phone 2, of User 1 and User 2, notified the way the mobile
    app's device action does."""
    registry = dr.async_get(hass)
    ids = {}
    for number in (1, 2):
        mobile_app = MockConfigEntry(domain="mobile_app", data={"user_id": f"user_{number}"})
        mobile_app.add_to_hass(hass)
        name = f"Phone {number}"
        ids[name] = registry.async_get_or_create(
            config_entry_id=mobile_app.entry_id, identifiers={("mobile_app", name)}, name=name
        ).id
    found = Phones(ids)

    class Platform:
        @staticmethod
        async def async_call_action_from_config(
            hass: HomeAssistant,
            config: dict[str, Any],
            variables: dict[str, Any],
            context: Context | None,
        ) -> None:
            name = found.names.get(config["device_id"])
            found.tried.append((str(name), dt_util.utcnow()))
            if name is None or name in found.disconnected:
                raise InvalidDeviceAutomationConfig(
                    "Unable to resolve webhook ID from the device ID"
                )
            if name in found.failing:
                raise HomeAssistantError("Device not connected to local push notifications")
            # As the mobile app renders what it is given.
            rendered = {
                key: template.render_complex(config[key], variables)
                for key in ("message", "title", "data")
                if key in config
            }
            assert "title" not in rendered
            found.sent.append((name, rendered["message"], rendered["data"]))

    async def platform(hass: HomeAssistant, domain: str, kind: DeviceAutomationType) -> type:
        assert (domain, kind) == ("mobile_app", DeviceAutomationType.ACTION)
        return Platform

    monkeypatch.setattr(
        "custom_components.pet_recognition.phones.async_get_device_automation_platform", platform
    )
    return found


async def asking(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    phones: Phones,
    *subentries: ConfigSubentryData,
    examples: tuple[tuple[str, list[float]], ...] = ((PET_B, B), (PET_C, C)),
    phone_ids: list[str] | None = None,
    **options: object,
) -> MockConfigEntry:
    """Pets A, B and C, with examples, the feeder, and both phones in the options."""
    freezer.move_to(START)
    entry = await set_up(
        hass,
        aioclient_mock,
        shown,
        *(subentries or (pet(PET_A), pet(PET_B), pet(PET_C), source(FEEDER))),
    )
    store = entry.runtime_data.examples
    for key, vector in examples:
        await store.async_add(key, Candidate(CROP, tuple(vector), MODEL), Origin.UPLOAD)
    hass.config_entries.async_update_entry(
        entry,
        options={
            "phones": list(phones.ids.values()) if phone_ids is None else phone_ids,
            **options,
        },
    )
    await settle(hass)
    return entry


async def visit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    shown: Shown,
    freezer: FrozenDateTimeFactory,
    minutes: float,
    embedding: list[float] = UNSURE,
) -> None:
    """A visit of one picture at `minutes` after the start, until it ends."""
    await come(hass, aioclient_mock, shown, freezer, minutes, animal(embedding=embedding))
    await until(hass, freezer, START + timedelta(minutes=minutes) + ENDS)


def local_time(minutes: float) -> str:
    return dt_util.as_local(START + timedelta(minutes=minutes)).strftime("%H:%M")


def only_question(entry: MockConfigEntry) -> Asked:
    (asked,) = journal(entry).asked
    return asked
