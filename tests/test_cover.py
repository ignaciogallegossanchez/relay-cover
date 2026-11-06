"""The cover entity with the fake relay inside Home Assistant.

Times: opening 20 s, closing 30 s, dead time 0.5 s, no margin at the ends unless a test
sets one.
"""

import asyncio
from typing import Any
from unittest.mock import patch

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverState,
)
from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_STATE_CHANGED,
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
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er, restore_state as rs
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    mock_restore_cache_with_extra_data,
)

from custom_components.fake_relay import async_press_wall_button
from custom_components.fake_relay.switch import FakeRelay
from custom_components.relay_cover.motion import CONFIRM_TIMEOUT, DEAD_TIME

from .common import (
    CLOSE_RELAY,
    COVER,
    OPEN_RELAY,
    STEP,
    CoverTimers,
    call,
    cover_state,
    failing_relay_calls,
    relay_states,
    restore_saved_position,
    start_call,
    state_changes,
    steps,
    switch_calls,
    tick,
)


@pytest.mark.usefixtures("cover")
async def test_open_full(hass: HomeAssistant, freezer) -> None:
    """Position each second; relay off exactly at 100 (no margin); one final change."""
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await call(hass, SERVICE_OPEN_COVER)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, 10)
    assert cover_state(hass) == (CoverState.OPENING, 50)
    await tick(hass, freezer, 10 - STEP)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, STEP)
    assert cover_state(hass) == (CoverState.OPEN, 100)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    changes = state_changes(events, COVER)
    assert changes[0] == (CoverState.OPENING, 0)
    assert changes[-1] == (CoverState.OPEN, 100)
    assert {s for s, _ in changes[:-1]} == {CoverState.OPENING}
    # One write per second of travel (0 to 20 s) and the final open, not one per clock step
    assert len(changes) == 22


