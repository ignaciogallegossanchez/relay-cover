"""Cover platform for the Relay cover integration."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time
from typing import Any, Self, override

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoreEntity,
    async_get as async_get_restore_state,
)

from . import RelayCoverConfigEntry, async_switch_relay
from .const import DOMAIN
from .motion import MotionController, PositionUnknownError, Relay, RelayUnavailableError
from .travel import CLOSED, OPEN, Direction

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Position refresh while moving: 1 s is 3-5 % of a usual travel, enough for a slider
REFRESH_INTERVAL = timedelta(seconds=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RelayCoverConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the relay cover from a config entry."""
    async_add_entities([RelayCover(entry)])


def _relay_value(state: State | None) -> bool | None:
    """Return on/off, or None when the relay is missing, unavailable or unknown."""
    if state is None or state.state not in (STATE_ON, STATE_OFF):
        return None
    return state.state == STATE_ON


@dataclass(frozen=True, slots=True)
class RelayCoverExtraStoredData(ExtraStoredData):
    """Position saved apart from the state, which has none while unavailable."""

    position: float | None

    @override
    def as_dict(self) -> dict[str, Any]:
        """Return the data to store."""
        return {"position": self.position}

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Return the stored data, None if it is not ours or is damaged."""
        match restored.get("position"):
            case None:
                return cls(None)
            case bool():
                return None
            case int() | float() as position if CLOSED <= position <= OPEN:
                return cls(float(position))
            case _:
                return None


class RelayCover(RestoreEntity, CoverEntity):
    """A cover driven by an open relay and a close relay."""

    _attr_assumed_state = True
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, entry: RelayCoverConfigEntry) -> None:
        """Initialize the cover on a device of its own."""
        config = entry.runtime_data
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )
        self._relay_ids = {
            Relay.OPEN: config.open_relay,
            Relay.CLOSE: config.close_relay,
        }
        self._relays_by_id = {v: k for k, v in self._relay_ids.items()}
        self._controller = MotionController(
            entry.title,
            config.opening_time,
            config.closing_time,
            config.interlock,
            config.end_margin / 100,
        )
        self._command_lock = asyncio.Lock()
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._cancel_refresh: CALLBACK_TYPE | None = None
        self._removed = False
        self._restored = False
        # The cover showed a direction at the last check of the save
        self._was_active = False
        self._saved_position: float | None = None
        # A relay call failed and none has worked since: later failures are logged quietly
        self._relay_failing = False

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the position, follow the relays and catch up with their state."""
        await super().async_added_to_hass()
        # Home Assistant before 2026.11 removes and re-adds the entity on an entity_id change
        self._removed = False
        self.async_on_remove(self._async_on_removed)
        if not self._restored:
            # Once: on a re-add the controller already knows better than the saved state
            self._restored = True
            await self._async_restore_position()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, list(self._relay_ids.values()), self._async_relay_changed
            )
        )
        now = time.monotonic()
        for relay, entity_id in self._relay_ids.items():
            self._controller.relay_reported(
                relay, _relay_value(self.hass.states.get(entity_id)), now
            )
        # A relay still on after a restart may need the other one switched off
        await self._async_apply_logged()

    async def _async_restore_position(self) -> None:
        """Take the saved position."""
        if (extra := await self.async_get_last_extra_data()) is not None and (
            data := RelayCoverExtraStoredData.from_dict(extra.as_dict())
        ) is not None:
            self._controller.restore_position(data.position)
            self._saved_position = data.position

    @property
    @override
    def extra_restore_state_data(self) -> RelayCoverExtraStoredData:
        """Return the position to save, also while unavailable."""
        return RelayCoverExtraStoredData(
            self._controller.calc.position(time.monotonic())
        )

    # Overrides the hook only from Home Assistant 2026.11; mypy also runs against 2026.10
    @callback
    @override
    def async_entity_id_changed(self, old_entity_id: str) -> None:  # type: ignore[misc, unused-ignore]
        """Change the entity_id in place: nothing here is keyed on it."""
        super().async_entity_id_changed(old_entity_id)  # type: ignore[misc, unused-ignore]

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._async_move(OPEN)

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._async_move(CLOSED)

    @override
    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a position."""
        await self._async_move(kwargs[ATTR_POSITION])

    async def _async_move(self, target: float) -> None:
        """Ask the controller for a position and switch the relays."""
        try:
            self._controller.request_move(target, time.monotonic())
        except PositionUnknownError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="position_unknown"
            ) from err
        except RelayUnavailableError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="relay_unavailable"
            ) from err
        await self._async_apply()

    @override
    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        self._controller.request_stop(time.monotonic())
        await self._async_apply()

    async def _async_relay_changed(self, event: Event[EventStateChangedData]) -> None:
        """Follow a relay: our own echo or a change made outside."""
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]
        if (
            new_state is not None
            and old_state is not None
            and new_state.state == old_state.state
        ):
            return
        relay = self._relays_by_id[event.data["entity_id"]]
        self._controller.relay_reported(
            relay, _relay_value(new_state), time.monotonic()
        )
        await self._async_apply_logged()

    async def _async_on_timer(self, _now: datetime) -> None:
        """Arrival, end of the dead time, or a command to send again."""
        self._cancel_timer = None
        self._controller.advance(time.monotonic())
        await self._async_apply_logged()

    @callback
    def _async_refresh_position(self, _now: datetime) -> None:
        """Show the position while moving."""
        position_before = self._attr_current_cover_position
        self._async_update_attrs()
        if self._attr_current_cover_position != position_before:
            self.async_write_ha_state()
        self._async_save_when_due(time.monotonic())

    def _log_failure(self, err: Exception) -> None:
        """Log a failed relay call: the first of a row as an error, the rest for debug."""
        level = logging.DEBUG if self._relay_failing else logging.ERROR
        self._relay_failing = True
        if isinstance(err, HomeAssistantError):
            _LOGGER.log(level, "%s: could not switch a relay: %s", self.entity_id, err)
        else:
            _LOGGER.log(
                level,
                "%s: unexpected error switching a relay",
                self.entity_id,
                exc_info=err,
            )

    async def _async_apply(self) -> None:
        """Send the relay commands for an action of the user; raise the first error."""
        if (err := await self._async_send()) is not None:
            raise err

    async def _async_apply_logged(self) -> None:
        """Send the relay commands where nobody can receive the error: log it."""
        if (err := await self._async_send()) is not None:
            self._log_failure(err)

    async def _async_send(self) -> Exception | None:
        """Send the relay commands the controller wants, one at a time, then publish.

        A call that fails does not stop the others; the controller sends its command again
        when Home Assistant still does not show it. At most one command per relay goes out
        here and the timer sends the rest, so that a call that hangs never holds an action
        for good. Return the first error; later ones are logged.
        """
        sent: set[Relay] = set()
        first: Exception | None = None
        try:
            async with self._command_lock:
                # Unloaded during a slow relay call: nobody would switch the relay off again
                while (
                    not self._removed
                    and (command := self._controller.next_command(time.monotonic()))
                    is not None
                    and command[0] not in sent
                ):
                    relay, value = command
                    sent.add(relay)
                    self._controller.command_sent(relay, value, time.monotonic())
                    try:
                        # Child context, so the logbook shows who switched the relay
                        await async_switch_relay(
                            self.hass,
                            self._relay_ids[relay],
                            value,
                            Context(
                                parent_id=self._context.id if self._context else None
                            ),
                        )
                    except Exception as err:  # noqa: BLE001 - returned or logged
                        if first is None:
                            first = err
                        else:
                            self._log_failure(err)
                    else:
                        if self._relay_failing:
                            _LOGGER.info("%s: the relays respond again", self.entity_id)
                        self._relay_failing = False
        finally:
            self._async_publish()
        return first

    @callback
    def _async_publish(self) -> None:
        """Write the state once and set the timers for what comes next."""
        if self._removed:
            # A relay call still running when the entry was unloaded
            return
        self._async_update_attrs()
        self.async_write_ha_state()
        now = time.monotonic()
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if (deadline := self._controller.next_deadline(now)) is not None:
            self._cancel_timer = async_call_later(
                self.hass, deadline - now, self._async_on_timer
            )
        moving = self._controller.calc.direction is not None
        if moving and self._cancel_refresh is None:
            self._cancel_refresh = async_track_time_interval(
                self.hass, self._async_refresh_position, REFRESH_INTERVAL
            )
        elif not moving and self._cancel_refresh is not None:
            self._cancel_refresh()
            self._cancel_refresh = None
        self._async_save_when_due(now)

    @callback
    def _async_save_when_due(self, now: float) -> None:
        """Save the position when the cover stops, and once when it runs into an end.

        The pause of a reversal is no stop: the cover still shows the new direction. A relay
        switched on from outside may stay on at the end for good.
        """
        calc = self._controller.calc
        position = calc.position(now)
        active = self._controller.shown_direction() is not None
        stopped = self._was_active and not active
        self._was_active = active
        at_end = calc.direction is not None and position == calc.direction.end
        if (stopped or at_end) and position != self._saved_position:
            self._saved_position = position
            self._async_save_position()

    @callback
    def _async_save_position(self) -> None:
        """Save the restore states now, not at the next dump up to 15 min later.

        After a power cut Home Assistant restores what was saved last.
        """
        self._entry.async_create_background_task(
            self.hass,
            async_get_restore_state(self.hass).async_dump_states(),
            f"{DOMAIN} save the position of {self.entity_id}",
        )

    @callback
    def _async_update_attrs(self) -> None:
        position = self._controller.position(time.monotonic())
        direction = self._controller.shown_direction()
        self._attr_available = self._controller.available
        self._attr_current_cover_position = position
        self._attr_is_opening = direction is Direction.OPENING
        self._attr_is_closing = direction is Direction.CLOSING
        self._attr_is_closed = None if position is None else position == 0

    @callback
    def _async_on_removed(self) -> None:
        self._removed = True
        for cancel in (self._cancel_timer, self._cancel_refresh):
            if cancel is not None:
                cancel()
        self._cancel_timer = self._cancel_refresh = None
