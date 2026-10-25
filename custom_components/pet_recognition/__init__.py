"""Pet Recognition: tell which pet appears in pictures from Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import ISSUES, EngineCoordinator
from .core.matching import OTHER_ANIMAL
from .engine import EngineClient
from .examples import ExampleStore, async_get_store, async_remove_data
from .pets import Reembedder, async_check_examples, async_delete_pet_issues, pets
from .views import ExampleView

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class PetRecognitionData:
    """What a loaded entry holds."""

    client: EngineClient
    coordinator: EngineCoordinator
    examples: ExampleStore


type PetRecognitionConfigEntry = ConfigEntry[PetRecognitionData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the crops of the examples."""
    hass.http.register_view(ExampleView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> bool:
    """Connect to the engine of `entry`, and load its pets.

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

    examples = await async_get_store(hass, entry.entry_id)
    # The examples of pets removed meanwhile. The other animal is no subentry.
    await examples.async_keep_pets({*pets(entry), OTHER_ANIMAL})
    async_check_examples(hass, entry, examples)
    entry.async_on_unload(
        examples.async_add_listener(lambda: async_check_examples(hass, entry, examples))
    )

    reembedder = Reembedder(coordinator, examples)

    @callback
    def _reembed() -> None:
        if not reembedder.running and (model := reembedder.needed()):
            entry.async_create_background_task(
                hass, reembedder.async_run(model), "pet_recognition embed examples again"
            )

    # Also keeps the engine watched even if the user disables every entity.
    entry.async_on_unload(coordinator.async_add_listener(_reembed))
    _reembed()

    entry.runtime_data = PetRecognitionData(client, coordinator, examples)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> None:
    """Load the entry again after a change, such as a pet added or removed."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> bool:
    """Disconnect from the engine.

    Its repairs go too: nothing would clear them while the entry is not
    loaded, and a reload after the engine moved raises them again if needed.
    """
    for issue in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue)
    async_delete_pet_issues(hass)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> None:
    """Delete the examples and files of a deleted entry."""
    await async_remove_data(hass, entry.entry_id)
