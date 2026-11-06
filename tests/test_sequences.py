"""Sequences against the real entity with the fake relay (travel times 17 s and 41 s).

Cases where echoes, late reports, wall presses and reversals mix. The rules are those of
`test_motion`: no margin at the ends unless a test sets one, the dead time is 0.5 s from
the last off even before the relay confirms, an on still queued in the relay is not waited
for, and without interlock off and on go out back to back. The position starts and stops
with what Home Assistant shows; a command not shown goes out again CONFIRM_TIMEOUT (5 s)
after it was sent, not at once (the slow device confirms at 3 s, within it); a glitch is
two presses.
"""

from collections.abc import Awaitable, Callable

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.cover import CoverState
from homeassistant.const import (
    EVENT_CALL_SERVICE,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.fake_relay import async_press_wall_button
from custom_components.relay_cover.motion import CONFIRM_TIMEOUT, DEAD_TIME

from .common import (
    CLOSE_RELAY as DOWN,
    COVER,
    OPEN_RELAY as UP,
    STEP,
    CoverTimers,
    call,
    cover_state,
    relay_states,
    start_call,
    steps,
    switch_calls,
    tick,
)


@pytest.fixture
def opening_time() -> float:
    """Seconds of a full travel up."""
    return 17


@pytest.fixture
def closing_time() -> float:
    """Seconds of a full travel down."""
    return 41


# Echo or external, by value


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.usefixtures("cover")
async def test_cancelled_order_without_event_then_press(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Open and stop before confirming; the off confirmation changes nothing in HA.

    The cover follows a later wall on to 100 and leaves it on there, and stop and
    open from HA still send their commands. The press comes after the 5 s of our
    last command, the off of 1 s; HA never showed up on, so the position starts at 0.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 1)
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, CONFIRM_TIMEOUT + 0.25)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    async_press_wall_button(hass, UP, on=True)
    await hass.async_block_till_done()
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 17 + 5)
    assert switch_calls(calls) == [(UP, SERVICE_TURN_ON), (UP, SERVICE_TURN_OFF)]
    assert cover_state(hass) == (CoverState.OPENING, 100)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    async_press_wall_button(hass, UP, on=False)
    await hass.async_block_till_done()
    assert cover_state(hass) == (CoverState.OPEN, 100)
    # Another press (after the dead time from the reported off), then stop and open from
    # HA do send
    await tick(hass, freezer, 1)
    async_press_wall_button(hass, DOWN, on=True)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.CLOSING
    await tick(hass, freezer, 2)
    calls.clear()
    await call(hass, SERVICE_STOP_COVER)
    assert switch_calls(calls) == [(DOWN, SERVICE_TURN_OFF)]
    # The fake device confirms the off 3 s later; up waits the dead time from there
    await tick(hass, freezer, 3)
    await call(hass, SERVICE_OPEN_COVER)
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, DEAD_TIME)
    assert switch_calls(calls) == [(DOWN, SERVICE_TURN_OFF), (UP, SERVICE_TURN_ON)]
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, 3)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.usefixtures("cover")
async def test_cancelled_order_then_press_within_the_window(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """As above, but the wall on comes at 4.5 s, within 5 s of our off.

    It is taken as our on shown late: our off of 1 s goes out again at 6 s, and the
    fake device shows it at 9 s. The position follows what HA shows: 4.5 s of 17 s.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 1)
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, CONFIRM_TIMEOUT - 1.5)
    async_press_wall_button(hass, UP, on=True)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 1.5)
    assert switch_calls(calls) == [
        (UP, SERVICE_TURN_ON),
        (UP, SERVICE_TURN_OFF),
        (UP, SERVICE_TURN_OFF),
    ]
    await tick(hass, freezer, 3)
    assert cover_state(hass) == (CoverState.OPEN, 26)
    await tick(hass, freezer, 30)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert len(switch_calls(calls)) == 3


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.usefixtures("cover")
async def test_repeated_order_then_press(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """To 50, stop and to 50 again within the slow confirmation; a later press runs to 100.

    The second on (1 s) is shown at 4 s: the timer counts from there (8.5 s) and
    its off is shown 3 s later, so the cover stops at 68, not 50. The wall's up is
    left on at 100.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, position=50)
    await tick(hass, freezer, 0.5)
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, 0.5)
    await call(hass, SERVICE_SET_COVER_POSITION, position=50)
    await tick(hass, freezer, 30)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 68)
    async_press_wall_button(hass, UP, on=True)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 20)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.usefixtures("cover")