@pytest.mark.usefixtures("cover")
async def test_set_position(hass: HomeAssistant, freezer) -> None:
    """30 % of 20 s: relay off at 6 s, position 30."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    await tick(hass, freezer, 5.75)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, STEP)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 30)


@pytest.mark.parametrize("saved_position", [100])
@pytest.mark.usefixtures("cover")
async def test_open_when_open_sends_nothing(hass: HomeAssistant, freezer) -> None:
    """Without margin there is nothing to run at the end: no relay pulse."""
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 3)
    assert state_changes(events, OPEN_RELAY) == []
    assert cover_state(hass) == (CoverState.OPEN, 100)


@pytest.mark.parametrize("saved_position", [40])
@pytest.mark.usefixtures("cover")
async def test_set_position_equal_and_stop(hass: HomeAssistant, freezer) -> None:
    """Same position does nothing; stop freezes."""
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 40})
    await tick(hass, freezer, 3)
    assert state_changes(events, OPEN_RELAY) == []
    assert state_changes(events, CLOSE_RELAY) == []
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 7)
    await call(hass, SERVICE_STOP_COVER)
    assert cover_state(hass) == (CoverState.OPEN, 75)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.parametrize("saved_position", [None])
@pytest.mark.usefixtures("cover")
async def test_unknown_position(hass: HomeAssistant, freezer) -> None:
    """Fresh install: unknown; middle refused; a full travel makes it known."""
    assert cover_state(hass) == ("unknown", None)
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 50})
    await call(hass, SERVICE_OPEN_COVER)
    assert cover_state(hass) == (CoverState.OPENING, None)
    await tick(hass, freezer, 20)
    assert cover_state(hass) == (CoverState.OPEN, 100)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


async def _reverse(hass: HomeAssistant, freezer) -> list:
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 80})
    await tick(hass, freezer, 6)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 10})
    return events


@pytest.mark.usefixtures("cover")
async def test_reverse_with_interlock(hass: HomeAssistant, freezer) -> None:
    """Up off, dead time with both off showing closing, down on, stop at 10."""
    events = await _reverse(hass, freezer)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.CLOSING, 30)
    await tick(hass, freezer, DEAD_TIME / 2)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    await tick(hass, freezer, DEAD_TIME / 2)
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    await tick(hass, freezer, 6)
    assert cover_state(hass) == (CoverState.OPEN, 10)
    assert state_changes(events, OPEN_RELAY) == [(STATE_OFF, None)]
    assert state_changes(events, CLOSE_RELAY) == [(STATE_ON, None), (STATE_OFF, None)]
    # No CoverState.OPEN between opening and closing: the interface does not blink
    states = [s for s, _ in state_changes(events, COVER)]
    assert states[:-1] == [CoverState.CLOSING] * (len(states) - 1)


@pytest.mark.parametrize("interlock", [False])
@pytest.mark.usefixtures("cover")
async def test_reverse_without_interlock(hass: HomeAssistant, freezer) -> None:
    """Up off then down on in the same call, no wait."""
    events = await _reverse(hass, freezer)
    assert relay_states(hass) == (STATE_OFF, STATE_ON)
    relay_order = [
        (e.data["entity_id"], e.data["new_state"].state)
        for e in events
        if e.data["entity_id"] in (OPEN_RELAY, CLOSE_RELAY)
    ]
    assert relay_order[:2] == [(OPEN_RELAY, STATE_OFF), (CLOSE_RELAY, STATE_ON)]
    await tick(hass, freezer, 6)
    assert cover_state(hass) == (CoverState.OPEN, 10)


@pytest.mark.usefixtures("cover")
async def test_command_during_dead_time(hass: HomeAssistant, freezer) -> None:
    """Stop during the dead time: the down relay never comes on."""
    events = await _reverse(hass, freezer)
    await tick(hass, freezer, DEAD_TIME / 2)
    await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, 5)
    assert state_changes(events, CLOSE_RELAY) == []
    assert cover_state(hass) == (CoverState.OPEN, 30)


@pytest.mark.parametrize(
    ("button", "state", "position", "relays_after"),
    [
        pytest.param(
            OPEN_RELAY, CoverState.OPENING, 100, (STATE_ON, STATE_OFF), id="on_off"
        ),
        pytest.param(
            CLOSE_RELAY, CoverState.CLOSING, 0, (STATE_OFF, STATE_ON), id="off_on"
        ),
    ],
)
@pytest.mark.parametrize("interlock", [True, False])
@pytest.mark.parametrize("end_margin", [20])
@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.usefixtures("cover")
async def test_wall_button_start(
    hass: HomeAssistant,
    freezer,
    button: str,
    state: str,
    position: int,
    relays_after: tuple[str, str],
) -> None:
    """A wall button is followed to the end and left on, with or without interlock.

    The cover stays at the end showing the direction.
    """
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    async_press_wall_button(hass, button, on=True)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == state
    await tick(hass, freezer, 60)
    assert switch_calls(events) == []
    assert relay_states(hass) == relays_after
    assert cover_state(hass) == (state, position)


@pytest.mark.usefixtures("cover")
async def test_wall_button_stop(hass: HomeAssistant, freezer) -> None:
    """Off/off from the wall while moving: stop where it is."""
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 4)
    async_press_wall_button(hass, OPEN_RELAY, on=False)
    await hass.async_block_till_done()
    await tick(hass, freezer, 30)
    assert cover_state(hass) == (CoverState.OPEN, 20)


@pytest.mark.parametrize("saved_position", [100])
@pytest.mark.usefixtures("cover")
async def test_wall_both_on(hass: HomeAssistant, freezer) -> None:
    """Up pressed while going down: only our down goes off; up stays on, no pause.

    Up is left on also at the end.
    """
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_CLOSE_COVER)
    await tick(hass, freezer, 3)
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 60)
    assert switch_calls(events) == [
        (CLOSE_RELAY, SERVICE_TURN_ON),
        (CLOSE_RELAY, SERVICE_TURN_OFF),
    ]
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPENING, 100)


@pytest.mark.parametrize("interlock", [False])
@pytest.mark.parametrize("saved_position", [100])
@pytest.mark.usefixtures("cover")
async def test_wall_both_on_without_interlock(hass: HomeAssistant, freezer) -> None:
    """Up pressed while going down, no interlock: both stay on; then stop.

    Up, switched on last, is followed. The stop from Home Assistant switches off both.
    """
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_CLOSE_COVER)
    await tick(hass, freezer, 3)
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 30)
    assert switch_calls(events) == [(CLOSE_RELAY, SERVICE_TURN_ON)]
    assert relay_states(hass) == (STATE_ON, STATE_ON)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    await call(hass, SERVICE_STOP_COVER)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 100)


@pytest.mark.parametrize(
    ("confirm_delay", "press", "expected"),
    [
        pytest.param(0, None, [SERVICE_TURN_ON, SERVICE_TURN_OFF], id="own_command"),
        pytest.param(0, (2, False), [SERVICE_TURN_ON], id="wall_2s_after_our_order"),
        pytest.param(
            3, None, [SERVICE_TURN_ON, SERVICE_TURN_OFF], id="device_confirms_at_3s"
        ),
        pytest.param(
            CONFIRM_TIMEOUT - 1,
            None,
            [SERVICE_TURN_ON, SERVICE_TURN_OFF],
            id="device_confirms_1s_before_the_timeout",
        ),
        pytest.param(
            3,
            (1, True),
            [SERVICE_TURN_ON, SERVICE_TURN_OFF],
            id="wall_on_before_late_confirmation",
        ),
    ],
)
@pytest.mark.usefixtures("cover")
async def test_echo_or_external(
    hass: HomeAssistant,
    freezer,
    press: tuple[float, bool] | None,
    expected: list[str],
) -> None:
    """Echo or external follows value and pending command, not the context window.

    An echo keeps the order, which switches the relay off at its end; a change from
    outside gives it up. Told apart by what is switched.
    """
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    await call(hass, SERVICE_OPEN_COVER)
    if press is not None:
        await tick(hass, freezer, press[0])
        async_press_wall_button(hass, OPEN_RELAY, on=press[1])
        await hass.async_block_till_done()
    # Let the cover arrive and the fake device finish its pending confirmation
    await tick(hass, freezer, 40)
    assert switch_calls(events) == [(OPEN_RELAY, service) for service in expected]


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.usefixtures("cover")
async def test_late_confirmation_starts_the_position(
    hass: HomeAssistant, freezer
) -> None:
    """Confirmation at 3 s: the position starts with it, and so does the timer.

    The off goes out 10 s after the echo and is shown 3 s later: the position follows
    what Home Assistant shows, 13 s of opening.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 50})
    assert cover_state(hass) == (CoverState.OPENING, 0)
    await tick(hass, freezer, 3)
    assert cover_state(hass) == (CoverState.OPENING, 0)
    await tick(hass, freezer, 10)
    assert cover_state(hass) == (CoverState.OPENING, 50)
    await tick(hass, freezer, 3)
    assert cover_state(hass) == (CoverState.OPEN, 65)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.usefixtures("cover")
