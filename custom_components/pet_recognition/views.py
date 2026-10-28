"""The crops of the examples, for administrators, and of the questions, for the phones."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized

from .const import DOMAIN
from .examples import EXAMPLE_ID
from .files import FILE_ID

EXAMPLE_URL = "/api/pet_recognition/example/{entry_id}/{example_id}"
PICTURE_URL = "/api/pet_recognition/picture/{entry_id}/{crop_id}"
# Long enough to look at a form, short enough not to outlive it much.
SIGNED_FOR = timedelta(minutes=30)
# Long enough for a notification to be seen the next day. A restart of Home
# Assistant ends it sooner.
PICTURE_SIGNED_FOR = timedelta(hours=24)
NO_STORE = {"Cache-Control": "private, no-store"}


def signed_example_url(hass: HomeAssistant, entry_id: str, example_id: str) -> str:
    """A relative URL of an example's crop that works without a header."""
    return async_sign_path(
        hass, EXAMPLE_URL.format(entry_id=entry_id, example_id=example_id), SIGNED_FOR
    )


def signed_picture_url(hass: HomeAssistant, entry_id: str, crop_id: str) -> str:
    """A relative URL of the crop of a question that the companion apps can show.

    It is signed for Home Assistant's content user, which is no
    administrator, whatever started the question.
    """
    return async_sign_path(
        hass,
        PICTURE_URL.format(entry_id=entry_id, crop_id=crop_id),
        PICTURE_SIGNED_FOR,
        use_content_user=True,
    )


def _loaded(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    """A loaded entry of the integration."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    return entry


class ExampleView(HomeAssistantView):
    """Serves the crop of one example."""

    url = EXAMPLE_URL
    name = "api:pet_recognition:example"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, example_id: str) -> web.Response:
        """The JPEG crop, or 404 for anything that is not a known example."""
        if not request[KEY_HASS_USER].is_admin:
            raise Unauthorized
        entry = _loaded(request.app[KEY_HASS], entry_id)
        if entry is None or not EXAMPLE_ID.fullmatch(example_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        store = entry.runtime_data.examples
        if store.gallery.get(example_id) is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            crop = await store.async_read_crop(example_id)
        except FileNotFoundError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(body=crop, content_type="image/jpeg", headers=NO_STORE)


class PictureView(HomeAssistantView):
    """Serves the crop a question shows, to any user, such as a phone's."""

    url = PICTURE_URL
    name = "api:pet_recognition:picture"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, crop_id: str) -> web.Response:
        """The JPEG crop, or 404 for anything that is not the crop of a question."""
        entry = _loaded(request.app[KEY_HASS], entry_id)
        if entry is None or not FILE_ID.fullmatch(crop_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        journal = entry.runtime_data.journal
        if not any(asked.crop == crop_id for asked in journal.asked):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            crop = await journal.async_read_crop(crop_id)
        except FileNotFoundError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(body=crop, content_type="image/jpeg", headers=NO_STORE)
