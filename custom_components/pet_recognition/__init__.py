"""Pet Recognition: tell which pet appears in pictures from Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .cameras import CameraWatcher
from .const import CONF_ENTITIES, DOMAIN
from .coordinator import ISSUES, EngineCoordinator
from .core.matching import OTHER_ANIMAL
from .counts import VisitCounter
from .engine import EngineClient
from .examples import ExampleStore, async_get_store, async_remove_data
from .history import History
from .journal import Journal, async_get_journal, async_remove_journal
from .learning import Learner, Rematcher
from .options import entry_options
from .pets import Reembedder, async_check_examples, async_delete_pet_issues, pets
from .phones import async_check_phones, async_delete_phone_issues
from .questions import Asker, async_clear_questions
from .repairs import async_delete_source_issues, async_follow_renames, async_watch_entities
from .sources import Analyzer, SourceWatcher, source_settings, sources
from .views import ExampleView, PictureView

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.IMAGE, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class PetRecognitionData:
    """What a loaded entry holds."""

    client: EngineClient
    coordinator: EngineCoordinator
    examples: ExampleStore
    journal: Journal
    history: History
    counter: VisitCounter
    analyzer: Analyzer
    learner: Learner


type PetRecognitionConfigEntry = ConfigEntry[PetRecognitionData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the crops of the examples and questions, and follow the entities the sources rename."""
    hass.http.register_view(ExampleView())
    hass.http.register_view(PictureView())
    async_follow_renames(hass)
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

    journal = await async_get_journal(hass, entry.entry_id)
    # What concerns sources, entities and pets removed meanwhile.
    await journal.async_keep(
        {source_id: subentry.data[CONF_ENTITIES] for source_id, subentry in sources(entry).items()},
        set(pets(entry)),
    )
    analyzer = Analyzer(hass, entry, coordinator, journal, examples)
    entry.async_on_unload(analyzer.async_stop)
    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, analyzer.async_stop))

    @callback
    def _engine_updated() -> None:
        _reembed()
        analyzer.async_engine_updated()

    # Also keeps the engine watched even if the user disables every entity.
    entry.async_on_unload(coordinator.async_add_listener(_engine_updated))
    _engine_updated()

    settings = {
        source_id: source_settings(entry, subentry)
        for source_id, subentry in sources(entry).items()
    }
    # Before the purge, which keeps whole the visits it groups.
    history = History(journal, {source_id: each.visits for source_id, each in settings.items()})
    entry.async_on_unload(history.async_start())

    async def _async_purge(_now: datetime | None = None) -> None:
        before = dt_util.utcnow() - timedelta(days=entry_options(entry).retention_days)
        await journal.async_purge(before.timestamp())

    await _async_purge()
    entry.async_on_unload(
        async_track_time_interval(hass, _async_purge, timedelta(hours=1), cancel_on_shutdown=True)
    )
    entry.async_on_unload(SourceWatcher(hass, entry, journal, analyzer.async_wake).async_start())
    entry.async_on_unload(CameraWatcher(hass, entry, journal, analyzer.async_wake).async_start())
    entry.async_on_unload(async_watch_entities(hass, entry))

    counter = VisitCounter(hass, journal, history)
    learner = Learner(coordinator, journal, examples)
    asker = Asker(hass, entry, journal, history, examples, counter, learner, settings)
    rematcher = Rematcher(hass, entry, journal, examples, settings, entry_options(entry).match)
    async_check_phones(hass, entry_options(entry).phones)
    entry.runtime_data = PetRecognitionData(
        client, coordinator, examples, journal, history, counter, analyzer, learner
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(asker.async_start())
    # Once the event entities listen, so that a visit that ended while Home
    # Assistant was stopped is announced to them.
    entry.async_on_unload(counter.async_start())
    entry.async_on_unload(rematcher.async_start())
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
    async_delete_source_issues(hass)
    async_delete_phone_issues(hass)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PetRecognitionConfigEntry) -> None:
    """Delete the examples, the journal and the files of a deleted entry.

    Its open questions are removed from the phones first.
    """
    await async_clear_questions(hass, await async_get_journal(hass, entry.entry_id))
    await async_remove_journal(hass, entry.entry_id)
    await async_remove_data(hass, entry.entry_id)