async def test_lost_off_is_sent_again(
    hass: HomeAssistant, cover_timers: CoverTimers, freezer
) -> None:
    """Our off is lost and HA keeps showing on: after 5 s it goes out again.

    The cover shows opening while HA shows on: from 20 (at 4 s) 5 s more, 45.
    """
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 4)
    with failing_relay_calls(SERVICE_TURN_OFF, None) as failed:
        await call(hass, SERVICE_STOP_COVER)
    assert failed == [OPEN_RELAY]
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPENING, 20)
    assert cover_timers.live
    await tick(hass, freezer, CONFIRM_TIMEOUT)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 45)


@pytest.mark.usefixtures("cover")
async def test_stop_again_does_not_repeat_a_lost_off(
    hass: HomeAssistant, freezer
) -> None:
    """Our off is lost; a second stop does not send it again: 5 s after the first."""
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 4)
    with failing_relay_calls(SERVICE_TURN_OFF, None):
        await call(hass, SERVICE_STOP_COVER)
    await tick(hass, freezer, 2)
    await call(hass, SERVICE_STOP_COVER)
    assert switch_calls(events) == [(OPEN_RELAY, SERVICE_TURN_OFF)]
    await tick(hass, freezer, CONFIRM_TIMEOUT - 2)
    assert switch_calls(events) == [(OPEN_RELAY, SERVICE_TURN_OFF)] * 2
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    await tick(hass, freezer, 30)
    assert len(switch_calls(events)) == 2


@pytest.mark.usefixtures("cover")
async def test_lost_on_is_sent_again(hass: HomeAssistant, freezer) -> None:
    """Our on is lost: after 5 s it goes out again. The position waits for it."""
    with failing_relay_calls(SERVICE_TURN_ON, None):
        await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 50})
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    await tick(hass, freezer, CONFIRM_TIMEOUT - 1)
    assert cover_state(hass) == (CoverState.OPENING, 0)
    await tick(hass, freezer, 1)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPENING, 0)
    await tick(hass, freezer, 10)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 50)


@pytest.mark.usefixtures("cover")
async def test_failed_off_from_the_timer_is_retried(
    hass: HomeAssistant,
    cover_timers: CoverTimers,
    freezer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The off at the target raises: the relay is on, so the cover keeps opening.

    The off goes out again 5 s later; the motor ran from 30 to 55 meanwhile.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    with failing_relay_calls(SERVICE_TURN_OFF, HomeAssistantError("lost")) as failed:
        await tick(hass, freezer, 6)
        assert failed == [OPEN_RELAY]
        assert "could not switch a relay: lost" in caplog.text
        assert relay_states(hass) == (STATE_ON, STATE_OFF)
        assert cover_state(hass) == (CoverState.OPENING, 30)
        assert cover_timers.live
        await tick(hass, freezer, CONFIRM_TIMEOUT)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 55)


@pytest.mark.usefixtures("cover")
async def test_failed_off_is_not_repeated_on_request(
    hass: HomeAssistant, freezer
) -> None:
    """After a failed off a stop of the user does not try again: 5 s after it."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    with failing_relay_calls(SERVICE_TURN_OFF, HomeAssistantError("lost")):
        await tick(hass, freezer, 6)
    await tick(hass, freezer, 1)
    await call(hass, SERVICE_STOP_COVER)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, CONFIRM_TIMEOUT - 1)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 55)


@pytest.mark.parametrize("interlock", [False])
@pytest.mark.usefixtures("cover")
async def test_failed_off_does_not_hold_back_the_other_relay(
    hass: HomeAssistant, freezer
) -> None:
    """Both on from the wall without interlock, then stop from HA; the off of up raises.

    Down still goes off and the stop raises the error. The off of up goes again
    5 s later, not when HA shows down off.
    """
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    async_press_wall_button(hass, CLOSE_RELAY, on=True)
    await hass.async_block_till_done()
    assert switch_calls(events) == []
    with (
        failing_relay_calls(SERVICE_TURN_OFF, HomeAssistantError("lost")) as failed,
        pytest.raises(HomeAssistantError),
    ):
        await call(hass, SERVICE_STOP_COVER)
    await hass.async_block_till_done()
    assert failed == [OPEN_RELAY]
    assert switch_calls(events) == [
        (OPEN_RELAY, SERVICE_TURN_OFF),
        (CLOSE_RELAY, SERVICE_TURN_OFF),
    ]
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, CONFIRM_TIMEOUT)
    assert switch_calls(events)[-1] == (OPEN_RELAY, SERVICE_TURN_OFF)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.usefixtures("cover")
async def test_any_error_of_a_relay_call_is_handled(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    """An error that is not a HomeAssistantError is raised too; the off goes again.

    HA still shows the relay on: the cover opens on until then.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 50})
    await tick(hass, freezer, 2)
    with (
        failing_relay_calls(SERVICE_TURN_OFF, TimeoutError()),
        pytest.raises(TimeoutError),
    ):
        await call(hass, SERVICE_STOP_COVER)
    assert cover_state(hass) == (CoverState.OPENING, 10)
    await tick(hass, freezer, CONFIRM_TIMEOUT)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 35)


