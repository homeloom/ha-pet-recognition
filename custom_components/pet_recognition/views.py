"""The crops of the examples, for administrators, such as in the pet's settings."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized

from .const import DOMAIN
from .examples import EXAMPLE_ID

EXAMPLE_URL = "/api/pet_recognition/example/{entry_id}/{example_id}"
# Long enough to look at a form, short enough not to outlive it much.
SIGNED_FOR = timedelta(minutes=30)


def signed_example_url(hass: HomeAssistant, entry_id: str, example_id: str) -> str:
    """A relative URL of an example's crop that works without a header."""
    return async_sign_path(
        hass, EXAMPLE_URL.format(entry_id=entry_id, example_id=example_id), SIGNED_FOR
    )


class ExampleView(HomeAssistantView):
    """Serves the crop of one example."""

    url = EXAMPLE_URL
    name = "api:pet_recognition:example"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, example_id: str) -> web.Response:
        """The JPEG crop, or 404 for anything that is not a known example."""
        if not request[KEY_HASS_USER].is_admin:
            raise Unauthorized
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
            or not EXAMPLE_ID.fullmatch(example_id)
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        store = entry.runtime_data.examples
        if store.gallery.get(example_id) is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            crop = await store.async_read_crop(example_id)
        except FileNotFoundError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=crop, content_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
        )
