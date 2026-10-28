"""The options of the entry: how the pictures of the sources are recognized and kept."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_ADVANCED,
    CONF_DETECTION_LIMIT,
    CONF_MARGIN,
    CONF_MIN_AREA,
    CONF_RETENTION,
    CONF_THRESHOLD,
    DEFAULT_MARGIN,
    DEFAULT_MIN_AREA,
    DEFAULT_RETENTION,
    DEFAULT_THRESHOLD,
    DETECTION_LIMIT,
    MAX_DETECTION_LIMIT,
    MAX_RETENTION,
    MIN_RETENTION,
)
from .core.matching import MatchSettings


@dataclass(frozen=True)
class EntryOptions:
    """What the options set, with their defaults for an entry that never set them.

    `min_area` is a share of the picture, from 0 to 1.
    """

    match: MatchSettings
    min_area: float
    detection_limit: int
    retention_days: int


def entry_options(entry: ConfigEntry) -> EntryOptions:
    """The options of `entry`."""
    options = entry.options
    return EntryOptions(
        match=MatchSettings(
            threshold=options.get(CONF_THRESHOLD, DEFAULT_THRESHOLD),
            margin=options.get(CONF_MARGIN, DEFAULT_MARGIN),
        ),
        min_area=options.get(CONF_MIN_AREA, DEFAULT_MIN_AREA) / 100,
        detection_limit=options.get(CONF_DETECTION_LIMIT, DETECTION_LIMIT),
        retention_days=options.get(CONF_RETENTION, DEFAULT_RETENTION),
    )


def _number(minimum: float, maximum: float, step: float, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


class PetRecognitionOptionsFlow(OptionsFlow):
    """The options of the entry, which apply once it loads again."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Tune the recognition, and how long pictures are kept."""
        if user_input is not None:
            advanced = user_input[CONF_ADVANCED]
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_THRESHOLD: float(advanced[CONF_THRESHOLD]),
                    CONF_MARGIN: float(advanced[CONF_MARGIN]),
                    CONF_MIN_AREA: float(advanced[CONF_MIN_AREA]),
                    CONF_DETECTION_LIMIT: int(advanced[CONF_DETECTION_LIMIT]),
                    CONF_RETENTION: int(advanced[CONF_RETENTION]),
                }
            )
        options = entry_options(self.config_entry)
        schema = vol.Schema(
            {
                vol.Required(CONF_ADVANCED): section(
                    vol.Schema(
                        {
                            vol.Required(CONF_THRESHOLD, default=options.match.threshold): _number(
                                0.3, 0.95, 0.01
                            ),
                            vol.Required(CONF_MARGIN, default=options.match.margin): _number(
                                0, 0.3, 0.01
                            ),
                            vol.Required(
                                CONF_MIN_AREA, default=round(options.min_area * 100, 1)
                            ): _number(0.1, 50, 0.1, "%"),
                            vol.Required(
                                CONF_DETECTION_LIMIT, default=options.detection_limit
                            ): _number(1, MAX_DETECTION_LIMIT, 1),
                            vol.Required(CONF_RETENTION, default=options.retention_days): _number(
                                MIN_RETENTION, MAX_RETENTION, 1, "d"
                            ),
                        }
                    ),
                    {"collapsed": False},
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