@pytest.mark.usefixtures("cover")
async def test_any_error_from_the_timer_is_logged(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    """From a timer an unexpected error is logged with its traceback, then retried."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    with failing_relay_calls(SERVICE_TURN_OFF, TimeoutError()):
        await tick(hass, freezer, 6)
    assert "unexpected error switching a relay" in caplog.text
    assert "TimeoutError" in caplog.text
    await tick(hass, freezer, CONFIRM_TIMEOUT)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.usefixtures("cover")
async def test_wall_off_before_confirmation_is_invisible(
    hass: HomeAssistant, freezer
) -> None:
    """Wall off at 1 s while HA still shows off: no event reaches the cover.

    The position starts with the confirmation at 3 s, and the off at 13 s is
    shown at 16 s.
    """
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 50})
    await tick(hass, freezer, 1)
    async_press_wall_button(hass, OPEN_RELAY, on=False)
    await hass.async_block_till_done()
    assert state_changes(events, OPEN_RELAY) == []
    await tick(hass, freezer, 2)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPENING, 0)
    await tick(hass, freezer, 13)
    assert cover_state(hass) == (CoverState.OPEN, 65)


@pytest.mark.usefixtures("cover")
async def test_relay_unavailable_while_moving(hass: HomeAssistant, freezer) -> None:
    """Unavailable relay: position kept, stop sent on return."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    await tick(hass, freezer, 2)
    hass.states.async_set(OPEN_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    # The close relay still answers
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 6)
    hass.states.async_set(OPEN_RELAY, STATE_ON)
    await hass.async_block_till_done()
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 40)


@pytest.mark.usefixtures("cover")
async def test_timer_stops_the_relay_that_answers(hass: HomeAssistant, freezer) -> None:
    """The close relay goes unavailable: up still goes off at 30."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    await tick(hass, freezer, 2)
    hass.states.async_set(CLOSE_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 4)
    assert relay_states(hass) == (STATE_OFF, STATE_UNAVAILABLE)
    hass.states.async_set(CLOSE_RELAY, STATE_OFF)
    await hass.async_block_till_done()
    assert cover_state(hass) == (CoverState.OPEN, 30)


@pytest.mark.usefixtures("cover")
async def test_position_saved_while_unavailable(hass: HomeAssistant, freezer) -> None:
    """The state has no position while unavailable; the saved data keeps it."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    await tick(hass, freezer, 6)
    hass.states.async_set(OPEN_RELAY, STATE_UNAVAILABLE)
    hass.states.async_set(CLOSE_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert ATTR_CURRENT_POSITION not in hass.states.get(COVER).attributes
    stored = {
        s.state.entity_id: s for s in rs.async_get(hass).async_get_stored_states()
    }
    assert stored[COVER].extra_data is not None
    assert stored[COVER].extra_data.as_dict() == {"position": 30.0}


@pytest.mark.parametrize(
    "extra",
    [
        {"position": 30.0},
        {"position": 30},
        {"position": None},
        {"other": 1},
        {"position": 150},
        {"position": -1},
    ],
)
async def test_restart_after_unavailable_keeps_position(
    hass: HomeAssistant,
    relays: None,
    mock_config_entry: MockConfigEntry,
    extra: dict[str, object],
) -> None:
    """Restart with the last state unavailable: the position comes from the saved data."""
    mock_restore_cache_with_extra_data(hass, [(State(COVER, STATE_UNAVAILABLE), extra)])
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    expected = 30 if extra.get("position") in (30, 30.0) else None
    assert hass.states.get(COVER).attributes.get(ATTR_CURRENT_POSITION) == expected


async def test_restart_with_relay_on(
    hass: HomeAssistant, freezer, relays: None, mock_config_entry: MockConfigEntry
) -> None:
    """Restart while the up relay stays on: follow from 40 to 100 in 12 s.

    Nobody knows who switched it on: it is left on at 100.
    """
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    restore_saved_position(hass, 40)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert cover_state(hass) == (CoverState.OPENING, 40)
    assert hass.states.get(COVER).attributes["assumed_state"] is True
    await tick(hass, freezer, 11)
    assert cover_state(hass) == (CoverState.OPENING, 95)
    await tick(hass, freezer, 30)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)


