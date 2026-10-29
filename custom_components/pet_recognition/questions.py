"""Questions: ask on the phones who was at a source, when unsure.

Once a visit has ended, each of its runs that stays unknown becomes a
question (see `core.doubts`), sent to each phone of the options: who was at
the source at the time of the picture, with the crop of the animal, a
button for each of the two likeliest pets, and Other, since Android shows
three buttons. A question is asked at most two hours after its visit ended.

While a pet expected at a source has no example yet, one question at a time
asks for the first ones, if the options say so. Once each has examples, a
source asks when unsure, if its settings say so. Nothing is asked without
phones.

A question is saved before it is sent, with the phones it is sent to, so a
restart sends it to the phones still missing. A phone that fails is tried
again later, longer after each failure, for as long as the question can be
asked. A question that is settled or expires is removed from the phones.

An answer is the action of a button, `PET_RECOGNITION|<question>|<choice>`,
which the companion app fires as an event with the user it was registered
for. Any other action is left to other automations. An answer counts only
for an open question, once, and from the user of one of the phones asked.
A pet corrects the pictures of the question (see `core.corrections`), which
settles it and removes it from every phone, and teaches an example of that
pet (see `learning`). Other closes the question
without telling who it was, and leaves the visit to review.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.corrections import answer
from .core.doubts import Doubt, Kind, Question, QuestionSettings, State, questions, review
from .counts import VisitCounter
from .examples import ExampleStore, Origin
from .files import new_id
from .history import History
from .journal import Asked, Journal
from .learning import Learner
from .options import entry_options
from .pets import pets
from .phones import PhoneMissingError, async_clear, async_notify, phone_users
from .sources import SourceSettings
from .views import signed_picture_url

_LOGGER = logging.getLogger(__name__)

# Seconds after the end of a visit during which its runs can be asked about,
# and during which a learning question stays open.
ASK_FOR = 2 * 3600
# Seconds before sending again to a phone that failed, doubled after each failure.
FIRST_RETRY = 60
LAST_RETRY = 1800
# Pets on the buttons, with Other.
CHOICES = 2
# What an answer starts with, and the choice of none of the pets shown.
ACTION = "PET_RECOGNITION"
OTHER = "other"
GROUP = "pet_recognition"
# The event of the companion apps when a button of a notification is pressed.
EVENT_ACTION = "mobile_app_notification_action"


def notification_tag(doubt_id: str) -> str:
    """The tag of the notification of a question, which removes it from the phones."""
    return f"pet_recognition_{doubt_id}"


def action_id(doubt_id: str, choice: str) -> str:
    """The action of a button: the question, and a pet key or Other."""
    return f"{ACTION}|{doubt_id}|{choice}"


async def async_clear_questions(hass: HomeAssistant, journal: Journal) -> None:
    """Remove the open questions of a journal from the phones, such as for a deleted entry."""
    for asked in journal.asked:
        if asked.open:
            await async_clear(hass, asked.sent, notification_tag(asked.doubt.id))


class Asker:
    """Asks the questions of the sources of one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        journal: Journal,
        history: History,
        examples: ExampleStore,
        counter: VisitCounter,
        learner: Learner,
        settings: Mapping[str, SourceSettings],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._journal = journal
        self._history = history
        self._examples = examples
        self._counter = counter
        self._learner = learner
        self._settings = dict(settings)
        # One check at a time, so that a question is asked and sent once.
        self._lock = asyncio.Lock()
        self._timer: CALLBACK_TYPE | None = None
        # By question: the failures so far, and when to try again.
        self._retries: dict[str, tuple[int, float]] = {}
        # The phones that are gone, until the entry loads again.
        self._missing: set[str] = set()

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Ask when the visits change, send what a restart left unsent, and hear answers."""
        stop_listening = self._counter.async_add_listener(self.async_check)
        stop_answers = self._hass.bus.async_listen(EVENT_ACTION, self._async_action)
        self.async_check()

        @callback
        def _async_stop() -> None:
            stop_listening()
            stop_answers()
            self._cancel()

        return _async_stop

    @callback
    def async_check(self, _now: datetime | None = None) -> None:
        """Review and ask, once what is being sent is sent."""
        self._entry.async_create_background_task(
            self._hass, self._async_check(), "pet_recognition ask questions"
        )

    async def _async_check(self) -> None:
        async with self._lock:
            await self._async_check_now()

    @callback
    def _async_action(self, event: Event) -> None:
        """Take an answer, leaving the actions of other notifications alone."""
        action = event.data.get("action")
        if not isinstance(action, str) or not action.startswith(f"{ACTION}|"):
            return
        self._entry.async_create_background_task(
            self._hass,
            self._async_answer(action, event.context.user_id),
            "pet_recognition take an answer",
        )

    async def _async_answer(self, action: str, user: str | None) -> None:
        async with self._lock:
            await self._async_answer_now(action, user)

    async def _async_answer_now(self, action: str, user: str | None) -> None:
        _prefix, _, rest = action.partition("|")
        doubt_id, _, choice = rest.partition("|")
        asked = next((item for item in self._journal.asked if item.doubt.id == doubt_id), None)
        if asked is None or not asked.open:
            # Another entry's, answered already, or closed.
            _LOGGER.debug("An answer to no open question is ignored")
            return
        if user is None or user not in phone_users(self._hass, asked.phones):
            _LOGGER.warning("An answer from a user whose phones were not asked is ignored")
            return
        doubt = asked.doubt
        if choice == OTHER:
            doubt.state = State.DISMISSED
            self._journal.async_closed()
            await async_clear(self._hass, asked.sent, notification_tag(doubt.id))
            return
        if choice not in asked.choices or choice not in pets(self._entry):
            _LOGGER.warning("An answer with a pet that was not offered is ignored")
            return
        correction = answer(
            doubt,
            [choice],
            self._history.visits(doubt.source),
            self._journal.doubts(doubt.source),
        )
        if correction is None:
            # Told since, which settles the question at the next check.
            return
        # The check that follows settles the question, and removes it from the phones.
        await self._learner.async_correct(correction, Origin.ANSWER)

    async def _async_check_now(self) -> None:
        self._cancel()
        now = dt_util.utcnow().timestamp()
        closed = [asked for asked in self._journal.asked if self._gone(asked)]
        for asked in closed:
            asked.doubt.state = State.EXPIRED
        for source, settings in self._settings.items():
            visits = self._history.visits(source)
            timing = _timing(settings)
            closed += map(
                self._journal.question,
                review(source, self._journal.doubts(source), visits, now, timing),
            )
            for question in self._questions(source, settings, now):
                asked = self._asked(question, settings, now)
                await self._journal.async_ask(asked)
        if closed:
            self._journal.async_closed()
        for asked in closed:
            self._retries.pop(asked.doubt.id, None)
            await async_clear(self._hass, asked.sent, notification_tag(asked.doubt.id))
        for asked in list(self._journal.asked):
            if self._due(asked, now):
                await self._async_send(asked, now)
        self._schedule(now)

    def _gone(self, asked: Asked) -> bool:
        """An open question about a source that was removed."""
        return asked.open and asked.doubt.source not in self._settings

    def _questions(self, source: str, settings: SourceSettings, now: float) -> list[Question]:
        options = entry_options(self._entry)
        if not options.phones or not settings.pets:
            return []
        return questions(
            source,
            self._history.visits(source),
            self._journal.doubts(source),
            now,
            settings.visits,
            _timing(settings),
            learned=set(settings.pets) <= self._examples.gallery.pets(),
            ask_when_unsure=settings.ask_when_unsure,
            ask_while_learning=options.ask_while_learning,
        )

    def _asked(self, question: Question, settings: SourceSettings, now: float) -> Asked:
        """The question to send, with the crop and the pets it shows."""
        shown = next(
            record
            for record in self._journal.of(question.visit.source)
            if record.id == question.run.shown.id
        )
        crop = None
        scores: dict[str, float] = {}
        if shown.detections:
            largest = max(shown.detections, key=lambda detection: detection.area)
            crop, scores = largest.crop, largest.scores
        # The pets without examples, which have no score, come last.
        ranked = sorted(settings.pets, key=lambda pet: -scores.get(pet, -2.0))
        return Asked(
            doubt=Doubt.ask(question, new_id(), now),
            taken=shown.taken,
            crop=crop,
            choices=tuple(ranked[:CHOICES]),
            phones=entry_options(self._entry).phones,
        )

    def _due(self, asked: Asked, now: float) -> bool:
        """An open question that phones still miss, and that is time to send."""
        if not asked.open or now - asked.doubt.asked > ASK_FOR:
            return False
        if not set(asked.phones) - set(asked.sent) - self._missing:
            return False
        _failures, retry = self._retries.get(asked.doubt.id, (0, now))
        return retry <= now

    async def _async_send(self, asked: Asked, now: float) -> None:
        message, data = await self._notification(asked)
        failed = False
        for phone in asked.phones:
            if phone in asked.sent or phone in self._missing:
                continue
            try:
                await async_notify(self._hass, phone, message, data)
            except PhoneMissingError as err:
                _LOGGER.warning("A phone to ask is gone: %s", err)
                self._missing.add(phone)
                continue
            except HomeAssistantError as err:
                _LOGGER.debug("Cannot ask a phone, which is tried again later: %s", err)
                failed = True
                continue
            self._journal.async_sent(asked, phone)
        if not failed:
            self._retries.pop(asked.doubt.id, None)
            return
        failures = self._retries.get(asked.doubt.id, (0, now))[0] + 1
        delay = min(FIRST_RETRY * 2 ** (failures - 1), LAST_RETRY)
        self._retries[asked.doubt.id] = (failures, now + delay)

    async def _notification(self, asked: Asked) -> tuple[str, dict[str, Any]]:
        """The message and the data of the notification of a question."""
        doubt = asked.doubt
        texts = await _texts(self._hass)
        message = texts[f"question_{doubt.kind}"].format(
            source=self._entry.subentries[doubt.source].title,
            time=dt_util.as_local(dt_util.utc_from_timestamp(asked.taken)).strftime("%H:%M"),
        )
        names = pets(self._entry)
        actions = [
            {"action": action_id(doubt.id, pet), "title": names[pet].title}
            for pet in asked.choices
            if pet in names
        ]
        actions.append({"action": action_id(doubt.id, OTHER), "title": texts["question_other"]})
        data: dict[str, Any] = {
            "tag": notification_tag(doubt.id),
            "group": GROUP,
            "actions": actions,
        }
        if asked.crop is not None:
            data["image"] = signed_picture_url(self._hass, self._entry.entry_id, asked.crop)
        return message, data

    @callback
    def _schedule(self, now: float) -> None:
        """Check again when a learning question expires, or a phone is to be tried again."""
        times: list[float] = []
        for asked in self._journal.asked:
            if not asked.open:
                continue
            if asked.doubt.kind is Kind.LEARNING:
                times.append(asked.doubt.asked + ASK_FOR + 1)
            if (retry := self._retries.get(asked.doubt.id)) and self._due(asked, retry[1]):
                times.append(retry[1])
        if times:
            self._timer = async_call_later(self._hass, max(min(times) - now, 0), self.async_check)

    @callback
    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None


def _timing(settings: SourceSettings) -> QuestionSettings:
    """How long after its visit a run can be asked about, once the visit ended."""
    return QuestionSettings(
        recent=settings.visits.gap + settings.visits.late + ASK_FOR, learning_expiry=ASK_FOR
    )


async def _texts(hass: HomeAssistant) -> dict[str, str]:
    """The texts of the questions, in the language of Home Assistant."""
    translations = await async_get_translations(hass, hass.config.language, "exceptions", {DOMAIN})
    prefix = f"component.{DOMAIN}.exceptions."
    return {
        key: translations[f"{prefix}{key}.message"]
        for key in (*(f"question_{kind}" for kind in Kind), "question_other")
    }
