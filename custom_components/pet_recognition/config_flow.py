"""Set up the connection to the Pet Recognition Engine, and the pets.

Under the Supervisor the engine app announces its address and key, and the
user only confirms. Elsewhere the user enters the engine's URL and key. One
engine serves the whole installation, so the entry has a fixed unique ID: a
new announcement, after the app moved or got a new key, updates the entry
instead of adding another one.

Each pet is a subentry, taught from photos the user uploads or takes from a
camera. A new pet's photos are kept in the flow and stored when the user
finishes, under the pet's unique ID, which the flow chooses: an abandoned
flow leaves nothing behind. In a pet's settings, each change is stored at
once.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_ENTITY_ID, CONF_NAME, CONF_URL
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    FileSelector,
    FileSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.util import dt as dt_util
from yarl import URL

from .const import (
    CONF_SPECIES,
    DOMAIN,
    EXAMPLE_LIMIT,
    MIN_KEY_LENGTH,
    SPECIES,
    SUBENTRY_PET,
    SUBENTRY_SOURCE,
    TITLE,
)
from .core.matching import NEAR_DUPLICATE, Added, similarity
from .engine import (
    EngineAuthError,
    EngineClient,
    EngineConnectionError,
    EngineError,
    engine_url,
)
from .examples import Candidate, Origin, stored_vector
from .files import new_id
from .options import PetRecognitionOptionsFlow
from .pets import PictureError, async_candidate, async_capture
from .source_flow import SourceFlow
from .views import signed_example_url

if TYPE_CHECKING:
    from . import PetRecognitionData

URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
PHOTO_SELECTOR = FileSelector(FileSelectorConfig(accept="image/jpeg,image/png,image/webp"))
CAMERA_SELECTOR = EntitySelector(EntitySelectorConfig(domain=["camera", "image"]))
SPECIES_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(SPECIES), translation_key=CONF_SPECIES, mode=SelectSelectorMode.LIST
    )
)

CONF_PHOTO = "photo"
CONF_EXAMPLES = "examples"


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

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Pets and sources are subentries."""
        return {SUBENTRY_PET: PetFlow, SUBENTRY_SOURCE: SourceFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PetRecognitionOptionsFlow:
        """The options of the recognition."""
        return PetRecognitionOptionsFlow()

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


def _read_upload(hass: HomeAssistant, file_id: str, max_bytes: int) -> bytes:
    """The bytes of an uploaded file, if it is not too large for the engine."""
    try:
        with process_uploaded_file(hass, file_id) as path:
            if Path(path).stat().st_size > max_bytes:
                raise PictureError("picture_too_large")
            return Path(path).read_bytes()
    except (OSError, ValueError) as err:
        raise PictureError("upload_failed") from err


class PetFlow(ConfigSubentryFlow):
    """Add a pet and teach it from photos, or change a pet."""

    def __init__(self) -> None:
        self._name = ""
        self._species = ""
        # The key of the pet's examples, and the subentry's unique ID.
        self._key = ""
        # The examples of a new pet, stored when the user finishes. A flow
        # left open keeps them in memory until it is closed or Home Assistant
        # restarts: at most 200 crops, of a few tens of kilobytes each.
        self._pending: list[tuple[Candidate, Origin]] = []
        self._release: CALLBACK_TYPE | None = None

    @callback
    def async_remove(self) -> None:
        """Let a reload forget the examples of a pet that was not created."""
        if self._release:
            self._release()

    def _data(self) -> PetRecognitionData:
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            raise PictureError("cannot_connect")
        return entry.runtime_data

    def _count(self) -> int:
        if self.source == SOURCE_USER:
            return len(self._pending)
        return len(self._data().examples.gallery.of(self._key))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Name a new pet and choose its species."""
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = user_input[CONF_NAME].strip()
            self._species = user_input[CONF_SPECIES]
            if self._name:
                self._key = new_id()
                self._release = self._get_entry().runtime_data.examples.reserve(self._key)
                return self._menu()
            errors[CONF_NAME] = "empty_name"
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_SPECIES, default=SPECIES[0]): SPECIES_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change a pet."""
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        subentry = self._get_reconfigure_subentry()
        self._name = subentry.title
        self._species = subentry.data[CONF_SPECIES]
        self._key = subentry.unique_id or ""
        return self._menu()

    async def async_step_photos(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose how to add a photo of a new pet, or finish."""
        return self._menu()

    async def async_step_change(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose what to change in a pet."""
        return self._menu()

    def _menu(self) -> SubentryFlowResult:
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        placeholders = {"pet": self._name, "count": str(self._count())}
        if self.source == SOURCE_USER:
            return self.async_show_menu(
                step_id="photos",
                menu_options=["photo", "capture", "finish"],
                description_placeholders=placeholders,
            )
        return self.async_show_menu(
            step_id="change",
            menu_options=["photo", "capture", "remove", "rename", "done"],
            description_placeholders=placeholders,
        )

    async def async_step_photo(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Upload a photo of the pet, or go back without one."""
        errors: dict[str, str] = {}
        if user_input is not None and not user_input.get(CONF_PHOTO):
            return self._menu()
        if user_input is not None:
            try:
                if (info := self._data().coordinator.data) is None:
                    raise PictureError("cannot_connect")
                picture = await self.hass.async_add_executor_job(
                    _read_upload, self.hass, user_input[CONF_PHOTO], info.max_bytes
                )
                await self._learn(picture, Origin.UPLOAD)
            except PictureError as err:
                errors["base"] = err.reason
            else:
                return self._menu()
        return self.async_show_form(
            step_id="photo",
            data_schema=vol.Schema({vol.Optional(CONF_PHOTO): PHOTO_SELECTOR}),
            description_placeholders={"pet": self._name},
            errors=errors,
        )

    async def async_step_capture(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Take a photo of the pet now, from a camera or an image entity."""
        errors: dict[str, str] = {}
        if user_input is not None and not user_input.get(CONF_ENTITY_ID):
            return self._menu()
        if user_input is not None:
            try:
                picture = await async_capture(self.hass, user_input[CONF_ENTITY_ID])
                await self._learn(picture, Origin.CAPTURE)
            except PictureError as err:
                errors["base"] = err.reason
            else:
                return self._menu()
        schema = vol.Schema({vol.Optional(CONF_ENTITY_ID): CAMERA_SELECTOR})
        return self.async_show_form(
            step_id="capture",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            description_placeholders={"pet": self._name},
            errors=errors,
        )

    async def _learn(self, picture: bytes, origin: Origin) -> None:
        """Make the one animal of `picture` an example of the pet."""
        data = self._data()
        candidate = await async_candidate(data.coordinator, picture, self._species)
        if self.source != SOURCE_USER:
            outcome = await data.examples.async_add(self._key, candidate, origin)
            if outcome is Added.DUPLICATE:
                raise PictureError("duplicate_picture")
            if outcome is Added.FULL:
                raise PictureError("too_many_examples")
            return
        # Compared as the store will keep it, so that finishing refuses none.
        candidate = replace(candidate, vector=stored_vector(candidate.vector))
        if any(
            other.model == candidate.model
            and similarity(candidate.vector, other.vector) >= NEAR_DUPLICATE
            for other, _ in self._pending
        ):
            raise PictureError("duplicate_picture")
        if len(self._pending) >= EXAMPLE_LIMIT:
            raise PictureError("too_many_examples")
        self._pending.append((candidate, origin))

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Store the new pet's examples, and add the pet."""
        try:
            store = self._data().examples
        except PictureError:
            return self.async_abort(reason="entry_not_loaded")
        await store.async_add_many(self._key, self._pending)
        return self.async_create_entry(
            title=self._name, data={CONF_SPECIES: self._species}, unique_id=self._key
        )

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Remove some of the pet's examples, shown with their pictures."""
        try:
            store = self._data().examples
        except PictureError:
            return self.async_abort(reason="entry_not_loaded")
        if user_input is not None:
            await store.async_remove(user_input.get(CONF_EXAMPLES, []))
            return self._menu()
        entry_id = self._get_entry().entry_id
        options: list[SelectOptionDict] = []
        pictures: list[str] = []
        for number, example in enumerate(store.gallery.of(self._key), start=1):
            added = dt_util.as_local(dt_util.utc_from_timestamp(example.added))
            options.append(
                SelectOptionDict(value=example.id, label=f"{number}. {added:%Y-%m-%d %H:%M}")
            )
            url = signed_example_url(self.hass, entry_id, example.id)
            pictures.append(f'{number}.&nbsp;<img src="{url}" alt="{number}" height="96">')
        schema = vol.Schema(
            {
                vol.Optional(CONF_EXAMPLES, default=[]): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="remove",
            data_schema=schema,
            description_placeholders={
                "pet": self._name,
                "pictures": " ".join(pictures) or "-",
            },
        )

    async def async_step_rename(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Rename the pet."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if name := user_input[CONF_NAME].strip():
                return self.async_update_and_abort(
                    self._get_entry(), self._get_reconfigure_subentry(), title=name
                )
            errors[CONF_NAME] = "empty_name"
        schema = vol.Schema({vol.Required(CONF_NAME): TextSelector()})
        return self.async_show_form(
            step_id="rename",
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_NAME: self._name}),
            errors=errors,
        )

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Close the pet's settings, whose changes are already stored."""
        return self.async_abort(reason="reconfigure_successful")