async def test_unload_while_moving_leaves_relay(
    hass: HomeAssistant, cover_timers: CoverTimers, freezer, cover: MockConfigEntry
) -> None:
    """Unload mid-movement cancels timers and leaves the relay as it is."""
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 2)
    assert cover_timers.live
    assert await hass.config_entries.async_unload(cover.entry_id)
    await hass.async_block_till_done()
    assert cover_timers.live == 0
    await tick(hass, freezer, 30)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert hass.states.get(COVER).state == STATE_UNAVAILABLE


async def test_reload_while_moving_runs_to_the_end(
    hass: HomeAssistant, freezer, cover: MockConfigEntry
) -> None:
    """A reload on the way to 50 loses the target: up runs to 100.

    After the reload the relay on is not known as ours: it is left on at 100.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 50})
    await tick(hass, freezer, 4)
    assert await hass.config_entries.async_reload(cover.entry_id)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 30)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)


@pytest.mark.parametrize("source", ["wall", "state_write"])
@pytest.mark.usefixtures("cover")
async def test_idle_change_from_outside(
    hass: HomeAssistant, freezer, source: str
) -> None:
    """A change with a fresh context while idle is external and is followed.

    It is left on at the end.
    """
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    if source == "wall":
        async_press_wall_button(hass, OPEN_RELAY, on=True)
    else:
        hass.states.async_set(OPEN_RELAY, STATE_ON)
    await hass.async_block_till_done()
    assert cover_state(hass)[0] == CoverState.OPENING
    await tick(hass, freezer, 20)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    assert switch_calls(events) == []


@pytest.mark.parametrize("command_delay", [2])
async def test_unload_during_slow_relay_call(
    hass: HomeAssistant, cover_timers: CoverTimers, freezer, cover: MockConfigEntry
) -> None:
    """A relay call still running at unload does not leave timers behind."""
    task = start_call(hass, SERVICE_OPEN_COVER)
    await asyncio.sleep(0)
    assert not task.done()
    assert await hass.config_entries.async_unload(cover.entry_id)
    # Without async_block_till_done: it would wait for the sleeping relay call
    await steps(hass, freezer, round(3 / STEP))
    await hass.async_block_till_done()
    assert task.done()
    assert task.exception() is None
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_timers.live == 0


@pytest.mark.usefixtures("cover")
async def test_relay_commands_have_our_context(hass: HomeAssistant) -> None:
    """Relay changes made by the cover carry a child context of the cover action."""
    await call(hass, SERVICE_OPEN_COVER)
    relay = hass.states.get(OPEN_RELAY)
    assert relay.state == STATE_ON
    assert relay.context.parent_id == hass.states.get(COVER).context.id


async def test_entity_id_change_keeps_working(
    hass: HomeAssistant,
    freezer,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Renaming the cover entity_id keeps the relays followed and the position.

    The wall's up is left on at 100.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    await tick(hass, freezer, 6)
    entity_registry.async_update_entity(COVER, new_entity_id="cover.renamed")
    await hass.async_block_till_done()
    state = hass.states.get("cover.renamed")
    assert state is not None
    assert state.attributes[ATTR_CURRENT_POSITION] == 30
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    assert hass.states.get("cover.renamed").state == CoverState.OPENING
    await tick(hass, freezer, 14)
    state = hass.states.get("cover.renamed")
    assert (state.state, state.attributes[ATTR_CURRENT_POSITION]) == (
        CoverState.OPENING,
        100,
    )
    assert relay_states(hass) == (STATE_ON, STATE_OFF)


def _position_of(hass: HomeAssistant, entity_id: str) -> tuple[str, int | None]:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state, state.attributes.get(ATTR_CURRENT_POSITION)


async def test_rename_while_moving(
    hass: HomeAssistant,
    freezer,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Renamed while opening to 60: it keeps the timing and stops there."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 60})
    await tick(hass, freezer, 6)
    entity_registry.async_update_entity(COVER, new_entity_id="cover.renamed")
    await hass.async_block_till_done()
    await tick(hass, freezer, 1)
    assert _position_of(hass, "cover.renamed") == (CoverState.OPENING, 35)
    await tick(hass, freezer, 5)
    assert _position_of(hass, "cover.renamed") == (CoverState.OPEN, 60)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.parametrize("confirm_delay", [3])
@pytest.mark.parametrize(
    ("order", "wait", "expected"),
    [
        pytest.param(
            SERVICE_STOP_COVER, 4, (CoverState.OPEN, 20), id="unconfirmed_off"
        ),
        pytest.param(
            SERVICE_OPEN_COVER, 0, (CoverState.OPEN, 100), id="unconfirmed_on"
        ),
    ],
)
async def test_rename_with_an_unconfirmed_order(
    hass: HomeAssistant,
    freezer,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    order: str,
    wait: float,
    expected: tuple[str, int],
) -> None:
    """Renamed before the relay confirms our order: the state passed again is no report."""
    if wait:
        await call(hass, SERVICE_OPEN_COVER)
        await tick(hass, freezer, wait)
    await call(hass, order)
    entity_registry.async_update_entity(COVER, new_entity_id="cover.renamed")
    await hass.async_block_till_done()
    await tick(hass, freezer, 30)
    assert _position_of(hass, "cover.renamed") == expected
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.parametrize("saved_position", [100])
@pytest.mark.usefixtures("cover")
async def test_wall_press_at_the_end_is_left_on(hass: HomeAssistant, freezer) -> None:
    """Believed open, up pressed on the wall: it stays on until it is released."""
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 60)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    async_press_wall_button(hass, OPEN_RELAY, on=False)
    await hass.async_block_till_done()
    assert cover_state(hass) == (CoverState.OPEN, 100)
    assert switch_calls(events) == []


@pytest.mark.usefixtures("cover")
async def test_lost_stop_then_reconnect_sends_the_off_again(
    hass: HomeAssistant, freezer
) -> None:
    """The off is lost, the relay drops out and comes back on: the off goes again.

    5 s after it was sent, not on the return; the position follows HA until then.
    """
    async_press_wall_button(hass, OPEN_RELAY, on=True)
    await hass.async_block_till_done()
    await tick(hass, freezer, 4)
    with failing_relay_calls(SERVICE_TURN_OFF, None):
        await call(hass, SERVICE_STOP_COVER)
    hass.states.async_set(OPEN_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await tick(hass, freezer, 1)
    hass.states.async_set(OPEN_RELAY, STATE_ON)
    await hass.async_block_till_done()
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, CONFIRM_TIMEOUT - 1)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 45)
    await tick(hass, freezer, 30)
    assert cover_state(hass) == (CoverState.OPEN, 45)


@pytest.mark.usefixtures("cover")
async def test_stop_while_the_other_relay_is_unavailable(
    hass: HomeAssistant, freezer
) -> None:
    """Opening, the close relay drops out; the cover can still be stopped."""
    await call(hass, SERVICE_OPEN_COVER)
    await tick(hass, freezer, 2)
    hass.states.async_set(CLOSE_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await call(hass, SERVICE_STOP_COVER)
    assert relay_states(hass) == (STATE_OFF, STATE_UNAVAILABLE)
    assert cover_state(hass) == (CoverState.OPEN, 10)


@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.parametrize("service", [SERVICE_OPEN_COVER, SERVICE_CLOSE_COVER])
@pytest.mark.usefixtures("cover")
async def test_move_needing_an_unavailable_relay_is_refused(
    hass: HomeAssistant, service: str
) -> None:
    """With interlock a move needs both relays; the error is translated."""
    hass.states.async_set(CLOSE_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, service)
    assert err.value.translation_key == "relay_unavailable"
    assert relay_states(hass) == (STATE_OFF, STATE_UNAVAILABLE)


@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.parametrize("interlock", [False])
@pytest.mark.usefixtures("cover")
async def test_move_away_from_an_unavailable_relay_without_interlock(
    hass: HomeAssistant,
) -> None:
    """Without interlock opening needs only the open relay; closing is refused."""
    hass.states.async_set(CLOSE_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_CLOSE_COVER)
    await call(hass, SERVICE_OPEN_COVER)
    assert relay_states(hass) == (STATE_ON, STATE_UNAVAILABLE)


@pytest.mark.usefixtures("cover")
async def test_failed_call_after_its_on_is_shown_keeps_the_move(
    hass: HomeAssistant, freezer
) -> None:
    """The relay switches on and then its call raises: the move to 30 goes on."""
    original = FakeRelay.async_turn_on

    async def on_then_raise(self: FakeRelay, **kwargs: Any) -> None:
        await original(self, **kwargs)
        raise HomeAssistantError("acknowledgement timed out")

    with (
        patch.object(FakeRelay, "async_turn_on", on_then_raise),
        pytest.raises(HomeAssistantError),
    ):
        await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    assert cover_state(hass) == (CoverState.OPENING, 0)
    await tick(hass, freezer, 6)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 30)


@pytest.mark.usefixtures("cover")
async def test_hanging_relay_call_is_given_up(hass: HomeAssistant, freezer) -> None:
    """A turn_on that never returns fails after 5 s; a stop waiting for it goes on."""
    never = asyncio.Event()

    async def hang(self: FakeRelay, **kwargs: Any) -> None:
        await never.wait()

    with patch.object(FakeRelay, "async_turn_on", hang):
        opening = start_call(hass, SERVICE_OPEN_COVER)
        await steps(hass, freezer, 4)
        stopping = start_call(hass, SERVICE_STOP_COVER)
        await steps(hass, freezer, round(CONFIRM_TIMEOUT / STEP))
        await hass.async_block_till_done()
    assert opening.done()
    assert stopping.done()
    error = opening.exception()
    assert isinstance(error, HomeAssistantError)
    assert error.translation_key == "relay_timeout"
    assert stopping.exception() is None
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.CLOSED, 0)


@pytest.mark.usefixtures("cover")
async def test_hanging_relay_call_does_not_hold_the_action(
    hass: HomeAssistant, freezer
) -> None:
    """A turn_on that hangs every time: the action fails after the first try.

    The on goes again from the timer when the call gives up, only logged. Only
    once: 10 s after the order it is given up, and a stop sends no more ons.
    """
    tries = 0
    never = asyncio.Event()

    async def hang(self: FakeRelay, **kwargs: Any) -> None:
        nonlocal tries
        tries += 1
        await never.wait()

    with patch.object(FakeRelay, "async_turn_on", hang):
        opening = start_call(hass, SERVICE_OPEN_COVER)
        await steps(hass, freezer, round(CONFIRM_TIMEOUT / STEP) + 1)
        assert opening.done()
        assert isinstance(opening.exception(), HomeAssistantError)
        # The timer fires at the next step after the call gives up
        await steps(hass, freezer, round(CONFIRM_TIMEOUT / STEP) + 2)
        assert tries == 2
        await steps(hass, freezer, round(3 * CONFIRM_TIMEOUT / STEP))
        assert tries == 2
        assert cover_state(hass) == (CoverState.CLOSED, 0)
        stopping = start_call(hass, SERVICE_STOP_COVER)
        await steps(hass, freezer, round(3 * CONFIRM_TIMEOUT / STEP))
        await hass.async_block_till_done()
    assert stopping.done()
    assert tries == 2
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)


@pytest.mark.usefixtures("cover")
async def test_an_off_that_always_fails_is_logged_once(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    """The off is retried every 5 s; only the first failure is an error."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    with failing_relay_calls(SERVICE_TURN_OFF, HomeAssistantError("lost"), count=4):
        await tick(hass, freezer, 6 + 3 * CONFIRM_TIMEOUT)
    failures = [r for r in caplog.records if "could not switch a relay" in r.message]
    assert [r.levelname for r in failures] == ["ERROR", "DEBUG", "DEBUG", "DEBUG"]
    assert "respond again" not in caplog.text
    await tick(hass, freezer, CONFIRM_TIMEOUT)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    # Once, at the first call that works
    back = [r for r in caplog.records if "respond again" in r.message]
    assert [r.levelname for r in back] == ["INFO"]
    # After a call that works, the next failure is an error again
    caplog.clear()
    await call(hass, SERVICE_CLOSE_COVER)
    with failing_relay_calls(SERVICE_TURN_OFF, HomeAssistantError("lost")) as failed:
        await tick(hass, freezer, 31)
    assert failed == [CLOSE_RELAY]
    assert [r.levelname for r in caplog.records if "could not" in r.message] == [
        "ERROR"
    ]