async def test_press_with_the_value_of_a_pending_order_is_harmless(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A wall on before our on is confirmed is taken for its echo.

    The position and the timer start with it at 1 s: off at 9.5 s, shown at
    12.5 s, so the cover stops at 68.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, position=50)
    await tick(hass, freezer, 1)
    async_press_wall_button(hass, UP, on=True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 8.25)
    assert relay_states(hass)[0] == STATE_ON
    await tick(hass, freezer, 0.25)
    await tick(hass, freezer, 3)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 68)
    # A later press is followed
    async_press_wall_button(hass, UP, on=True)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.OPENING
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, 3)


@pytest.mark.parametrize("confirm_delay", [-1])
@pytest.mark.usefixtures("cover")
async def test_report_glitch_is_two_presses(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An off-on glitch of the report during our movement to 25: two presses.

    The target is given up: no off is sent and the cover runs to 100 with up left on.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_SET_COVER_POSITION, position=25)
    hass.states.async_set(UP, STATE_ON)
    await hass.async_block_till_done()
    await tick(hass, freezer, 1)
    hass.states.async_set(UP, STATE_OFF)
    hass.states.async_set(UP, STATE_ON)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 20)
    assert switch_calls(calls) == [(UP, SERVICE_TURN_ON)]
    assert cover_state(hass) == (CoverState.OPENING, 100)


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.usefixtures("cover")
async def test_same_order_twice_sends_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Opening twice in a row sends a single turn_on."""
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 1)
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 1)
    assert switch_calls(calls) == [(UP, SERVICE_TURN_ON)]
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, 7)


# Lifecycle


async def _moving(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 2)


async def _dead_time(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 2)
    await call(hass, SERVICE_CLOSE_COVER)
    await tick(hass, freezer, 0.25)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


async def _give_up_wait(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 2)
    # The up relay keeps reporting on after our off
    await call(hass, SERVICE_CLOSE_COVER)
    hass.states.async_set(UP, STATE_ON)
    await tick(hass, freezer, 1)


@pytest.mark.parametrize(
    ("confirm_delay", "prepare"),
    [
        pytest.param(0, _moving, id="moving"),
        pytest.param(0, _dead_time, id="dead_time"),
        pytest.param(-1, _give_up_wait, id="waiting_for_off_report"),
    ],
)
async def test_unload_leaves_no_timers_or_listeners(
    hass: HomeAssistant,
    cover_timers: CoverTimers,
    freezer: FrozenDateTimeFactory,
    cover: MockConfigEntry,
    prepare: Callable[[HomeAssistant, FrozenDateTimeFactory], Awaitable[None]],
) -> None:
    """Each state has live timers; after unloading none is left and no relay is switched."""
    await prepare(hass, freezer)
    assert cover_timers.live
    assert await hass.config_entries.async_unload(cover.entry_id)
    await hass.async_block_till_done()
    assert cover_timers.live == 0
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    async_press_wall_button(hass, DOWN, on=True)
    hass.states.async_set(UP, STATE_OFF)
    await hass.async_block_till_done()
    await tick(hass, freezer, 60)
    assert switch_calls(calls) == []
    assert hass.states.get(COVER).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("command_delay", [2])
async def test_unload_during_a_slow_stop_from_a_timer(
    hass: HomeAssistant,
    cover_timers: CoverTimers,
    freezer: FrozenDateTimeFactory,
    cover: MockConfigEntry,
) -> None:
    """The arrival timer sends a slow off; unloading in the middle leaves nothing behind.

    turn_on takes 2 s and HA shows up on when it returns: the 1.7 s timer (10 % of
    17 s) counts from there and sends turn_off at 3.7 s, which lasts until 5.7 s.
    """
    task = start_call(hass, SERVICE_SET_COVER_POSITION, position=10)
    await steps(hass, freezer, 16)
    assert task.done()
    assert task.exception() is None
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert await hass.config_entries.async_unload(cover.entry_id)
    await steps(hass, freezer, 12)
    await hass.async_block_till_done()
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_timers.live == 0


@pytest.mark.parametrize("interlock", [False])
@pytest.mark.parametrize("command_delay", [2])
async def test_unload_during_a_slow_reversal_sends_nothing_more(
    hass: HomeAssistant,
    cover_timers: CoverTimers,
    freezer: FrozenDateTimeFactory,
    cover: MockConfigEntry,
) -> None:
    """Without interlock, unloading during the off of a reversal cuts the loop.

    Up is shown on when its 2 s call returns; the reversal at 5 s finds the cover
    at 17.6 %.
    """
    opening = start_call(hass, SERVICE_SET_COVER_POSITION, position=80)
    await steps(hass, freezer, 20)
    assert opening.done()
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    reverse = start_call(hass, SERVICE_SET_COVER_POSITION, position=10)
    await steps(hass, freezer, 4)
    assert not reverse.done()
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    assert await hass.config_entries.async_unload(cover.entry_id)
    await steps(hass, freezer, 40)
    await hass.async_block_till_done()
    assert reverse.done()
    assert reverse.exception() is None
    assert switch_calls(calls) == []
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_timers.live == 0


# Position with different times and reversals


@pytest.mark.usefixtures("cover")
async def test_reversal_with_interlock(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """0 -> 80; at 5 s (29.4 %) ask 10: dead time, then 19.4 % of 41 s = 7.96 s down."""
    await call(hass, SERVICE_SET_COVER_POSITION, position=80)
    await tick(hass, freezer, 5)
    assert cover_state(hass) == (CoverState.OPENING, 29)
    await call(hass, SERVICE_SET_COVER_POSITION, position=10)
    await tick(hass, freezer, DEAD_TIME - STEP)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.CLOSING, 29)
    await tick(hass, freezer, STEP)
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    await tick(hass, freezer, 7.75)
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    await tick(hass, freezer, 0.25)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 10)


@pytest.mark.parametrize("interlock", [False])
@pytest.mark.usefixtures("cover")
async def test_reversal_without_interlock(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Down starts at 5 s and stops 7.96 s later; open then runs 90 % of 17 s."""
    await call(hass, SERVICE_SET_COVER_POSITION, position=80)
    await tick(hass, freezer, 5)
    await call(hass, SERVICE_SET_COVER_POSITION, position=10)
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    await tick(hass, freezer, 7.75)
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    await tick(hass, freezer, 0.25)
    assert cover_state(hass) == (CoverState.OPEN, 10)
    await call(hass, SERVICE_OPEN_COVER)
    # 15.3 s, no margin
    await tick(hass, freezer, 15.25)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, 0.25)
    assert cover_state(hass) == (CoverState.OPEN, 100)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.parametrize("interlock", [False])
