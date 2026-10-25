"""Pet Recognition: tell which pet appears in pictures from Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import ISSUES, EngineCoordinator
from .engine import EngineClient

PLATFORMS = [Platform.BINARY_SENSOR]


@dataclass
class PetRecognitionData:
    """What a loaded entry holds."""

    client: EngineClient
    coordinator: EngineCoordinator


type PetRecognitionConfigEntry = ConfigEntry[PetRecognitionData]


async def async_setup_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> bool:
    """Connect to the engine of `entry`.

    The entry loads even while the engine does not answer, so that its
    connectivity sensor and repairs can say so, instead of retrying its
    setup: the coordinator fails its updates meanwhile, and later platforms
    find no engine information until it answers. Only a refused key stops
    the setup, to ask for a new one.
    """
    client = EngineClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_API_KEY]
    )
    integration = await async_get_integration(hass, DOMAIN)
    coordinator = EngineCoordinator(hass, entry, client, str(integration.version))
    await coordinator.async_refresh()
    if isinstance(coordinator.last_exception, ConfigEntryAuthFailed):
        raise coordinator.last_exception
    # The engine is watched even if the user disables every entity.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    entry.runtime_data = PetRecognitionData(client, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> bool:
    """Disconnect from the engine.

    Its repairs go too: nothing would clear them while the entry is not
    loaded, and a reload after the engine moved raises them again if needed.
    """
    for issue in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
