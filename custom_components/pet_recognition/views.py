"""The crops of the examples, for administrators, and the crops and pictures
kept, for the phones, the dashboards and the entities."""

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
from .files import FILE_ID, content_type
from .journal import Journal

EXAMPLE_URL = "/api/pet_recognition/example/{entry_id}/{example_id}"
PICTURE_URL = "/api/pet_recognition/picture/{entry_id}/{crop_id}"
FRAME_URL = "/api/pet_recognition/frame/{entry_id}/{picture_id}"
# Long enough to look at a form, short enough not to outlive it much.
SIGNED_FOR = timedelta(minutes=30)
# Long enough for a notification to be seen the next day. A restart of Home
# Assistant ends it sooner.
PICTURE_SIGNED_FOR = timedelta(hours=24)
# The picture of an entity is signed again each hour.
ENTITY_PICTURE_SIGNED_FOR = timedelta(hours=2)
NO_STORE = {"Cache-Control": "private, no-store"}


def signed_example_url(hass: HomeAssistant, entry_id: str, example_id: str) -> str:
    """A relative URL of an example's crop that works without a header."""
    return async_sign_path(
        hass, EXAMPLE_URL.format(entry_id=entry_id, example_id=example_id), SIGNED_FOR
    )


def picture_paths(entry_id: str, journal: Journal, source: str) -> dict[str, str]:
    """What shows each picture of a source, by picture ID, as a path: the
    crop of its largest animal, or the picture itself when it is kept
    without one."""
    kept = journal.kept_frames()
    paths = {}
    for record in journal.of(source):
        if record.detections:
            largest = max(record.detections, key=lambda detection: detection.area)
            paths[record.id] = PICTURE_URL.format(entry_id=entry_id, crop_id=largest.crop)
        elif record.id in kept:
            paths[record.id] = FRAME_URL.format(entry_id=entry_id, picture_id=record.id)
    return paths


def signed_picture_url(hass: HomeAssistant, path: str) -> str:
    """A relative URL of the picture of a question that the companion apps can show.

    It is signed for Home Assistant's content user, which is no
    administrator, whatever started the question.
    """
    return async_sign_path(hass, path, PICTURE_SIGNED_FOR, use_content_user=True)


def signed_entity_picture(hass: HomeAssistant, path: str) -> str:
    """A relative URL of the picture of an entity, which any user can see,
    signed for Home Assistant's content user for two hours."""
    return async_sign_path(hass, path, ENTITY_PICTURE_SIGNED_FOR, use_content_user=True)


def signed_url(hass: HomeAssistant, path: str, refresh_token_id: str | None) -> str:
    """A relative URL of a crop or a picture kept, for a dashboard, signed
    for the user of `refresh_token_id` for half an hour."""
    return async_sign_path(hass, path, SIGNED_FOR, refresh_token_id=refresh_token_id)


def _file_id(value: str) -> str:
    """The ID of a file named by a request.

    The companion apps load a relative URL of a notification with the
    phone's token, but add it to the server's URL as a path, so its
    signature comes escaped at the end of the ID: it is left out.
    """
    return value.partition("?")[0]


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
    """Serves the crop of a picture kept, or of a question, to any user, such
    as a phone's or a dashboard's."""

    url = PICTURE_URL
    name = "api:pet_recognition:picture"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, crop_id: str) -> web.Response:
        """The JPEG crop, or 404 for anything that is not the crop of a picture kept."""
        crop_id = _file_id(crop_id)
        entry = _loaded(request.app[KEY_HASS], entry_id)
        if entry is None or not FILE_ID.fullmatch(crop_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        journal = entry.runtime_data.journal
        # The crop of a question is that of one of its pictures, kept longer than it.
        if not any(
            detection.crop == crop_id
            for record in journal.records
            for detection in record.detections
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            crop = await journal.async_read_crop(crop_id)
        except FileNotFoundError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(body=crop, content_type="image/jpeg", headers=NO_STORE)


class FrameView(HomeAssistantView):
    """Serves a picture kept, to any user, such as a phone's or a dashboard's."""

    url = FRAME_URL
    name = "api:pet_recognition:frame"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, picture_id: str) -> web.Response:
        """The picture, or 404 for anything that is not a picture kept."""
        picture_id = _file_id(picture_id)
        entry = _loaded(request.app[KEY_HASS], entry_id)
        if entry is None or not FILE_ID.fullmatch(picture_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        journal = entry.runtime_data.journal
        if picture_id not in journal.kept_frames():
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            picture = await journal.async_read_frame(picture_id)
        except FileNotFoundError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(body=picture, content_type=content_type(picture), headers=NO_STORE)