@pytest.mark.parametrize("end_margin", [20])
@pytest.mark.parametrize("saved_position", [100])
@pytest.mark.usefixtures("cover")
async def test_open_when_open_runs_the_margin(hass: HomeAssistant, freezer) -> None:
    """20 %: open at 100 keeps up on 4 s (20 % of 20 s), shown opening at 100."""
    await call(hass, SERVICE_OPEN_COVER)
    assert cover_state(hass) == (CoverState.OPENING, 100)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, 4 - STEP)
    assert relay_states(hass) == (STATE_ON, STATE_OFF)
    await tick(hass, freezer, STEP)
    assert relay_states(hass) == (STATE_OFF, STATE_OFF)
    assert cover_state(hass) == (CoverState.OPEN, 100)


def _saved_position(hass_storage: dict[str, Any]) -> float | None:
    """Return the position of the cover in the saved restore states."""
    (saved,) = (
        item
        for item in hass_storage[rs.STORAGE_KEY]["data"]
        if item["state"]["entity_id"] == COVER
    )
    return saved["extra_data"]["position"]


def _counting_saves() -> Any:
    """Count the saves of the restore states, which still take place."""
    dump = rs.RestoreStateData.async_dump_states
    return patch.object(
        rs.RestoreStateData, "async_dump_states", autospec=True, side_effect=dump
    )


