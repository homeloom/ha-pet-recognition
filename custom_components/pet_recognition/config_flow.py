"""Set up the connection to the Pet Recognition Engine.

Under the Supervisor the engine app announces its address and key, and the
user only confirms. Elsewhere the user enters the engine's URL and key. One
engine serves the whole installation, so the entry has a fixed unique ID: a
new announcement, after the app moved or got a new key, updates the entry
instead of adding another one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from yarl import URL

from .const import DOMAIN, MIN_KEY_LENGTH, TITLE
from .engine import (
    EngineAuthError,
    EngineClient,
    EngineConnectionError,
    EngineError,
    engine_url,
)

URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _clean_url(value: str) -> str | None:
    """`value` as an engine URL without a trailing slash, or None if it is not one."""
    try:
        url = URL(value.strip())
    except ValueError:
        return None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.user is not None
        or url.password is not None
        or url.query_string
        or url.fragment
    ):
        return None
    return str(url).rstrip("/")


class PetRecognitionConfigFlow(ConfigFlow, domain=DOMAIN):
    """The setup of the engine connection."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}
        self._app = ""

    async def _check(self, url: str, api_key: str) -> dict[str, str]:
        """The errors of the form for the engine at `url` with `api_key`."""
        if len(api_key) < MIN_KEY_LENGTH:
            return {CONF_API_KEY: "key_too_short"}
        client = EngineClient(async_get_clientsession(self.hass), url, api_key)
        try:
            await client.info()
        except EngineAuthError:
            return {CONF_API_KEY: "invalid_auth"}
        except EngineConnectionError:
            return {"base": "cannot_connect"}
        except EngineError:
            return {"base": "not_an_engine"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enter the engine's URL and key."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _clean_url(user_input[CONF_URL])
            api_key = user_input[CONF_API_KEY].strip()
            errors = {CONF_URL: "invalid_url"} if url is None else await self._check(url, api_key)
            if url is not None and not errors:
                return self.async_create_entry(
                    title=TITLE, data={CONF_URL: url, CONF_API_KEY: api_key}
                )
        schema = vol.Schema(
            {vol.Required(CONF_URL): URL_SELECTOR, vol.Required(CONF_API_KEY): KEY_SELECTOR}
        )
        suggested = {CONF_URL: user_input[CONF_URL]} if user_input else None
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Receive the address and key the engine app announced."""
        config = discovery_info.config
        host, port, api_key = config.get("host"), config.get("port"), config.get(CONF_API_KEY)
        if (
            not isinstance(host, str)
            or not host
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 < port < 65536
            or not isinstance(api_key, str)
            or len(api_key) < MIN_KEY_LENGTH
        ):
            return self.async_abort(reason="invalid_discovery_info")
        try:
            url = engine_url(host, port)
        except ValueError:
            return self.async_abort(reason="invalid_discovery_info")
        self._discovered = {CONF_URL: url, CONF_API_KEY: api_key}
        await self.async_set_unique_id(DOMAIN)
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN)
        if entry is not None and entry.state is ConfigEntryState.SETUP_ERROR:
            # Home Assistant reloads only a loaded entry, or one waiting to
            # retry. An entry whose key was refused gets the app's new key and
            # is set up again, which also closes its request for a new key.
            if self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **self._discovered}
            ):
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured(updates=self._discovered)
        self._app = discovery_info.name
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the engine the app announced."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._check(self._discovered[CONF_URL], self._discovered[CONF_API_KEY])
            if not errors:
                return self.async_create_entry(title=TITLE, data=self._discovered)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"app": self._app},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the engine's URL, and its key if a new one is entered."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _clean_url(user_input[CONF_URL])
            api_key = user_input.get(CONF_API_KEY, "").strip() or entry.data[CONF_API_KEY]
            errors = {CONF_URL: "invalid_url"} if url is None else await self._check(url, api_key)
            if url is not None and not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_URL: url, CONF_API_KEY: api_key}
                )
        schema = vol.Schema(
            {vol.Required(CONF_URL): URL_SELECTOR, vol.Optional(CONF_API_KEY): KEY_SELECTOR}
        )
        suggested = {CONF_URL: (user_input or entry.data)[CONF_URL]}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Ask for a new key after the engine refused the stored one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the engine's new key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            errors = await self._check(entry.data[CONF_URL], api_key)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): KEY_SELECTOR}),
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )
