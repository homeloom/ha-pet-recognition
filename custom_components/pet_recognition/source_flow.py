"""The flow of a source: its name, its entities, and how its visits form."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.camera.const import DOMAIN as CAMERA_DOMAIN
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
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
    CONF_ASK_WHEN_UNSURE,
    CONF_CAMERA,
    CONF_ENTITIES,
    CONF_EVERY_PICTURE,
    CONF_FRAMES,
    CONF_GAP,
    CONF_INTERVAL,
    CONF_KIND,
    CONF_LATE,
    CONF_MEAL_PICTURES,
    CONF_PETS,
    CONF_TRIGGER,
    DEFAULT_FRAMES,
    DEFAULT_GAP,
    DEFAULT_INTERVAL,
    DEFAULT_LATE,
    KINDS,
    KINDS_SHOWING_A_PET,
    SUBENTRY_SOURCE,
)
from .pets import pets
from .sources import cameras

KIND_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(KINDS), translation_key="kind", mode=SelectSelectorMode.DROPDOWN
    )
)
ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["image", CAMERA_DOMAIN], multiple=True)
)
TRIGGER_SELECTOR = EntitySelector(EntitySelectorConfig(domain=["binary_sensor", "event"]))


def _number(minimum: int, maximum: int, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def entity_errors(entry: ConfigEntry, entities: list[str], mine: str | None) -> dict[str, str]:
    """The errors of the entities chosen for a source, other than the source `mine`."""
    if not entities:
        return {CONF_ENTITIES: "no_entity"}
    taken = {
        entity_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_SOURCE and subentry_id != mine
        for entity_id in subentry.data[CONF_ENTITIES]
    }
    if taken.intersection(entities):
        return {CONF_ENTITIES: "entity_in_use"}
    return {}


def meal_errors(entities: list[str], meal_pictures: list[str]) -> dict[str, str]:
    """The pictures that mean a meal are some of the source's own."""
    if set(meal_pictures) - set(entities):
        return {CONF_MEAL_PICTURES: "meal_picture_not_watched"}
    return {}


def trigger_errors(entities: list[str], trigger: str | None) -> dict[str, str]:
    """A camera takes pictures only when its trigger fires, so it needs one."""
    if cameras(entities) and not trigger:
        return {"base": "trigger_needed"}
    return {}


class SourceFlow(ConfigSubentryFlow):
    """Add a source of pictures, or change one."""

    def __init__(self) -> None:
        self._name = ""
        self._kind = ""
        self._entities: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Name a new source, say what it is, and where its pictures come from."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = user_input[CONF_NAME].strip()
            self._kind = user_input[CONF_KIND]
            self._entities = list(dict.fromkeys(user_input.get(CONF_ENTITIES) or []))
            errors = self._check_entities(self._entities)
            if not self._name:
                errors[CONF_NAME] = "empty_name"
            if not errors:
                return await self.async_step_details()
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_KIND, default=KINDS[0]): KIND_SELECTOR,
                vol.Required(CONF_ENTITIES): ENTITY_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_details(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose the pets of a new source, how its cameras take pictures, and its visits."""
        camera = bool(cameras(self._entities))
        errors: dict[str, str] = {}
        if user_input is not None:
            # The selector keeps the meal pictures among the entities.
            errors = self._check_trigger(self._entities, user_input)
            if not errors:
                return self.async_create_entry(
                    title=self._name, data=self._data(self._entities, user_input)
                )
        defaults: dict[str, Any] = {
            CONF_PETS: [],
            CONF_ASK_WHEN_UNSURE: True,
            CONF_ADVANCED: {
                CONF_GAP: DEFAULT_GAP,
                CONF_LATE: DEFAULT_LATE,
                # A camera takes pictures whether a pet is there or not.
                CONF_EVERY_PICTURE: self._kind in KINDS_SHOWING_A_PET and not camera,
            },
        }
        if camera:
            defaults[CONF_CAMERA] = {CONF_FRAMES: DEFAULT_FRAMES, CONF_INTERVAL: DEFAULT_INTERVAL}
        return self.async_show_form(
            step_id="details",
            data_schema=self.add_suggested_values_to_schema(
                self._schema(self._entities, camera=camera), user_input or defaults
            ),
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
            entities = list(dict.fromkeys(user_input.get(CONF_ENTITIES) or []))
            errors = (
                self._check_entities(entities)
                or self._check_trigger(entities, user_input)
                or self._check_meals(entities, user_input)
            )
            if not name:
                errors[CONF_NAME] = "empty_name"
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=name, data=self._data(entities, user_input)
                )
        data = subentry.data
        current = {
            CONF_NAME: subentry.title,
            CONF_KIND: data[CONF_KIND],
            CONF_ENTITIES: data[CONF_ENTITIES],
            CONF_PETS: [key for key in data[CONF_PETS] if key in pets(self._get_entry())],
            CONF_ASK_WHEN_UNSURE: data.get(CONF_ASK_WHEN_UNSURE, True),
            CONF_MEAL_PICTURES: data.get(CONF_MEAL_PICTURES, []),
            CONF_CAMERA: {
                CONF_TRIGGER: data[CONF_TRIGGER],
                CONF_FRAMES: data[CONF_FRAMES],
                CONF_INTERVAL: data[CONF_INTERVAL],
            },
            CONF_ADVANCED: {
                CONF_GAP: data[CONF_GAP],
                CONF_LATE: data[CONF_LATE],
                CONF_EVERY_PICTURE: data[CONF_EVERY_PICTURE],
            },
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_KIND): KIND_SELECTOR,
                vol.Required(CONF_ENTITIES): ENTITY_SELECTOR,
                **self._schema(
                    data[CONF_ENTITIES], camera=True, collapsed=not cameras(data[CONF_ENTITIES])
                ).schema,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or current),
            errors=errors,
        )

    def _schema(self, entities: list[str], *, camera: bool, collapsed: bool = False) -> vol.Schema:
        options = [
            SelectOptionDict(value=key, label=subentry.title)
            for key, subentry in pets(self._get_entry()).items()
        ]
        fields: dict[Any, Any] = {
            vol.Optional(CONF_PETS): SelectSelector(
                SelectSelectorConfig(options=options, multiple=True)
            ),
            vol.Required(CONF_ASK_WHEN_UNSURE, default=True): BooleanSelector(),
            # Among the source's own entities.
            vol.Optional(CONF_MEAL_PICTURES): EntitySelector(
                EntitySelectorConfig(include_entities=list(entities), multiple=True)
            ),
        }
        if camera:
            fields[vol.Required(CONF_CAMERA)] = section(
                vol.Schema(
                    {
                        vol.Optional(CONF_TRIGGER): TRIGGER_SELECTOR,
                        vol.Required(CONF_FRAMES, default=DEFAULT_FRAMES): _number(1, 20),
                        vol.Required(CONF_INTERVAL, default=DEFAULT_INTERVAL): _number(1, 60, "s"),
                    }
                ),
                {"collapsed": collapsed},
            )
        fields[vol.Required(CONF_ADVANCED)] = section(
            vol.Schema(
                {
                    vol.Required(CONF_GAP): _number(1, 120, "min"),
                    vol.Required(CONF_LATE): _number(0, 60, "min"),
                    vol.Required(CONF_EVERY_PICTURE): BooleanSelector(),
                }
            ),
            {"collapsed": True},
        )
        return vol.Schema(fields)

    def _check_entities(self, entities: list[str]) -> dict[str, str]:
        mine = None if self.source == SOURCE_USER else self._get_reconfigure_subentry().subentry_id
        return entity_errors(self._get_entry(), entities, mine)

    @staticmethod
    def _check_meals(entities: list[str], user_input: dict[str, Any]) -> dict[str, str]:
        return meal_errors(entities, user_input.get(CONF_MEAL_PICTURES) or [])

    @staticmethod
    def _check_trigger(entities: list[str], user_input: dict[str, Any]) -> dict[str, str]:
        return trigger_errors(entities, user_input.get(CONF_CAMERA, {}).get(CONF_TRIGGER))

    def _data(self, entities: list[str], user_input: dict[str, Any]) -> dict[str, Any]:
        advanced = user_input[CONF_ADVANCED]
        camera = user_input.get(CONF_CAMERA, {})
        return {
            CONF_KIND: self._kind,
            CONF_ENTITIES: entities,
            CONF_PETS: list(user_input.get(CONF_PETS, [])),
            CONF_ASK_WHEN_UNSURE: bool(user_input[CONF_ASK_WHEN_UNSURE]),
            CONF_MEAL_PICTURES: list(dict.fromkeys(user_input.get(CONF_MEAL_PICTURES) or [])),
            CONF_GAP: int(advanced[CONF_GAP]),
            CONF_LATE: int(advanced[CONF_LATE]),
            CONF_EVERY_PICTURE: bool(advanced[CONF_EVERY_PICTURE]),
            # Kept when the cameras go, so that they come back as they were.
            CONF_TRIGGER: camera.get(CONF_TRIGGER) or None,
            CONF_FRAMES: int(camera.get(CONF_FRAMES, DEFAULT_FRAMES)),
            CONF_INTERVAL: int(camera.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
        }
