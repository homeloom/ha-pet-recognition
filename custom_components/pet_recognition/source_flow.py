"""The flow of a source: its name, its image entities, and how its visits form."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ADVANCED,
    CONF_ENTITIES,
    CONF_EVERY_PICTURE,
    CONF_GAP,
    CONF_KIND,
    CONF_LATE,
    CONF_PETS,
    DEFAULT_GAP,
    DEFAULT_LATE,
    KINDS,
    KINDS_SHOWING_A_PET,
    SUBENTRY_SOURCE,
)
from .pets import pets

KIND_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(KINDS), translation_key="kind", mode=SelectSelectorMode.DROPDOWN
    )
)
IMAGE_SELECTOR = EntitySelector(EntitySelectorConfig(domain="image", multiple=True))


def _minutes(minimum: int, maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            unit_of_measurement="min",
            mode=NumberSelectorMode.BOX,
        )
    )


class SourceFlow(ConfigSubentryFlow):
    """Add a source of pictures, or change one."""

    def __init__(self) -> None:
        self._name = ""
        self._kind = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Name a new source and say what it is."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = user_input[CONF_NAME].strip()
            self._kind = user_input[CONF_KIND]
            if self._name:
                return await self.async_step_pictures()
            errors[CONF_NAME] = "empty_name"
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_KIND, default=KINDS[0]): KIND_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_pictures(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose the entities of a new source, its pets, and how its visits form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._check(user_input)
            if not errors:
                return self.async_create_entry(title=self._name, data=self._data(user_input))
        defaults = {
            CONF_PETS: [],
            CONF_ADVANCED: {
                CONF_GAP: DEFAULT_GAP,
                CONF_LATE: DEFAULT_LATE,
                CONF_EVERY_PICTURE: self._kind in KINDS_SHOWING_A_PET,
            },
        }
        return self.async_show_form(
            step_id="pictures",
            data_schema=self.add_suggested_values_to_schema(self._schema(), user_input or defaults),
            description_placeholders={"source": self._name},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change a source."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            self._kind = user_input[CONF_KIND]
            errors = self._check(user_input)
            if not name:
                errors[CONF_NAME] = "empty_name"
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=name, data=self._data(user_input)
                )
        current = {
            CONF_NAME: subentry.title,
            CONF_KIND: subentry.data[CONF_KIND],
            CONF_ENTITIES: subentry.data[CONF_ENTITIES],
            CONF_PETS: [key for key in subentry.data[CONF_PETS] if key in pets(self._get_entry())],
            CONF_ADVANCED: {
                CONF_GAP: subentry.data[CONF_GAP],
                CONF_LATE: subentry.data[CONF_LATE],
                CONF_EVERY_PICTURE: subentry.data[CONF_EVERY_PICTURE],
            },
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_KIND): KIND_SELECTOR,
                **self._schema().schema,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or current),
            errors=errors,
        )

    def _schema(self) -> vol.Schema:
        options = [
            SelectOptionDict(value=key, label=subentry.title)
            for key, subentry in pets(self._get_entry()).items()
        ]
        return vol.Schema(
            {
                vol.Required(CONF_ENTITIES): IMAGE_SELECTOR,
                vol.Optional(CONF_PETS): SelectSelector(
                    SelectSelectorConfig(options=options, multiple=True)
                ),
                vol.Required(CONF_ADVANCED): section(
                    vol.Schema(
                        {
                            vol.Required(CONF_GAP): _minutes(1, 120),
                            vol.Required(CONF_LATE): _minutes(0, 60),
                            vol.Required(CONF_EVERY_PICTURE): BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

    def _check(self, user_input: dict[str, Any]) -> dict[str, str]:
        """The errors of the entities chosen for a source."""
        entities = user_input.get(CONF_ENTITIES) or []
        if not entities:
            return {CONF_ENTITIES: "no_entity"}
        mine = None if self.source == SOURCE_USER else self._get_reconfigure_subentry().subentry_id
        taken = {
            entity_id
            for subentry_id, subentry in self._get_entry().subentries.items()
            if subentry.subentry_type == SUBENTRY_SOURCE and subentry_id != mine
            for entity_id in subentry.data[CONF_ENTITIES]
        }
        if taken.intersection(entities):
            return {CONF_ENTITIES: "entity_in_use"}
        return {}

    def _data(self, user_input: dict[str, Any]) -> dict[str, Any]:
        advanced = user_input[CONF_ADVANCED]
        return {
            CONF_KIND: self._kind,
            CONF_ENTITIES: list(dict.fromkeys(user_input[CONF_ENTITIES])),
            CONF_PETS: list(user_input.get(CONF_PETS, [])),
            CONF_GAP: int(advanced[CONF_GAP]),
            CONF_LATE: int(advanced[CONF_LATE]),
            CONF_EVERY_PICTURE: bool(advanced[CONF_EVERY_PICTURE]),
        }