@pytest.mark.usefixtures("cover")
async def test_position_saved_when_a_movement_ends(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """The restore states are saved once when each movement ends, not on the way.

    A power cut then restores where the cover stopped, not a position up to 15 min old.
    """
    with _counting_saves() as saves:
        await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
        await tick(hass, freezer, 6 - STEP)
        assert saves.call_count == 0
        await tick(hass, freezer, STEP)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saves.call_count == 1
        assert _saved_position(hass_storage) == 30
        await call(hass, SERVICE_OPEN_COVER)
        await tick(hass, freezer, 4)
        assert saves.call_count == 1
        await call(hass, SERVICE_STOP_COVER)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saves.call_count == 2
        assert _saved_position(hass_storage) == 50
        # Nothing moves: nothing more is saved
        await tick(hass, freezer, 10)
        await call(hass, SERVICE_STOP_COVER)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saves.call_count == 2


async def test_unload_right_after_a_stop_cancels_the_save(
    hass: HomeAssistant, cover: MockConfigEntry
) -> None:
    """A save still running when the entry unloads is cancelled, not left behind."""
    started = asyncio.Event()

    async def slow_dump(_self: rs.RestoreStateData) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(rs.RestoreStateData, "async_dump_states", slow_dump):
        await call(hass, SERVICE_OPEN_COVER)
        await call(hass, SERVICE_STOP_COVER)
        await started.wait()
        assert await hass.config_entries.async_unload(cover.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("saved_position", [100])
@pytest.mark.usefixtures("cover")
async def test_wall_relay_held_at_the_end_is_saved_once(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """A wall switch that holds the relay on: saved once on reaching 0.

    The movement never ends, so a power cut would otherwise restore 90.
    """
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 90})
    await tick(hass, freezer, 3)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _saved_position(hass_storage) == 90
    with _counting_saves() as saves:
        async_press_wall_button(hass, CLOSE_RELAY, True)
        await hass.async_block_till_done()
        await tick(hass, freezer, 26)
        assert saves.call_count == 0
        await tick(hass, freezer, 120)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert cover_state(hass) == (CoverState.CLOSING, 0)
        assert saves.call_count == 1
        assert _saved_position(hass_storage) == 0
        # Released at the end: nothing new to save
        async_press_wall_button(hass, CLOSE_RELAY, False)
        await tick(hass, freezer, 5)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saves.call_count == 1


@pytest.mark.parametrize("saved_position", [50])
@pytest.mark.parametrize("end_margin", [20])
@pytest.mark.usefixtures("cover")
async def test_order_to_an_end_with_margin_saves_once(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """Saved on reaching the end; the stop after the margin saves nothing more.

    From 50: 10 s to the end and 4 s of margin.
    """
    with _counting_saves() as saves:
        await call(hass, SERVICE_OPEN_COVER)
        await tick(hass, freezer, 11)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert relay_states(hass) == (STATE_ON, STATE_OFF)
        assert saves.call_count == 1
        assert _saved_position(hass_storage) == 100
        await tick(hass, freezer, 5)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert relay_states(hass) == (STATE_OFF, STATE_OFF)
        assert saves.call_count == 1


@pytest.mark.usefixtures("cover")
async def test_reversal_saves_only_when_it_ends(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """The pause of a reversal with interlock is not the end of a movement."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 50})
    await tick(hass, freezer, 4)
    with _counting_saves() as saves:
        await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 10})
        await tick(hass, freezer, 1)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert relay_states(hass) == (STATE_OFF, STATE_ON)
        assert saves.call_count == 0
        await tick(hass, freezer, 10)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert relay_states(hass) == (STATE_OFF, STATE_OFF)
        assert saves.call_count == 1
        assert _saved_position(hass_storage) == 10


@pytest.mark.usefixtures("cover")
async def test_glitch_of_the_running_relay_is_a_stop_and_a_press(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """Off and on again shown by HA while moving: a stop and a press from outside.

    The stop saves; the cover then runs to the end, saves there once and is left on.
    """
    with _counting_saves() as saves:
        await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
        await tick(hass, freezer, 2)
        hass.states.async_set(OPEN_RELAY, STATE_OFF)
        await hass.async_block_till_done()
        await tick(hass, freezer, STEP)
        hass.states.async_set(OPEN_RELAY, STATE_ON)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saves.call_count == 1
        assert _saved_position(hass_storage) == 10
        await tick(hass, freezer, 20)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert cover_state(hass) == (CoverState.OPENING, 100)
        assert relay_states(hass) == (STATE_ON, STATE_OFF)
        assert saves.call_count == 2
        assert _saved_position(hass_storage) == 100


@pytest.mark.usefixtures("cover")
async def test_wall_stop_saves_at_once(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """A stop from outside is saved at once: it is never undone as a glitch."""
    await call(hass, SERVICE_SET_COVER_POSITION, **{ATTR_POSITION: 30})
    await tick(hass, freezer, 3)
    with _counting_saves() as saves:
        async_press_wall_button(hass, OPEN_RELAY, False)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert cover_state(hass) == (CoverState.OPEN, 15)
        assert saves.call_count == 1
        assert _saved_position(hass_storage) == 15


@pytest.mark.usefixtures("cover")
async def test_short_move_from_an_end_is_saved(
    hass: HomeAssistant, freezer, hass_storage: dict[str, Any]
) -> None:
    """A move shorter than the position refresh is saved too."""
    with _counting_saves() as saves:
        await call(hass, SERVICE_OPEN_COVER)
        await tick(hass, freezer, 0.5)
        await call(hass, SERVICE_STOP_COVER)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saves.call_count == 1
        assert _saved_position(hass_storage) == 2.5


@pytest.mark.parametrize("opening_time", [20, 5], ids=["slow", "fast"])
@pytest.mark.parametrize("confirm_delay", [-1])
@pytest.mark.usefixtures("cover")
async def test_lost_on_saves_nothing(hass: HomeAssistant, freezer) -> None:
    """A relay that loses every on: the position never moves, nothing is saved.

    The order is given up at 10 s and the cover shows stopped.
    """
    with _counting_saves() as saves:
        await call(hass, SERVICE_OPEN_COVER)
        await tick(hass, freezer, 60)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert saves.call_count == 0
    assert cover_state(hass) == (CoverState.CLOSED, 0)