@pytest.mark.usefixtures("cover")
async def test_wall_relay_left_on_without_interlock(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Up on from the wall; at 5 s (29.4 %) ask 10: only down goes on, up is left.

    With both on, down (switched on last) is followed: 19.4 % of 41 s. At 10 only
    our down goes off; the wall's up is left on and the position follows it to 100.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    async_press_wall_button(hass, UP, on=True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 5)
    assert cover_state(hass) == (CoverState.OPENING, 29)
    await call(hass, SERVICE_SET_COVER_POSITION, position=10)
    assert switch_calls(calls) == [(DOWN, SERVICE_TURN_ON)]
    assert relay_states(hass) == (STATE_ON, STATE_ON)
    assert cover_state(hass) == (CoverState.CLOSING, 29)
    await tick(hass, freezer, 7.75)
    assert relay_states(hass) == (STATE_ON, STATE_ON)
    await tick(hass, freezer, 0.25)
    assert cover_state(hass) == (CoverState.OPENING, 10)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert switch_calls(calls) == [(DOWN, SERVICE_TURN_ON), (DOWN, SERVICE_TURN_OFF)]
    await tick(hass, freezer, 30)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)


@pytest.mark.parametrize("confirm_delay", [-1])
@pytest.mark.usefixtures("cover")
async def test_second_reversal_after_give_up_waits_again(
    hass: HomeAssistant,
    cover_timers: CoverTimers,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Up keeps reporting on after our off; the reversal is given up after 2 s.

    A second close waits again with a live timer and is given up again: never closing
    without a timer. Meanwhile the cover shows what HA shows: opening.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_OPEN_COVER)
    hass.states.async_set(UP, STATE_ON)
    await tick(hass, freezer, 2)
    await call(hass, SERVICE_CLOSE_COVER)
    await tick(hass, freezer, 11)
    assert caplog.text.count("not reversing") == 1
    assert cover_state(hass)[0] == CoverState.OPENING
    await call(hass, SERVICE_CLOSE_COVER)
    assert cover_timers.live
    await tick(hass, freezer, 1.75)
    assert caplog.text.count("not reversing") == 1
    assert cover_timers.live
    await tick(hass, freezer, 0.25)
    assert caplog.text.count("not reversing") == 2
    await tick(hass, freezer, 120)
    assert (DOWN, SERVICE_TURN_ON) not in switch_calls(calls)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)


