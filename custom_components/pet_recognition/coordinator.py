"""Watch the engine's health, and raise repairs when it needs the user.

The entry loads even while the engine is stopped: the coordinator then fails
its updates, the connectivity sensor turns off, and after a quarter of an
hour without a good answer a repair says so. A refused key starts the
reauthentication flow, and polling waits for the new key. An engine that no
longer serves this integration's API, or of another release than the
integration, gets a repair that says which side to update.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from awesomeversion import AwesomeVersion
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .engine import (
    API,
    EngineAuthError,
    EngineClient,
    EngineConnectionError,
    EngineError,
    EngineIncompatibleError,
    EngineInfo,
)

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=1)
UNREACHABLE_AFTER = timedelta(minutes=15)

ISSUE_UNREACHABLE = "engine_unreachable"
ISSUE_INCOMPATIBLE = "engine_incompatible"
ISSUE_VERSION = "engine_version_mismatch"
ISSUES = (ISSUE_UNREACHABLE, ISSUE_INCOMPATIBLE, ISSUE_VERSION)

RELEASE = re.compile(r"\d+\.\d+\.\d+")
# The version of a build without one, as of an engine built by hand.
UNKNOWN_VERSION = "0.0.0"


class EngineCoordinator(DataUpdateCoordinator[EngineInfo]):
    """Asks the engine what it is, every minute."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EngineClient, version: str
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        # The integration's own release, to compare with the engine's.
        self.version = version
        self.failing_since: datetime | None = None

    async def _async_update_data(self) -> EngineInfo:
        try:
            info = await self.client.info()
        except EngineAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except EngineIncompatibleError as err:
            # The engine answers: it is not unreachable, only newer.
            self.failing_since = None
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_UNREACHABLE)
            self._incompatible()
            raise _update_failed("not_an_engine", err) from err
        except EngineConnectionError as err:
            self._unreachable()
            raise _update_failed("cannot_connect", err) from err
        except EngineError as err:
            # Something answers, but not as an engine: another service at the
            # address, or an engine that fails. Either way it cannot be used.
            self._unreachable()
            raise _update_failed("not_an_engine", err) from err
        self.failing_since = None
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_UNREACHABLE)
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_INCOMPATIBLE)
        self._compare_versions(info.version)
        return info

    def _unreachable(self) -> None:
        """Count a failed update, and raise a repair once failures last."""
        now = dt_util.utcnow()
        if self.failing_since is None:
            self.failing_since = now
        if now - self.failing_since >= UNREACHABLE_AFTER:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_UNREACHABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_UNREACHABLE,
                translation_placeholders={"url": self.client.url},
            )

    def _incompatible(self) -> None:
        """A repair for an engine that dropped this integration's API."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_INCOMPATIBLE,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INCOMPATIBLE,
            translation_placeholders={"url": self.client.url, "api": str(API)},
        )

    def _compare_versions(self, engine: str) -> None:
        """A repair while the engine and the integration are of different releases.

        Only releases are compared: an engine or integration built without a
        version, or of a development version, raises nothing.
        """
        ours, theirs = _release(self.version), _release(engine)
        newer = ours is not None and theirs is not None and theirs > ours
        older = ours is not None and theirs is not None and theirs < ours
        if not (newer or older):
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_VERSION)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_VERSION,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="engine_version_newer" if newer else "engine_version_older",
            translation_placeholders={"engine": engine, "integration": self.version},
        )


def _release(version: str) -> AwesomeVersion | None:
    """`version` if it is a release such as 1.2.0, or None."""
    if RELEASE.fullmatch(version) is None or version == UNKNOWN_VERSION:
        return None
    return AwesomeVersion(version)


def _update_failed(key: str, err: EngineError) -> UpdateFailed:
    return UpdateFailed(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"error": str(err)},
    )
