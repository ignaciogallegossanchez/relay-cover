"""Helpers shared by the Relay cover tests."""

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
import itertools
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.cover import ATTR_CURRENT_POSITION
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, Platform
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State
from homeassistant.helpers import event
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed_exact,
    mock_restore_cache_with_extra_data,
)

from custom_components.fake_relay.switch import FakeRelay

OPEN_RELAY = "switch.up"
CLOSE_RELAY = "switch.down"
COVER = "cover.living_room"
# Clock step of the tests; travel times are chosen so that stops fall on it
STEP = 0.25


def restore_saved_position(hass: HomeAssistant, position: float) -> None:
    """Have the cover find a position saved before the restart."""
    mock_restore_cache_with_extra_data(
        hass, [(State(COVER, STATE_UNKNOWN), {"position": position})]
    )


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move the clock in steps, letting every job finish after each one."""
    for _ in range(round(seconds / STEP)):
        freezer.tick(timedelta(seconds=STEP))
        async_fire_time_changed_exact(hass)
        await hass.async_block_till_done()


async def steps(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, count: int
) -> None:
    """Move the clock without waiting for slow relay calls to finish."""
    for _ in range(count):
        freezer.tick(timedelta(seconds=STEP))
        async_fire_time_changed_exact(hass)
        for _ in range(5):
            await asyncio.sleep(0)


async def call(hass: HomeAssistant, service: str, **data: int) -> None:
    """Call a cover action and wait for it."""
    await hass.services.async_call(
        Platform.COVER, service, {ATTR_ENTITY_ID: COVER, **data}, blocking=True
    )
    await hass.async_block_till_done()


def start_call(hass: HomeAssistant, service: str, **data: int) -> asyncio.Task[None]:
    """Call a cover action without waiting (not a hass task: the clock is frozen)."""
    return asyncio.create_task(
        hass.services.async_call(
            Platform.COVER, service, {ATTR_ENTITY_ID: COVER, **data}, blocking=True
        )
    )


def cover_state(hass: HomeAssistant) -> tuple[str, int | None]:
    """Return the state and position of the cover."""
    state = hass.states.get(COVER)
    assert state is not None
    return state.state, state.attributes.get(ATTR_CURRENT_POSITION)


def relay_states(hass: HomeAssistant) -> tuple[str, str]:
    """Return the states of the open and the close relay."""
    up, down = hass.states.get(OPEN_RELAY), hass.states.get(CLOSE_RELAY)
    assert up is not None
    assert down is not None
    return up.state, down.state


def switch_calls(events: list[Event]) -> list[tuple[str, str]]:
    """Return the relay actions in captured call_service events."""
    return [
        (e.data["service_data"][ATTR_ENTITY_ID], e.data["service"])
        for e in events
        if e.data["domain"] == Platform.SWITCH
    ]


def state_changes(events: list[Event], entity_id: str) -> list[tuple[str, int | None]]:
    """Return state and position of each captured state_changed of an entity."""
    return [
        (
            e.data["new_state"].state,
            e.data["new_state"].attributes.get(ATTR_CURRENT_POSITION),
        )
        for e in events
        if e.data["entity_id"] == entity_id and e.data["new_state"] is not None
    ]


class CoverTimers:
    """Timers and intervals of the covers not yet fired or cancelled.

    It stands in for the scheduling helpers the cover module imports (see `conftest.py`).
    """

    def __init__(self) -> None:
        """Start with no timers."""
        self._live: set[int] = set()
        self._ids = itertools.count()

    @property
    def live(self) -> int:
        """Return how many timers and intervals are live."""
        return len(self._live)

    def call_later(
        self,
        hass: HomeAssistant,
        delay: float,
        action: Callable[[datetime], Coroutine[Any, Any, None]],
    ) -> CALLBACK_TYPE:
        """Schedule a timer of the cover, as `async_call_later`."""
        key = next(self._ids)

        async def fire(now: datetime) -> None:
            self._live.discard(key)
            await action(now)

        return self._track(key, event.async_call_later(hass, delay, fire))

    def track_time_interval(
        self,
        hass: HomeAssistant,
        action: Callable[[datetime], None],
        interval: timedelta,
    ) -> CALLBACK_TYPE:
        """Schedule an interval of the cover, as `async_track_time_interval`."""
        key = next(self._ids)
        return self._track(key, event.async_track_time_interval(hass, action, interval))

    def _track(self, key: int, cancel: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._live.add(key)

        def cancel_and_forget() -> None:
            self._live.discard(key)
            cancel()

        return cancel_and_forget


@contextmanager
def failing_relay_calls(
    service: str, error: Exception | None, count: int = 1
) -> Iterator[list[str]]:
    """Make the next `count` calls of a relay service raise `error`; None loses them.

    A lost call returns normally and the relay does nothing, as an order lost on the way.
    Yields the entity_ids of the calls that failed.
    """
    original = getattr(FakeRelay, f"async_{service}")
    failed: list[str] = []

    async def call_or_fail(self: FakeRelay, **kwargs: Any) -> None:
        if len(failed) >= count:
            await original(self, **kwargs)
            return
        failed.append(self.entity_id)
        if error is not None:
            raise error

    with patch.object(FakeRelay, f"async_{service}", call_or_fail):
        yield failed