@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.parametrize("confirm_delay", [-1])
@pytest.mark.usefixtures("cover")
async def test_interlock_with_an_on_reported_late(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Open, and close before up reports. Up then reports on and off (its queue).

    While HA shows up on, down waits; the dead time counts from the reported off.
    From 50: from 0, with up never shown on, close would be an order to the end it is at.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 0.25)
    await call(hass, SERVICE_CLOSE_COVER)
    hass.states.async_set(UP, STATE_ON)
    await hass.async_block_till_done()
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert (DOWN, SERVICE_TURN_ON) not in switch_calls(calls)
    await tick(hass, freezer, 1)
    assert (DOWN, SERVICE_TURN_ON) not in switch_calls(calls)
    hass.states.async_set(UP, STATE_OFF)
    await hass.async_block_till_done()
    await tick(hass, freezer, DEAD_TIME - STEP)
    assert (DOWN, SERVICE_TURN_ON) not in switch_calls(calls)
    await tick(hass, freezer, STEP)
    assert switch_calls(calls)[-1] == (DOWN, SERVICE_TURN_ON)
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, 1)


@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.parametrize("confirm_delay", [-1])
@pytest.mark.usefixtures("cover")
async def test_interlock_with_unconfirmed_commands(
    hass: HomeAssistant, cover_timers: CoverTimers, freezer: FrozenDateTimeFactory
) -> None:
    """HA shows none of our commands yet: down comes on 0.5 s after our off of up.

    Down does not wait for the unconfirmed on of up to expire. From 50, as HA never
    shows up on.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 0.25)
    await call(hass, SERVICE_CLOSE_COVER)
    assert cover_state(hass)[0] == CoverState.CLOSING
    assert cover_timers.live
    await tick(hass, freezer, DEAD_TIME - STEP)
    assert switch_calls(calls) == [(UP, SERVICE_TURN_ON), (UP, SERVICE_TURN_OFF)]
    await tick(hass, freezer, STEP)
    assert switch_calls(calls)[-1] == (DOWN, SERVICE_TURN_ON)
    await call(hass, SERVICE_STOP_COVER)


# Same family, other cases


@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.parametrize("command_delay", [2])
async def test_press_of_the_other_relay_during_a_slow_call(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cover: MockConfigEntry
) -> None:
    """Wall down at 1 s while the turn_on of up (2 s) is running, from 50.

    When the call returns only up goes off; down, pressed from outside, is left
    on. Both are physically on from 2 to 4 s: the press came with our order already on
    its way.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    opening = start_call(hass, SERVICE_OPEN_COVER)
    await steps(hass, freezer, 4)
    async_press_wall_button(hass, DOWN, on=True)
    await steps(hass, freezer, 30)
    assert opening.done()
    assert opening.exception() is None
    assert switch_calls(calls) == [(UP, SERVICE_TURN_ON), (UP, SERVICE_TURN_OFF)]
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    assert cover_state(hass)[0] == CoverState.CLOSING
    stopping = start_call(hass, SERVICE_STOP_COVER)
    await steps(hass, freezer, 12)
    await hass.async_block_till_done()
    assert stopping.done()
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.parametrize("command_delay", [0.5])
async def test_two_quick_orders_with_a_slow_call(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cover: MockConfigEntry
) -> None:
    """Open, and 0.25 s later close: up on, up off, down on after the dead time.

    From 50: up is not shown on yet at 0.25 s.
    """
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    tasks = [start_call(hass, SERVICE_OPEN_COVER)]
    await steps(hass, freezer, 1)
    tasks.append(start_call(hass, SERVICE_CLOSE_COVER))
    await steps(hass, freezer, 3)
    assert switch_calls(calls) == [(UP, SERVICE_TURN_ON), (UP, SERVICE_TURN_OFF)]
    await steps(hass, freezer, round(DEAD_TIME / STEP) + 4)
    await hass.async_block_till_done()
    assert all(t.done() and t.exception() is None for t in tasks)
    assert switch_calls(calls) == [
        (UP, SERVICE_TURN_ON),
        (UP, SERVICE_TURN_OFF),
        (DOWN, SERVICE_TURN_ON),
    ]
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    assert cover_state(hass)[0] == CoverState.CLOSING
    stopping = start_call(hass, SERVICE_STOP_COVER)
    await steps(hass, freezer, 12)
    await hass.async_block_till_done()
    assert stopping.done()
