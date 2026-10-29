"""The phones: notifications sent through the device action of the mobile app.

The texts go as plain strings, never as templates, so that a pet or source
named like a template is shown as it is. A phone that is no longer a mobile
app device raises a repair, which the next load of the entry clears once
the phone is back or no longer chosen.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.device_automation import (
    DeviceAutomationType,
    InvalidDeviceAutomationConfig,
    async_get_device_automation_platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MOBILE_APP = "mobile_app"
# The user of a companion app's registration, in its config entry.
USER_ID = "user_id"
ISSUE_NOTIFY_DEVICE_MISSING = "notify_device_missing"
# The message that makes the companion apps remove a notification by its tag.
CLEAR_NOTIFICATION = "clear_notification"


class PhoneMissingError(HomeAssistantError):
    """The phone is no longer a mobile app device that can be notified."""


async def async_notify(hass: HomeAssistant, phone: str, message: str, data: dict[str, Any]) -> None:
    """Send a notification to a phone.

    Raises `PhoneMissingError` for a phone that is gone, after raising its
    repair, and `HomeAssistantError` for a failure worth trying again.
    """
    try:
        await _async_send(hass, phone, message, data)
    except InvalidDeviceAutomationConfig as err:
        async_phone_missing(hass, phone)
        raise PhoneMissingError(str(err)) from err


async def async_clear(hass: HomeAssistant, phones: Iterable[str], tag: str) -> None:
    """Remove the notification of `tag` from phones, as far as they can be reached."""
    for phone in phones:
        try:
            await _async_send(hass, phone, CLEAR_NOTIFICATION, {"tag": tag})
        except HomeAssistantError as err:
            _LOGGER.debug("Cannot remove a notification from a phone: %s", err)


async def _async_send(hass: HomeAssistant, phone: str, message: str, data: dict[str, Any]) -> None:
    config = {
        "device_id": phone,
        "domain": MOBILE_APP,
        "type": "notify",
        "message": message,
        "data": data,
    }
    platform = await async_get_device_automation_platform(
        hass, MOBILE_APP, DeviceAutomationType.ACTION
    )
    await platform.async_call_action_from_config(hass, config, {}, None)


@callback
def phone_users(hass: HomeAssistant, phones: Iterable[str]) -> set[str]:
    """The users the companion app of each phone was registered for."""
    registry = dr.async_get(hass)
    users: set[str] = set()
    for phone in phones:
        if (device := registry.async_get(phone)) is None:
            continue
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == MOBILE_APP and entry.data.get(USER_ID):
                users.add(entry.data[USER_ID])
    return users


@callback
def async_check_phones(hass: HomeAssistant, phones: Iterable[str]) -> None:
    """Raise a repair for each phone the device registry no longer knows."""
    registry = dr.async_get(hass)
    for phone in phones:
        if registry.async_get(phone) is None:
            async_phone_missing(hass, phone)


@callback
def async_phone_missing(hass: HomeAssistant, phone: str) -> None:
    """Say that a phone chosen in the options cannot be notified."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_NOTIFY_DEVICE_MISSING}_{phone}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NOTIFY_DEVICE_MISSING,
    )


@callback
def async_delete_phone_issues(hass: HomeAssistant) -> None:
    """Remove the repairs about phones."""
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(f"{ISSUE_NOTIFY_DEVICE_MISSING}_"):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
