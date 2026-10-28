"""Tests of the setup of the engine connection."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_IGNORE, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pet_recognition.const import DOMAIN

from .answers import KEY, URL, error, info
from .common import configured

APP_URL = "http://abc123-pet-recognition-engine:8765"
NEW_KEY = "n" * 32


def discovery(**config: object) -> HassioServiceInfo:
    announced = {"host": "abc123-pet-recognition-engine", "port": 8765, "api_key": KEY}
    return HassioServiceInfo(
        config={**announced, **config},
        name="Pet Recognition Engine",
        slug="abc123_pet_recognition_engine",
        uuid="0123456789abcdef0123456789abcdef",
    )


async def test_the_user_enters_the_url_and_the_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f" {URL}/ ", CONF_API_KEY: f" {KEY} "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Pet Recognition"
    assert result["data"] == {CONF_URL: URL, CONF_API_KEY: KEY}
    assert result["result"].unique_id == DOMAIN
    assert result["result"].state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("answer", "user_input", "errors"),
    [
        ({"json": info()}, {CONF_URL: "engine.test:8765"}, {CONF_URL: "invalid_url"}),
        ({"json": info()}, {CONF_URL: "ftp://engine.test"}, {CONF_URL: "invalid_url"}),
        ({"json": info()}, {CONF_URL: "http://"}, {CONF_URL: "invalid_url"}),
        ({"json": info()}, {CONF_URL: "http://[::1"}, {CONF_URL: "invalid_url"}),
        ({"json": info()}, {CONF_URL: f"{URL}/?a=1"}, {CONF_URL: "invalid_url"}),
        ({"json": info()}, {CONF_URL: "http://user:secret@engine.test"}, {CONF_URL: "invalid_url"}),
        ({"json": info()}, {CONF_URL: "http://user@engine.test"}, {CONF_URL: "invalid_url"}),
        ({"json": info()}, {CONF_API_KEY: "short"}, {CONF_API_KEY: "key_too_short"}),
        ({"status": 401, "json": error("unauthorized")}, {}, {CONF_API_KEY: "invalid_auth"}),
        ({"exc": ClientError()}, {}, {"base": "cannot_connect"}),
        ({"json": {**info(), "api": 2}}, {}, {"base": "not_an_engine"}),
        ({"text": "<html></html>"}, {}, {"base": "not_an_engine"}),
    ],
)
async def test_the_user_is_told_what_is_wrong_and_can_fix_it(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    answer: dict[str, Any],
    user_input: dict[str, str],
    errors: dict[str, str],
) -> None:
    aioclient_mock.get(f"{URL}/v1/info", **answer)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_KEY: KEY, **user_input}
    )
    assert (result["type"], result["errors"]) == (FlowResultType.FORM, errors)

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_KEY: KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_one_engine_serves_the_installation(hass: HomeAssistant) -> None:
    configured(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")


async def test_an_ignored_discovery_can_still_be_set_up_by_hand(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, source=SOURCE_IGNORE).add_to_hass(hass)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_KEY: KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_app_announces_the_engine_and_the_user_confirms(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{APP_URL}/v1/info", json=info())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=discovery()
    )
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "hassio_confirm")
    assert result["description_placeholders"] == {"app": "Pet Recognition Engine"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: APP_URL, CONF_API_KEY: KEY}
    assert result["result"].unique_id == DOMAIN


async def test_an_announced_engine_that_fails_shows_why(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{APP_URL}/v1/info", exc=ClientError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=discovery()
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert (result["type"], result["errors"]) == (FlowResultType.FORM, {"base": "cannot_connect"})


@pytest.mark.parametrize(
    "config",
    [
        {"host": ""},
        {"host": None},
        {"port": "8765"},
        {"port": True},
        {"port": 0},
        {"port": 65536},
        {"api_key": "short"},
        {"api_key": None},
        {"host": "not a host"},
    ],
)
async def test_an_unusable_announcement_is_dropped(
    hass: HomeAssistant, config: dict[str, Any]
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=discovery(**config)
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "invalid_discovery_info")


async def test_a_new_announcement_updates_the_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    aioclient_mock.get("http://moved-pet-recognition-engine:8765/v1/info", json=info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=discovery(host="moved-pet-recognition-engine", api_key=NEW_KEY),
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")
    await hass.async_block_till_done()
    assert entry.data == {
        CONF_URL: "http://moved-pet-recognition-engine:8765",
        CONF_API_KEY: NEW_KEY,
    }
    assert entry.state is ConfigEntryState.LOADED
    # Reloaded with the new address.
    assert entry.runtime_data.client.url == "http://moved-pet-recognition-engine:8765"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_new_announcement_repairs_an_engine_whose_key_was_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    # The app was reinstalled and created a new key; Home Assistant restarted.
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", status=401, json=error("unauthorized"))
    aioclient_mock.get(f"{APP_URL}/v1/info", json=info())
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=discovery(api_key=NEW_KEY)
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")
    await hass.async_block_till_done()
    assert entry.data == {CONF_URL: APP_URL, CONF_API_KEY: NEW_KEY}
    assert entry.state is ConfigEntryState.LOADED
    # The request for a new key is closed.
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    # The same announcement again changes nothing.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=discovery(api_key=NEW_KEY)
    )
    assert result["reason"] == "already_configured"


async def test_an_unchanged_announcement_leaves_a_failed_entry_alone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass, url=APP_URL)
    aioclient_mock.get(f"{APP_URL}/v1/info", status=401, json=error("unauthorized"))
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=discovery()
    )
    assert result["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    # Still waiting for the user's new key.
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_a_second_setup_waits_for_the_first(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=discovery()
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_in_progress")


async def test_the_user_changes_the_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    aioclient_mock.get("http://other.test:8765/v1/info", json=info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await entry.start_reconfigure_flow(hass)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "reconfigure")

    # An empty key keeps the current one.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://other.test:8765"}
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    assert entry.data == {CONF_URL: "http://other.test:8765", CONF_API_KEY: KEY}
    await hass.async_block_till_done()
    assert entry.runtime_data.client.url == "http://other.test:8765"


async def test_a_change_of_engine_is_checked_first(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get("http://other.test:8765/v1/info", status=401, json=error("unauthorized"))
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://other.test:8765", CONF_API_KEY: NEW_KEY}
    )
    assert (result["type"], result["errors"]) == (
        FlowResultType.FORM,
        {CONF_API_KEY: "invalid_auth"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "not a url", CONF_API_KEY: NEW_KEY}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_KEY: NEW_KEY}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_URL: URL, CONF_API_KEY: NEW_KEY}
    # The entry loads with its new engine.
    await hass.async_block_till_done()


async def test_a_refused_key_asks_for_the_new_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = configured(hass)
    aioclient_mock.get(f"{URL}/v1/info", status=401, json=error("unauthorized"))
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["step_id"] == "reauth_confirm"
    assert flow["context"]["source"] == "reauth"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert result["errors"] == {CONF_API_KEY: "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/info", json=info())
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reauth_successful")
    await hass.async_block_till_done()
    assert entry.data[CONF_API_KEY] == NEW_KEY
    assert entry.state is ConfigEntryState.LOADED
