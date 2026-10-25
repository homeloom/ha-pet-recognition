"""Pet Recognition: tell which pet appears in pictures from Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .engine import (
    EngineAuthError,
    EngineClient,
    EngineError,
    EngineInfo,
    EngineResponseError,
)


@dataclass
class PetRecognitionData:
    """What a loaded entry holds."""

    client: EngineClient
    info: EngineInfo


type PetRecognitionConfigEntry = ConfigEntry[PetRecognitionData]


async def async_setup_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> bool:
    """Connect to the engine of `entry`."""
    client = EngineClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_API_KEY]
    )
    try:
        info = await client.info()
    except EngineAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except EngineResponseError as err:
        # Retried too: the engine may be updated, or the address fixed.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="not_an_engine",
            translation_placeholders={"error": str(err)},
        ) from err
    except EngineError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    entry.runtime_data = PetRecognitionData(client, info)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> bool:
    """Disconnect from the engine."""
    return True
