"""Pure tests of the movement controller (no Home Assistant).

Times: opening 20 s (5 %/s), closing 30 s (3.33 %/s); D = DEAD_TIME (0.5 s); no margin at
the ends unless a test sets one. Rules checked:
- An order to an end runs the time to the end plus a margin, a percent of the full travel
  of that direction, at most a full travel. A move to a middle position has no margin.
- The dead time is always 0.5 s from the last off, ordered or shown, also before the relay
  confirms. An on that may still be queued in the relay is not waited for.
- Without interlock off and on go out back to back. With interlock a reversal waits at
  most REVERSAL_TIMEOUT (2 s) for the off to be shown.
- A relay switched on from outside is never switched off by the integration, with or
  without interlock, also at the end; a wall button towards the end the cover is believed
  at is left as the person left it. Only with interlock ours goes off when the other one is
  switched on from outside. A stop from Home Assistant or an order reaching its target
  switches off both.
- Without interlock no relay is switched because of what the relays did from outside: a
  change from outside leaves the relays as they are, ours too, with no target and no
  timer, and the position follows the relay switched on last.
- What Home Assistant shows is the only truth: the position starts and stops with it. Of
  each relay only the last command is kept; it is not repeated while Home Assistant does
  not show it and is sent again CONFIRM_TIMEOUT (5 s) after it was sent if it still does
  not: an on only once, an off every 5 s until it is shown. An order whose relay is not
  shown on 10 s after it was given is given up. A call that raises is the same as a lost
  command, and a lost command of ours is never followed as a wall button.
- Within CONFIRM_TIMEOUT a different value shown is taken as an earlier command shown late,
  not as a press. A glitch of a relay (off and on again) is two presses, never undone.
"""

import contextlib
import random

import pytest

from custom_components.relay_cover.motion import (
    CONFIRM_TIMEOUT,
    DEAD_TIME,
    REVERSAL_TIMEOUT,
    MotionController,
    Move,
    PositionUnknownError,
    Relay,
    RelayUnavailableError,
    Stop,
)
from custom_components.relay_cover.travel import Direction

from .motion_sim import Sim

D = DEAD_TIME

OPEN, CLOSE = Relay.OPEN, Relay.CLOSE
ON, OFF = True, False


def test_open_full_stops_at_computed_time() -> None:
    """0 to 100 in 20 s: the relay goes off at 20 s, no extra run time."""
    sim = Sim(interlock=True, position=0)
    sim.open()
    assert sim.log == [(0, OPEN, ON)]
    assert sim.controller.next_deadline(sim.now) == 20
    sim.tick(10)
    assert (sim.position, sim.shown) == (50, Direction.OPENING)
    sim.tick(10)
    assert sim.log[-1] == (20, OPEN, OFF)
    assert (sim.position, sim.shown) == (100, None)


def test_close_full_uses_closing_time() -> None:
    """100 to 0 in 30 s."""
    sim = Sim(interlock=True, position=100)
    sim.close()
    sim.tick(15)
    assert sim.position == 50
    sim.tick(20)
    assert sim.log == [(0, CLOSE, ON), (30, CLOSE, OFF)]
    assert sim.position == 0


@pytest.mark.parametrize(
    ("start", "target", "stop_at"),
    [
        pytest.param(0, 30, 6, id="up_30pct_of_20s"),
        pytest.param(80, 20, 18, id="down_60pct_of_30s"),
        pytest.param(50, 100, 10, id="up_to_the_end"),
    ],
)
def test_set_position_stops_on_time(start: int, target: int, stop_at: float) -> None:
    """A position stops exactly at the computed time, also at an end."""
    sim = Sim(interlock=True, position=start)
    sim.move(target)
    sim.tick(stop_at)
    assert sim.log[-1] == (stop_at, OPEN if target > start else CLOSE, OFF)
    assert sim.position == target


def test_stop_in_the_middle() -> None:
    """Stop freezes the position."""
    sim = Sim(interlock=True, position=0)
    sim.open()
    sim.tick(7)
    sim.stop()
    assert sim.log == [(0, OPEN, ON), (7, OPEN, OFF)]
    sim.tick(30)
    assert (sim.position, sim.shown) == (35, None)


def test_target_equal_to_position() -> None:
    """Idle at the target does nothing; moving through it stops."""
    sim = Sim(interlock=True, position=40)
    sim.move(40)
    assert sim.log == []
    sim = Sim(interlock=True, position=0)
    sim.open()
    sim.tick(8)
    sim.move(40)
    assert sim.log == [(0, OPEN, ON), (8, OPEN, OFF)]


@pytest.mark.parametrize(("position", "end"), [(100, 100), (0, 0)])
def test_end_when_already_there_sends_nothing(position: int, end: int) -> None:
    """Without margin, open at 100 or close at 0 has nothing to run: no relay pulse."""
    sim = Sim(interlock=True, position=position)
    sim.move(end)
    sim.tick(5)
    assert sim.log == []
    assert sim.shown is None


def test_wall_press_towards_the_end_it_is_at_is_left_on() -> None:
    """Believed open, a wall on of the open relay stays on until it is switched off.

    The position stays at the end; the relay goes off only when the person switches it off.
    """
    sim = Sim(interlock=True, position=100)
    assert sim.press(OPEN, ON)
    assert sim.controller.next_deadline(sim.now) is None
    sim.tick(600)
    assert sim.log == []
    assert sim.physical[OPEN] is ON
    assert (sim.position, sim.shown) == (100, Direction.OPENING)
    assert sim.press(OPEN, OFF)
    assert (sim.position, sim.shown) == (100, None)
    assert sim.log == []


def test_wall_press_towards_the_end_it_is_at_then_back() -> None:
    """Held at the end, the other wall button: with interlock open goes off.

    The cover closes to the end and close is left on there.
    """
    sim = Sim(interlock=True, position=100)
    sim.press(OPEN, ON)
    sim.tick(5)
    sim.press(CLOSE, ON)
    assert sim.log == [(5, OPEN, OFF)]
    sim.tick(60)
    assert sim.log == [(5, OPEN, OFF)]
    assert (sim.position, sim.shown) == (0, Direction.CLOSING)


def test_new_target_same_direction_keeps_relay() -> None:
    """Going to 80, ask 60 at 4 s: no relay change, stop at 60."""
    sim = Sim(interlock=True, position=0)
    sim.move(80)
    sim.tick(4)
    sim.move(60)
    sim.tick(20)
    assert sim.log == [(0, OPEN, ON), (12, OPEN, OFF)]
    assert sim.position == 60


def test_unknown_position() -> None:
    """Unknown position: middle refused, an end runs exactly a full travel."""
    sim = Sim(interlock=True, position=None)
    with pytest.raises(PositionUnknownError):
        sim.move(50)
    sim.open()
    sim.tick(19)
    assert sim.position is None
    sim.tick(1)
    assert sim.position == 100
    assert sim.log == [(0, OPEN, ON), (20, OPEN, OFF)]


def _margin_sim(position: float | None, end_margin: float = 0.2) -> Sim:
    """Up 10 s, down 20 s, as in the example of the margin at the ends."""
    return Sim(
        interlock=True,
        position=position,
        end_margin=end_margin,
        opening_time=10,
        closing_time=20,
    )


@pytest.mark.parametrize(
    ("position", "target", "off_at"),
    [
        pytest.param(90, 100, 3, id="open_from_90_1s_plus_2s"),
        pytest.param(90, 0, 20, id="close_from_90_capped_at_a_full_travel"),
        pytest.param(0, 0, 4, id="close_when_closed_runs_the_margin"),
        pytest.param(90, 50, 8, id="middle_has_no_margin"),
    ],
)
def test_end_margin(position: float, target: int, off_at: float) -> None:
    """Up 10 s, down 20 s, 20 %: 1 + 2 s; 18 + 4 s capped at 20 s; 0 + 4 s; none."""
    sim = _margin_sim(position)
    sim.move(target)
    relay = OPEN if target > position or target == 100 else CLOSE
    assert sim.log == [(0, relay, ON)]
    sim.tick(off_at - 0.25)
    assert sim.log == [(0, relay, ON)]
    sim.tick(0.25)
    assert sim.log == [(0, relay, ON), (off_at, relay, OFF)]
    assert (sim.position, sim.shown) == (target, None)


def test_end_margin_shows_the_end_while_moving() -> None:
    """During the extra time the cover is at the end, still shown opening."""
    sim = _margin_sim(90)
    sim.open()
    sim.tick(2)
    assert (sim.position, sim.shown) == (100, Direction.OPENING)
    assert sim.controller.calc.position(sim.now) == 100
    sim.tick(1)
    assert (sim.position, sim.shown) == (100, None)


def test_end_margin_is_not_extended_by_asking_again() -> None:
    """Asking for the same end during the extra time keeps the time of the relay."""
    sim = _margin_sim(90)
    sim.open()
    sim.tick(2)
    sim.open()
    sim.tick(5)
    assert sim.log == [(0, OPEN, ON), (3, OPEN, OFF)]


def test_order_to_the_end_a_wall_button_runs_to() -> None:
    """Opening from the wall, open from HA: the order takes the relay with margin.

    The time counts from when the motor started: 5 s from 50 plus 2 s.
    """
    sim = _margin_sim(50)
    sim.press(OPEN, ON)
    sim.tick(3)
    sim.open()
    sim.tick(10)
    assert sim.log == [(7, OPEN, OFF)]
    assert (sim.position, sim.shown) == (100, None)


@pytest.mark.parametrize("interlock", [True, False])
def test_order_takes_the_relay_of_its_direction(interlock: bool) -> None:
    """The wall holds open from 20, HA asks for 50: open goes off at 50."""
    sim = Sim(interlock=interlock, position=20)
    sim.press(OPEN, ON)
    sim.tick(2)
    sim.move(50)
    assert sim.log == []
    sim.tick(30)
    assert sim.log == [(6, OPEN, OFF)]
    assert (sim.position, sim.shown) == (50, None)


def test_stop_switches_off_a_wall_relay_left_after_an_order() -> None:
    """After our close reaches 30 the wall's open is left on; a stop switches it off."""
    sim = Sim(interlock=False, position=50)
    sim.press(OPEN, ON)
    sim.tick(2)
    sim.move(30)
    sim.tick(10)
    sim.stop()
    assert sim.commands_since(11.5) == [(12, OPEN, OFF)]
    assert sim.physical == {OPEN: OFF, CLOSE: OFF}


def test_opposite_order_switches_off_a_wall_relay_with_interlock() -> None:
    """The wall holds open, HA asks for 30: with interlock open goes off first.

    Without it the cover could not move; close goes on after the dead time.
    """
    sim = Sim(interlock=True, position=50)
    sim.press(OPEN, ON)
    sim.tick(2)
    sim.move(30)
    sim.tick(D)
    assert sim.log == [(2, OPEN, OFF), (2 + D, CLOSE, ON)]
    assert sim.violations == []


def test_without_end_margin_nothing_changes() -> None:
    """0 %: the time to the end only, and nothing at the end it is at."""
    sim = _margin_sim(90, end_margin=0)
    sim.open()
    sim.tick(5)
    sim.open()
    sim.tick(5)
    assert sim.log == [(0, OPEN, ON), (1, OPEN, OFF)]


@pytest.mark.parametrize(
    ("position", "target", "relay", "off_at"),
    [
        pytest.param(90, 100, OPEN, 10, id="open_from_90"),
        pytest.param(50, 0, CLOSE, 20, id="close_from_50"),
        pytest.param(100, 100, OPEN, 10, id="open_when_open"),
        pytest.param(None, 0, CLOSE, 20, id="close_from_unknown"),
    ],
)
def test_full_end_margin_always_runs_a_full_travel(
    position: float | None, target: int, relay: Relay, off_at: float
) -> None:
    """100 %: an order to an end always keeps the relay on a full travel."""
    sim = _margin_sim(position, end_margin=1)
    sim.move(target)
    sim.tick(30)
    assert sim.log == [(0, relay, ON), (off_at, relay, OFF)]
    assert sim.position == target


def _reverse_at_50(interlock: bool, confirm_delay: float | None = 0) -> Sim:
    """Open from 20 to 50, then ask 30; the relays answer with `confirm_delay` from then on."""
    sim = Sim(interlock=interlock, position=20)
    sim.move(80)
    sim.tick(6)
    assert sim.position == 50
    sim.confirm_delay = confirm_delay
    sim.move(30)
    return sim


def test_reverse_with_interlock() -> None:
    """Off, dead time, on; position frozen and closing shown meanwhile."""
    sim = _reverse_at_50(interlock=True)
    assert sim.commands_since(6) == [(6, OPEN, OFF)]
    assert (sim.position, sim.shown) == (50, Direction.CLOSING)
    assert sim.controller.next_deadline(sim.now) == 6 + D
    sim.tick(D / 2)
    assert sim.commands_since(6) == [(6, OPEN, OFF)]
    sim.tick(D / 2)
    assert sim.commands_since(6) == [(6, OPEN, OFF), (6 + D, CLOSE, ON)]
    sim.tick(10)
    assert sim.log[-1] == (12 + D, CLOSE, OFF)
    assert sim.position == 30
    assert sim.violations == []


@pytest.mark.parametrize("confirm_delay", [0, 3, None])
def test_reverse_without_interlock(confirm_delay: float | None) -> None:
    """No interlock: off first, on right after, no wait, whatever the relay reports."""
    sim = _reverse_at_50(interlock=False, confirm_delay=confirm_delay)
    assert sim.commands_since(6) == [(6, OPEN, OFF), (6, CLOSE, ON)]
    if confirm_delay == 0:
        sim.tick(10)
        assert sim.log[-1] == (12, CLOSE, OFF)
        assert sim.position == 30


def test_open_while_closing_with_interlock() -> None:
    """Open while closing: close off, open on after the dead time, to 100 exactly."""
    sim = Sim(interlock=True, position=100)
    sim.close()
    sim.tick(3)
    sim.open()
    sim.tick(20)
    assert sim.log == [
        (0, CLOSE, ON),
        (3, CLOSE, OFF),
        (3 + D, OPEN, ON),
        (5 + D, OPEN, OFF),
    ]
    assert sim.position == 100


def test_stop_during_dead_time() -> None:
    """Stop while waiting: the other relay never comes on."""
    sim = _reverse_at_50(interlock=True)
    sim.tick(D / 2)
    sim.stop()
    sim.tick(10)
    assert sim.commands_since(6) == [(6, OPEN, OFF)]
    assert (sim.position, sim.shown) == (50, None)


def test_back_to_first_direction_during_dead_time() -> None:
    """Back to opening while waiting: open again at once (no reversal)."""
    sim = _reverse_at_50(interlock=True)
    sim.tick(D / 2)
    sim.move(80)
    assert sim.commands_since(6) == [(6, OPEN, OFF), (6 + D / 2, OPEN, ON)]
    sim.tick(10)
    assert sim.log[-1] == (12 + D / 2, OPEN, OFF)
    assert sim.position == 80


def test_new_target_during_dead_time() -> None:
    """Another target in the same new direction: same wait, new target."""
    sim = _reverse_at_50(interlock=True)
    sim.tick(D / 2)
    sim.move(10)
    sim.tick(20)
    assert sim.commands_since(6) == [
        (6, OPEN, OFF),
        (6 + D, CLOSE, ON),
        (18 + D, CLOSE, OFF),
    ]
    assert sim.position == 10


def test_stop_then_reverse_waits_dead_time() -> None:
    """Closing, stop, open D/2 later: the dead time counts from the stop."""
    sim = Sim(interlock=True, position=100)
    sim.close()
    sim.tick(3)
    sim.stop()
    sim.tick(D / 2)
    sim.open()
    assert sim.commands_since(3) == [(3, CLOSE, OFF)]
    sim.tick(D / 2)
    assert sim.commands_since(3) == [(3, CLOSE, OFF), (3 + D, OPEN, ON)]


def test_dead_time_counts_from_the_reported_off() -> None:
    """Relay reporting 1.5 s late and still shown on: close waits for the off, then D.

    The position follows what Home Assistant shows: opening until 7.5 (57.5),
    closing from 9.5 (close shown on) for the 8.25 s down to 30, and still closing until
    close is shown off 1.5 s later (25).
    """
    sim = _reverse_at_50(interlock=True, confirm_delay=1.5)
    sim.tick(1.5)
    assert sim.commands_since(6) == [(6, OPEN, OFF)]
    sim.tick(D)
    assert sim.commands_since(6) == [(6, OPEN, OFF), (7.5 + D, CLOSE, ON)]
    sim.tick(15)
    assert sim.log[-1] == (17.75, CLOSE, OFF)
    assert sim.position == 25
    assert sim.violations == []


def test_reversal_waits_at_most_two_seconds_for_the_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Off shown 3 s late: the reversal is given up at 2 s and the cover stays.

    Home Assistant shows open on until 9 s: the cover shows opening until then and
    the position follows it (50 to 65).
    """
    sim = _reverse_at_50(interlock=True, confirm_delay=3)
    assert REVERSAL_TIMEOUT == 2
    sim.tick(REVERSAL_TIMEOUT - 0.1)
    assert isinstance(sim.controller.goal, Move)
    sim.tick(0.1)
    assert isinstance(sim.controller.goal, Stop)
    assert "not reversing" in caplog.text
    assert sim.shown is Direction.OPENING
    sim.tick(60)
    assert sim.log[-1] == (6, OPEN, OFF)
    assert (sim.position, sim.shown) == (65, None)
    assert sim.physical == {OPEN: OFF, CLOSE: OFF}


def test_reversal_given_up_if_relay_stays_on(caplog: pytest.LogCaptureFixture) -> None:
    """Our off of the open relay is lost: never turn the other on.

    After 2 s the reversal is given up. After `CONFIRM_TIMEOUT` the off goes out again.
    The position follows what Home Assistant shows: the cover ran up to 75
    meanwhile.
    """
    sim = _reverse_at_50(interlock=True, confirm_delay=None)
    sim.tick(REVERSAL_TIMEOUT - 0.1)
    assert isinstance(sim.controller.goal, Move)
    sim.tick(0.2)
    assert isinstance(sim.controller.goal, Stop)
    assert sim.commands_since(6) == [(6, OPEN, OFF)]
    sim.confirm_delay = 0
    sim.tick(CONFIRM_TIMEOUT)
    assert sim.commands_since(6) == [(6, OPEN, OFF), (6 + CONFIRM_TIMEOUT, OPEN, OFF)]
    assert sim.shown is None
    assert sim.position == 75
    assert "not reversing" in caplog.text


def test_due_give_up_is_applied_and_a_new_request_waits_again() -> None:
    """A later close waits again, with a deadline, instead of hanging."""
    sim = _reverse_at_50(interlock=True, confirm_delay=None)
    sim.tick(REVERSAL_TIMEOUT + 1)
    assert isinstance(sim.controller.goal, Stop)
    sim.close()
    assert isinstance(sim.controller.goal, Move)
    assert sim.controller.next_deadline(sim.now) == sim.now + REVERSAL_TIMEOUT


def test_reversal_with_unconfirmed_commands() -> None:
    """HA shows none of our commands: close comes on D after our off of open.

    Nor does the position move: Home Assistant never showed a relay on. Close waits only
    the dead time, not for the unconfirmed on of 0 s to expire.
    """
    sim = Sim(interlock=True, position=50, confirm_delay=None)
    sim.move(80)
    sim.tick(3)
    sim.move(30)
    sim.tick(D)
    assert sim.log == [(0, OPEN, ON), (3, OPEN, OFF), (3 + D, CLOSE, ON)]
    assert sim.position == 50


def test_quick_reversal_of_an_unconfirmed_on() -> None:
    """Open, then close 0.25 s later before any report: close on D after our off."""
    sim = Sim(interlock=True, position=50, confirm_delay=None)
    sim.open()
    sim.tick(0.25)
    sim.close()
    sim.tick(D)
    assert sim.log == [(0, OPEN, ON), (0.25, OPEN, OFF), (0.25 + D, CLOSE, ON)]


@pytest.mark.parametrize("interlock", [True, False])
@pytest.mark.parametrize("relay", [OPEN, CLOSE])
def test_external_start_is_left_on(relay: Relay, interlock: bool) -> None:
    """A wall press: followed to the end and left on there, with or without interlock.

    No timer switches it off at the end; the position stays at the end.
    """
    sim = Sim(interlock=interlock, position=50, end_margin=0.2)
    assert sim.press(relay, ON)
    sim.tick(60)
    assert sim.log == []
    assert sim.controller.next_deadline(sim.now) is None
    assert (sim.position, sim.shown) == (relay.direction.end, relay.direction)
    sim.press(relay, OFF)
    assert (sim.position, sim.shown) == (relay.direction.end, None)


def test_external_stop() -> None:
    """Off/off from outside while moving: stop and freeze."""
    sim = Sim(interlock=True, position=0)
    sim.move(80)
    sim.tick(4)
    assert sim.press(OPEN, OFF)
    sim.tick(30)
    assert sim.log == [(0, OPEN, ON)]
    assert (sim.position, sim.shown) == (20, None)


@pytest.mark.parametrize("newest", [OPEN, CLOSE])
def test_external_both_on(newest: Relay) -> None:
    """On/on from outside with interlock: the older goes off, the newest goes on.

    The interlock switches off the relay switched on before, also one of outside. The
    newest is left on, also at the end.
    """
    sim = Sim(interlock=True, position=50)
    sim.press(newest.other, ON)
    sim.tick(2)
    sim.press(newest, ON)
    assert sim.log == [(2, newest.other, OFF)]
    sim.tick(60)
    assert sim.log == [(2, newest.other, OFF)]
    assert sim.physical == {newest: ON, newest.other: OFF}
    assert (sim.position, sim.shown) == (newest.direction.end, newest.direction)


def test_wall_reverse_while_our_move_runs() -> None:
    """Opening by our order, the close relay comes on from outside: open goes off.

    It is the one exception to leaving relays of outside alone. Close stays on, also at
    the end: the cover closes to 0 and nothing else is switched.
    """
    sim = Sim(interlock=True, position=20, end_margin=0.2)
    sim.move(80)
    sim.tick(4)
    sim.press(CLOSE, ON)
    assert sim.commands_since(4) == [(4, OPEN, OFF)]
    assert sim.physical == {OPEN: OFF, CLOSE: ON}
    assert sim.shown is Direction.CLOSING
    sim.tick(60)
    assert sim.commands_since(4.1) == []
    assert sim.physical == {OPEN: OFF, CLOSE: ON}
    assert (sim.position, sim.shown) == (0, Direction.CLOSING)
    assert sim.violations == []


def test_wall_reverse_while_our_move_runs_without_interlock() -> None:
    """Opening to 80 by our order, close comes on from outside at 4: both stay on.

    Our target is given up: no relay is switched, the position follows close (the last
    one on) and, once close is released at 7, open again up to the end.
    """
    sim = Sim(interlock=False, position=20)
    sim.move(80)
    sim.tick(4)
    sim.press(CLOSE, ON)
    assert sim.physical == {OPEN: ON, CLOSE: ON}
    assert sim.shown is Direction.CLOSING
    sim.tick(3)
    # 40 % at 4 s, then 3 s closing
    assert sim.position == 30
    sim.press(CLOSE, OFF)
    assert sim.shown is Direction.OPENING
    sim.tick(60)
    assert sim.log == [(0, OPEN, ON)]
    assert sim.controller.next_deadline(sim.now) is None
    assert (sim.position, sim.physical[OPEN]) == (100, ON)


def test_stop_switches_off_both_relays_without_interlock() -> None:
    """Opening by our order, close on from outside, stop: both go off."""
    sim = Sim(interlock=False, position=20)
    sim.move(80)
    sim.tick(4)
    sim.press(CLOSE, ON)
    sim.tick(1)
    sim.stop()
    assert sorted(sim.commands_since(5)) == [(5, CLOSE, OFF), (5, OPEN, OFF)]
    assert sim.physical == {OPEN: OFF, CLOSE: OFF}
    assert sim.shown is None


def test_order_after_a_wall_press_without_interlock_has_a_timer() -> None:
    """Left as the wall left it, a new order is ours again: 70 stops at 70."""
    sim = Sim(interlock=False, position=20)
    sim.move(80)
    sim.tick(4)
    sim.press(CLOSE, ON)
    sim.tick(1)
    sim.press(CLOSE, OFF)
    # 40 % at 4 s, 36.67 at 5 s; open is still on: 70 at 5 + 33.33 / 5 s
    sim.move(70)
    sim.tick(30)
    assert sim.commands_since(0.1) == [(pytest.approx(5 + 100 / 15), OPEN, OFF)]
    assert (sim.position, sim.shown) == (70, None)


def test_lost_on_while_left_as_is_without_interlock() -> None:
    """Our close on is lost; the wall presses open and releases it: nothing moves.

    The press gives the order up: the cover shows stopped, and the lost on is not sent
    again when its 5 s are over.
    """
    sim = Sim(interlock=False, position=50, confirm_delay=None)
    sim.close()
    sim.tick(1)
    sim.press(OPEN, ON)
    sim.tick(1)
    sim.press(OPEN, OFF)
    assert sim.shown is None
    sim.tick(CONFIRM_TIMEOUT)
    assert sim.log == [(0, CLOSE, ON)]
    assert sim.shown is None
    assert sim.controller.next_deadline(sim.now) is None


@pytest.mark.parametrize("released", [False, True])
def test_failed_on_while_left_as_is_without_interlock(released: bool) -> None:
    """Our open on raises (nothing shown); the wall switches close on.

    Nothing is sent, now or after 5 s: close is left on, or, released, the cover shows
    stopped.
    """
    sim = Sim(interlock=False, position=50, confirm_delay=None)
    controller = sim.controller
    controller.request_move(80, 0)
    assert controller.next_command(0) == (OPEN, ON)
    controller.command_sent(OPEN, ON, 0)
    sim.now = 1
    controller.relay_reported(CLOSE, ON, 1)
    if released:
        controller.relay_reported(CLOSE, OFF, 1.5)
    for now in (2, CONFIRM_TIMEOUT + 1):
        assert controller.next_command(now) is None
    assert sim.shown is (None if released else Direction.CLOSING)


def test_external_quick_reversal_with_interlock() -> None:
    """Wall stop, then wall reverse D/2 later: the new relay is left on, no pause."""
    sim = Sim(interlock=True, position=50)
    sim.press(CLOSE, ON)
    sim.tick(3)
    sim.press(CLOSE, OFF)
    sim.tick(D / 2)
    sim.press(OPEN, ON)
    sim.tick(D)
    assert sim.log == []
    assert sim.shown is Direction.OPENING


@pytest.mark.parametrize(
    ("newest", "after_4_s", "end_at"),
    [
        # Closing 2 s from 50 (43.33), then opening at 5 %/s up to 100
        pytest.param(OPEN, 63, 2 + (100 - 50 + 20 / 3) / 5, id="open_last"),
        # Opening 2 s from 50 (60), then closing at 3.33 %/s down to 0
        pytest.param(CLOSE, 47, 2 + 60 * 0.3, id="close_last"),
    ],
)
def test_external_both_on_without_interlock(
    newest: Relay, after_4_s: int, end_at: float
) -> None:
    """On/on from outside: neither is switched off; the last one is followed."""
    sim = Sim(interlock=False, position=50)
    sim.press(newest.other, ON)
    sim.tick(2)
    sim.press(newest, ON)
    assert sim.log == []
    assert sim.physical == {OPEN: ON, CLOSE: ON}
    assert sim.shown is newest.direction
    sim.tick(4)
    assert sim.position == after_4_s
    sim.tick(end_at - 6 - 1)
    assert sim.position is not None
    assert 0 < sim.position < 100
    sim.tick(60)
    # No timer: both are left on at the end
    assert sim.log == []
    assert sim.physical == {OPEN: ON, CLOSE: ON}
    assert (sim.position, sim.shown) == (newest.direction.end, newest.direction)


def test_external_quick_reversal_without_interlock() -> None:
    """The other relay pressed while one is on from outside: both left on."""
    sim = Sim(interlock=False, position=50)
    sim.press(CLOSE, ON)
    sim.tick(0.25)
    sim.press(OPEN, ON)
    sim.tick(D)
    assert sim.log == []
    assert sim.physical == {OPEN: ON, CLOSE: ON}
    assert sim.shown is Direction.OPENING


@pytest.mark.parametrize("device_interlock", [False, True])
def test_our_reversal_leaves_a_relay_switched_on_from_outside(
    device_interlock: bool,
) -> None:
    """Opening by a wall button, we close to 30: only close goes on, at once.

    The open relay is left as it is. A relay module with its own interlock switches it off
    and reports it: the target is kept. Either way the cover reaches 30 and only our close
    goes off there. Without the module's interlock the wall's open is still on: the
    position follows it to 100 and it is left on.
    """
    sim = Sim(interlock=False, position=50)
    sim.press(OPEN, ON)
    sim.tick(2)
    sim.move(30)
    assert sim.commands_since(2) == [(2, CLOSE, ON)]
    assert sim.physical == {OPEN: ON, CLOSE: ON}
    assert sim.shown is Direction.CLOSING
    if device_interlock:
        sim.press(OPEN, OFF)
    # From 60 % down to 30 %: 9 s
    sim.tick(9)
    assert sim.commands_since(2.1) == [(11, CLOSE, OFF)]
    assert sim.position == 30
    sim.tick(30)
    assert sim.commands_since(2.1) == [(11, CLOSE, OFF)]
    if device_interlock:
        assert sim.physical == {OPEN: OFF, CLOSE: OFF}
        assert (sim.position, sim.shown) == (30, None)
    else:
        assert sim.physical == {OPEN: ON, CLOSE: OFF}
        assert (sim.position, sim.shown) == (100, Direction.OPENING)


def test_reversal_without_interlock_ignores_the_other_relay() -> None:
    """The close relay, last seen on from outside, drops out: opening still goes."""
    sim = Sim(interlock=False, position=50)
    sim.press(CLOSE, ON)
    sim.tick(1)
    sim.report_raw(CLOSE, None)
    sim.open()
    assert sim.commands_since(1) == [(1, OPEN, ON)]
    assert sim.shown is Direction.OPENING


def test_glitch_of_our_relay_is_two_presses_without_interlock() -> None:
    """A read glitch of our open relay is two presses: a reversal leaves it on."""
    sim = Sim(interlock=False, position=20)
    sim.move(80)
    sim.tick(2)
    sim.report_raw(OPEN, OFF)
    sim.report_raw(OPEN, ON)
    sim.tick(1)
    sim.move(30)
    assert sim.commands_since(2) == [(3, CLOSE, ON)]
    assert sim.physical == {OPEN: ON, CLOSE: ON}
    assert sim.position == 35


def test_wall_stop_soon_after_our_start_reverses_after_dead_time() -> None:
    """Our open at 0, wall stop at 3, close at 4: close on at once.

    An on of ours that may still be queued in the relay is not waited for.
    """
    sim = Sim(interlock=True, position=0)
    sim.open()
    sim.tick(3)
    assert sim.press(OPEN, OFF)
    sim.tick(1)
    sim.close()
    assert sim.commands_since(3) == [(4, CLOSE, ON)]


def test_wall_stop_then_immediate_close_waits_dead_time() -> None:
    """Wall stop at 12 and close right away: only the dead time from the reported off."""
    sim = Sim(interlock=True, position=0)
    sim.open()
    sim.tick(12)
    sim.press(OPEN, OFF)
    sim.close()
    assert sim.commands_since(12) == []
    sim.tick(D)
    assert sim.commands_since(12) == [(12 + D, CLOSE, ON)]


def test_echo_versus_external_table() -> None:
    """What the controller makes of each report: the order goes on, or is given up.

    Value and the last command only, no context: within the 5 s of our last
    command a different value is taken as an earlier command shown late.
    """
    rows: list[tuple[str, bool, bool]] = []

    sim = Sim(interlock=True, position=0)
    sim.open()
    rows.append(("own command, instant echo", sim.controller.goal is not None, True))

    sim = Sim(interlock=True, position=0)
    sim.press(OPEN, ON)
    rows.append(("wall button while idle", sim.shown is Direction.OPENING, True))

    sim = Sim(interlock=True, position=0)
    sim.open()
    sim.tick(2)
    sim.press(OPEN, OFF)
    rows.append(("wall button 2 s after our order", sim.controller.goal is None, True))

    sim = Sim(interlock=True, position=0, confirm_delay=6)
    sim.open()
    sim.tick(6)
    rows.append(("device confirms at 6 s", sim.controller.goal is not None, True))

    sim = Sim(interlock=True, position=0, confirm_delay=6)
    sim.open()
    sim.tick(1)
    # Home Assistant still shows off: the press makes no event at all
    rows.append(("wall button before the late echo", sim.press(OPEN, OFF), False))

    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.open()
    sim.tick(1)
    sim.stop()
    sim.report_raw(OPEN, ON)
    rows.append(
        ("late echo of an overtaken command", sim.controller.goal is not None, True)
    )
    sim.report_raw(OPEN, OFF)
    rows.append(
        ("late echo of the newest command", sim.controller.goal is not None, True)
    )

    assert [r[1] for r in rows] == [r[2] for r in rows], rows


def test_late_echo_of_overtaken_command_moves_nothing() -> None:
    """On then off unconfirmed; echoes on, off arrive late: no command, no new goal.

    While Home Assistant shows on the position follows it: 1 s at 5 %/s.
    """
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.open()
    sim.tick(1)
    sim.stop()
    sim.tick(5)
    sim.report_raw(OPEN, ON)
    assert sim.shown is Direction.OPENING
    sim.tick(1)
    sim.report_raw(OPEN, OFF)
    assert sim.log == [(0, OPEN, ON), (1, OPEN, OFF)]
    assert (sim.position, sim.shown) == (5, None)


def test_button_before_late_echo_then_device_still_on() -> None:
    """Button off before our late echo is not seen; the device then reports on."""
    sim = Sim(interlock=True, position=0, confirm_delay=6)
    sim.open()
    sim.tick(1)
    assert not sim.press(OPEN, OFF)
    assert sim.shown is Direction.OPENING
    sim.report_raw(OPEN, ON)
    assert sim.shown is Direction.OPENING


def test_press_after_an_order_cancelled_without_event() -> None:
    """Open and stop before any report, HA never changes; a wall on after 11 s is followed.

    The window is that of the last command, the off of 1 s. It is left on
    at the end.
    """
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.open()
    sim.tick(1)
    sim.stop()
    sim.tick(10.25)
    sim.report_raw(OPEN, ON)
    assert sim.controller.goal is None
    assert sim.shown is Direction.OPENING
    sim.tick(60)
    assert sim.log == [(0, OPEN, ON), (1, OPEN, OFF)]
    assert (sim.position, sim.shown) == (100, Direction.OPENING)


def test_on_shown_within_the_window_of_our_off_is_no_press() -> None:
    """Open and stop unconfirmed; HA shows on at 4 s, within 5 s of our off.

    It is taken as our on shown late, not as a press: our off goes out again at 6 s.
    The position follows Home Assistant from 4 s to 6 s.
    """
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.open()
    sim.tick(1)
    sim.stop()
    sim.tick(CONFIRM_TIMEOUT - 2)
    sim.report_raw(OPEN, ON)
    assert isinstance(sim.controller.goal, Stop)
    assert sim.shown is Direction.OPENING
    sim.confirm_delay = 0
    sim.tick(2)
    assert sim.commands_since(1.5) == [(1 + CONFIRM_TIMEOUT, OPEN, OFF)]
    assert (sim.position, sim.shown) == (10, None)


def test_relay_unavailable_during_movement() -> None:
    """Unavailable keeps the last state; the stop is sent when it is back."""
    sim = Sim(interlock=True, position=0)
    sim.move(30)
    sim.tick(2)
    sim.report_raw(OPEN, None)
    # The close relay still answers: the cover can be stopped
    assert sim.controller.available
    sim.tick(10)
    assert sim.log == [(0, OPEN, ON)]
    assert sim.position == 60
    sim.report_raw(OPEN, ON)
    assert sim.log[-1] == (12, OPEN, OFF)


def test_back_from_unavailable_showing_off_lets_the_other_on() -> None:
    """On and off unconfirmed; the relay comes back showing off: close comes on at once.

    The on that may still be queued is not kept: close does not wait for it to expire.
    """
    sim = Sim(interlock=True, position=50, confirm_delay=None)
    sim.open()
    sim.close()
    sim.report_raw(OPEN, None)
    sim.tick(1)
    assert sim.commands_since(0) == [(0, OPEN, ON), (0, OPEN, OFF)]
    sim.report_raw(OPEN, OFF)
    assert sim.commands_since(0)[-1] == (1, CLOSE, ON)


def test_lost_on_back_from_unavailable_is_sent_again() -> None:
    """Unconfirmed on, back from unavailable showing off: nothing changes then.

    The on goes again 5 s after it was sent. A close in between switches open off first
    and waits the dead time from that off.
    """
    sim = Sim(interlock=True, position=50, confirm_delay=None)
    sim.open()
    sim.report_raw(OPEN, None)
    sim.tick(1)
    sim.report_raw(OPEN, OFF)
    assert sim.commands_since(0.5) == []
    sim.tick(CONFIRM_TIMEOUT - 1)
    assert sim.commands_since(0.5) == [(CONFIRM_TIMEOUT, OPEN, ON)]
    sim.close()
    assert sim.commands_since(0.5)[-1] == (CONFIRM_TIMEOUT, OPEN, OFF)
    sim.tick(D)
    assert sim.commands_since(0.5)[-1] == (CONFIRM_TIMEOUT + D, CLOSE, ON)


def test_lost_off_back_from_unavailable_showing_on_is_sent_again() -> None:
    """The off of a stop is lost and the relay drops out and comes back showing on.

    It is not a wall button: the cover does not run to the end. The off goes
    again 5 s after it was sent, not on the return; the position follows HA until then.
    """
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.open()
    sim.report_raw(OPEN, ON)
    sim.tick(2)
    sim.stop()
    sim.tick(1)
    sim.report_raw(OPEN, None)
    sim.tick(1)
    sim.confirm_delay = 0
    sim.report_raw(OPEN, ON)
    assert sim.commands_since(2) == [(2, OPEN, OFF)]
    sim.tick(CONFIRM_TIMEOUT)
    assert isinstance(sim.controller.goal, Stop)
    assert sim.commands_since(2) == [(2, OPEN, OFF), (2 + CONFIRM_TIMEOUT, OPEN, OFF)]
    assert (sim.position, sim.shown) == (35, None)
    sim.tick(30)
    assert sim.commands_since(2 + CONFIRM_TIMEOUT + 0.1) == []


def test_order_waiting_for_an_unavailable_relay_is_given_up() -> None:
    """A reversal waits the dead time and the relay to switch on drops out.

    The order is given up `2 * CONFIRM_TIMEOUT` after it was given; the relay back an
    hour later moves nothing.
    """
    sim = Sim(interlock=True, position=50)
    sim.open()
    sim.tick(2)
    sim.close()
    sim.tick(0.2)
    sim.report_raw(CLOSE, None)
    assert sim.shown is Direction.CLOSING
    sim.tick(2 * CONFIRM_TIMEOUT - 0.3)
    assert sim.shown is Direction.CLOSING
    sim.tick(0.2)
    assert sim.shown is None
    sim.tick(3600)
    sim.report_raw(CLOSE, OFF)
    assert sim.commands_since(2.1) == []


def test_order_waiting_for_a_relay_back_soon_goes_on() -> None:
    """The relay to switch on drops out for 1 s during the dead time: the reversal goes on."""
    sim = Sim(interlock=True, position=50)
    sim.open()
    sim.tick(2)
    sim.close()
    sim.report_raw(CLOSE, None)
    sim.tick(1)
    sim.report_raw(CLOSE, OFF)
    assert sim.commands_since(2.1) == [(3, CLOSE, ON)]


@pytest.mark.parametrize("interlock", [True, False])
def test_move_needing_an_unavailable_relay_is_refused(interlock: bool) -> None:
    """Towards the relay that dropped out, a move is refused; a stop is not."""
    sim = Sim(interlock=interlock, position=50)
    sim.report_raw(CLOSE, None)
    with pytest.raises(RelayUnavailableError):
        sim.close()
    with pytest.raises(RelayUnavailableError):
        sim.move(20)
    sim.stop()
    assert sim.log == []


def test_move_away_from_an_unavailable_relay() -> None:
    """Without interlock, the other relay last seen off is enough; with it, it is not."""
    sim = Sim(interlock=False, position=50)
    sim.report_raw(CLOSE, None)
    sim.open()
    assert sim.log == [(0, OPEN, ON)]
    sim = Sim(interlock=True, position=50)
    sim.report_raw(CLOSE, None)
    with pytest.raises(RelayUnavailableError):
        sim.open()


def test_stop_while_the_other_relay_is_unavailable() -> None:
    """Opening, the close relay drops out; the open relay still goes off at 60 and on a stop.

    With interlock a new move needs both relays, also in the running direction.
    """
    sim = Sim(interlock=True, position=0)
    sim.move(60)
    sim.tick(2)
    sim.report_raw(CLOSE, None)
    assert sim.controller.available
    with pytest.raises(RelayUnavailableError):
        sim.move(80)
    sim.tick(20)
    assert sim.log == [(0, OPEN, ON), (12, OPEN, OFF)]
    assert sim.position == 60
    sim = Sim(interlock=True, position=0)
    sim.open()
    sim.tick(2)
    sim.report_raw(CLOSE, None)
    sim.tick(1)
    sim.stop()
    assert sim.log == [(0, OPEN, ON), (3, OPEN, OFF)]
    assert sim.position == 15


def test_startup_with_relay_on() -> None:
    """After a restart a relay still on is followed from the restored position.

    Nobody knows who switched it on: it is left on, also at the end.
    """
    controller = MotionController("test", 20, 30, True, 0)
    controller.restore_position(40)
    controller.relay_reported(OPEN, ON, 0)
    controller.relay_reported(CLOSE, OFF, 0)
    assert controller.shown_direction() is Direction.OPENING
    assert controller.next_command(0) is None
    assert controller.next_deadline(0) is None
    assert controller.position(30) == 100


def test_startup_with_both_relays_on() -> None:
    """With interlock, both on after a restart: the one reported first goes off."""
    controller = MotionController("test", 20, 30, True, 0)
    controller.restore_position(40)
    controller.relay_reported(OPEN, ON, 0)
    controller.relay_reported(CLOSE, ON, 0)
    assert controller.next_command(0) == (OPEN, OFF)


def test_startup_other_relay_not_loaded_yet() -> None:
    """One relay on, the other still unknown: do not stop the running one."""
    sim = Sim(interlock=True, position=40)
    for tracker in sim.controller.relays.values():
        tracker.reported, tracker.belief, tracker.available = None, None, False
    sim.press(OPEN, ON)
    assert sim.log == []
    sim.press(CLOSE, OFF)
    assert sim.log == []
    assert sim.shown is Direction.OPENING


@pytest.mark.parametrize("length", [0, 0.25, 0.5])
def test_glitch_of_the_running_relay_is_two_presses(length: float) -> None:
    """Off and on again while moving to 30: two presses, the relay is left on.

    The target is given up and the cover runs to the end, also for a glitch within 0.5 s.
    """
    sim = Sim(interlock=True, position=0)
    sim.move(30)
    sim.tick(2)
    sim.report_raw(OPEN, OFF)
    sim.tick(length)
    sim.report_raw(OPEN, ON)
    assert sim.controller.goal is None
    sim.tick(60)
    assert sim.log == [(0, OPEN, ON)]
    assert (sim.position, sim.shown) == (100, Direction.OPENING)


def test_quick_wall_reverse_and_stop_during_our_order() -> None:
    """Closing, the wall opens; we ask for 70 and the wall stops 0.2 s later: a stop."""
    sim = Sim(interlock=True, position=50)
    sim.move(10)
    sim.tick(2)
    sim.press(OPEN, ON)
    sim.tick(0.2)
    sim.move(70)
    sim.tick(0.2)
    sim.press(OPEN, OFF)
    sim.tick(60)
    assert not [c for c in sim.commands_since(2.1) if c[2] is ON]


def test_order_between_an_off_and_an_on_of_the_running_relay() -> None:
    """Close shows off at 2, we ask for 70 at 2.2, close shows on at 2.4: a press, to 0.

    The order to 70 waits the dead time and is then given up by the press: with
    interlock open never goes on while close is on.
    """
    sim = Sim(interlock=True, position=50)
    sim.move(10)
    sim.tick(2)
    sim.report_raw(CLOSE, OFF)
    sim.tick(0.2)
    sim.move(70)
    sim.tick(0.2)
    sim.report_raw(CLOSE, ON)
    sim.tick(60)
    assert sim.position == 0
    assert not any(c[1] is OPEN for c in sim.log)


def test_quick_press_and_release_of_the_other_relay_is_a_stop() -> None:
    """Opening to 80; at the wall close is pressed at 4 and released 0.4 s later.

    Our open goes off and is not switched on again. Without interlock, see the next test.
    """
    sim = Sim(interlock=True, position=20)
    sim.move(80)
    sim.tick(4)
    sim.press(CLOSE, ON)
    sim.tick(0.4)
    sim.press(CLOSE, OFF)
    sim.tick(30)
    assert sim.commands_since(4) == [(4, OPEN, OFF)]
    assert sim.shown is None


@pytest.mark.parametrize("held", [0.25, 0.4, 0.5, 3])
@pytest.mark.parametrize("seen", ["press", "read_glitch"])
def test_wall_relay_is_never_commanded_without_interlock(
    held: float, seen: str
) -> None:
    """Without interlock, close on and off from outside while opening to 80.

    No relay gets a command: our open is left on, with no target, up to the end.
    """
    sim = Sim(interlock=False, position=20)
    sim.move(80)
    sim.tick(4)
    switch = sim.press if seen == "press" else sim.report_raw
    switch(CLOSE, ON)
    sim.tick(held)
    switch(CLOSE, OFF)
    sim.tick(60)
    assert sim.log == [(0, OPEN, ON)]
    assert (sim.position, sim.shown) == (100, Direction.OPENING)


def test_failed_off_of_ours_with_a_wall_relay_on() -> None:
    """Opening, the wall closes and our off of open is lost: it goes again 5 s later.

    A call that raises is the same case. The wall's close is left on.
    """
    sim = Sim(interlock=True, position=20)
    sim.move(80)
    sim.tick(4)
    sim.confirm_delay = None
    sim.press(CLOSE, ON)
    assert sim.commands_since(4) == [(4, OPEN, OFF)]
    assert sim.controller.next_deadline(sim.now) == 4 + CONFIRM_TIMEOUT
    sim.tick(CONFIRM_TIMEOUT)
    assert sim.commands_since(4) == [(4, OPEN, OFF), (4 + CONFIRM_TIMEOUT, OPEN, OFF)]
    assert not any(c[1] is CLOSE for c in sim.commands_since(4))


def test_failed_off_of_ours_with_a_wall_relay_on_without_interlock() -> None:
    """The wall holds close; we open, then close to 10, and our off of open is lost.

    The wall's close is left on and the move to 10 goes on; only open is sent again,
    5 s later.
    """
    sim = Sim(interlock=False, position=50)
    sim.press(CLOSE, ON)
    sim.move(80)
    assert sim.log == [(0, OPEN, ON)]
    sim.tick(1)
    sim.confirm_delay = None
    sim.move(10)
    assert sim.commands_since(1) == [(1, OPEN, OFF)]
    assert sim.shown is Direction.CLOSING
    goal = sim.controller.goal
    assert isinstance(goal, Move)
    assert goal.target == 10
    sim.tick(CONFIRM_TIMEOUT)
    assert sim.commands_since(1) == [(1, OPEN, OFF), (1 + CONFIRM_TIMEOUT, OPEN, OFF)]


def test_open_again_with_an_unknown_position_keeps_the_travel() -> None:
    """Unknown position, open at 0 and again at 10: open goes off at 20, not at 30."""
    sim = Sim(interlock=True, position=None)
    sim.open()
    sim.tick(10)
    sim.open()
    sim.tick(40)
    assert sim.commands_since(0.1) == [(20, OPEN, OFF)]
    assert sim.position == 100


@pytest.mark.parametrize("interlock", [True, False])
def test_open_while_a_wall_button_holds_open_at_the_top(interlock: bool) -> None:
    """At 100 the wall holds open; open from Home Assistant changes nothing."""
    sim = Sim(interlock=interlock, position=100)
    sim.press(OPEN, ON)
    sim.tick(5)
    sim.open()
    sim.tick(5)
    assert sim.log == []
    assert sim.shown is Direction.OPENING
    sim.stop()
    assert sim.log == [(10, OPEN, OFF)]


@pytest.mark.parametrize("seed", range(300))
def test_interlock_never_broken(seed: int) -> None:
    """Random commands, presses, outages and report delays keep the interlock.

    The simulated relays switch at once, so even unconfirmed commands are real.
    """
    rng = random.Random(seed)
    sim = Sim(interlock=True, position=rng.choice([None, 0, 50, 100]))
    sim.confirm_delay = rng.choice([0, 0.2, 3, 6, None])
    for _ in range(40):
        match rng.randrange(7):
            case 0:
                with contextlib.suppress(RelayUnavailableError):
                    sim.open()
            case 1:
                with contextlib.suppress(RelayUnavailableError):
                    sim.close()
            case 2:
                sim.stop()
            case 3:
                if sim.position is not None:
                    with contextlib.suppress(RelayUnavailableError):
                        sim.move(rng.randrange(101))
            case 4:
                sim.press(rng.choice([OPEN, CLOSE]), rng.choice([ON, OFF]))
            case 5:
                relay = rng.choice([OPEN, CLOSE])
                available = sim.controller.relays[relay].available
                sim.report_raw(relay, None if available else sim.physical[relay])
            case _:
                pass
        sim.tick(rng.choice([0, 0.1, 0.5, 1, 3, 15]))
        position = sim.controller.calc.position(sim.now)
        assert position is None or 0 <= position <= 100
    assert sim.violations == []


def test_lost_off_is_sent_again_and_the_position_follows_ha() -> None:
    """Our off at 6 is lost (HA keeps showing on): it goes out again at 11.

    The cover shows opening while HA shows on: the motor ran 5 s more (50 to 75).
    """
    sim = Sim(interlock=True, position=20)
    sim.move(80)
    sim.tick(6)
    sim.confirm_delay = None
    sim.stop()
    assert sim.controller.next_deadline(sim.now) == 6 + CONFIRM_TIMEOUT
    sim.confirm_delay = 0
    sim.tick(CONFIRM_TIMEOUT - 1)
    assert (sim.position, sim.shown) == (70, Direction.OPENING)
    sim.tick(1)
    assert sim.commands_since(6) == [(6, OPEN, OFF), (6 + CONFIRM_TIMEOUT, OPEN, OFF)]
    assert (sim.position, sim.shown) == (75, None)
    assert sim.physical == {OPEN: OFF, CLOSE: OFF}


def test_lost_on_is_sent_again_and_the_move_goes_on() -> None:
    """Our on is lost: the position stays at 0, and at 5 s the on goes again."""
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.move(80)
    sim.confirm_delay = 0
    sim.tick(CONFIRM_TIMEOUT - 0.1)
    assert (sim.position, sim.shown) == (0, Direction.OPENING)
    sim.tick(0.1)
    assert sim.log == [(0, OPEN, ON), (CONFIRM_TIMEOUT, OPEN, ON)]
    sim.tick(16)
    assert sim.log[-1] == (CONFIRM_TIMEOUT + 16, OPEN, OFF)
    assert sim.position == 80


@pytest.mark.parametrize("interlock", [True, False])
def test_lost_on_is_sent_once_more_then_the_order_is_given_up(
    interlock: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A relay that loses every on: one resend at 5 s, the order given up at 10 s.

    The position never moves and the cover is left stopped: no more ons.
    """
    sim = Sim(interlock=interlock, position=0, confirm_delay=None)
    sim.move(30)
    sim.tick(2 * CONFIRM_TIMEOUT - 0.1)
    assert isinstance(sim.controller.goal, Move)
    assert sim.log == [(0, OPEN, ON), (CONFIRM_TIMEOUT, OPEN, ON)]
    sim.tick(0.1)
    assert sim.controller.goal == Stop(
        frozenset((OPEN, CLOSE) if interlock else (OPEN,))
    )
    assert (sim.position, sim.shown) == (0, None)
    assert "the order is given up" in caplog.text
    sim.tick(3600)
    assert sim.log == [(0, OPEN, ON), (CONFIRM_TIMEOUT, OPEN, ON)]
    assert sim.controller.next_deadline(sim.now) is None


@pytest.mark.parametrize("interlock", [True, False])
def test_relay_back_after_the_order_was_given_up_starts_nothing(
    interlock: bool,
) -> None:
    """The relay of the order drops out; it is back showing off an hour later.

    The order was given up at 10 s: nothing is sent on the return.
    """
    sim = Sim(interlock=interlock, position=50, confirm_delay=None)
    sim.open()
    sim.tick(1)
    sim.report_raw(OPEN, None)
    sim.tick(2 * CONFIRM_TIMEOUT)
    assert isinstance(sim.controller.goal, Stop)
    sim.tick(3600)
    sim.confirm_delay = 0
    sim.report_raw(OPEN, OFF)
    sim.tick(60)
    assert sim.log == [(0, OPEN, ON)]
    assert (sim.position, sim.shown) == (50, None)
    assert sim.physical == {OPEN: OFF, CLOSE: OFF}


@pytest.mark.parametrize("interlock", [True, False])
def test_on_shown_in_time_after_the_resend_keeps_the_order(interlock: bool) -> None:
    """The first on is lost, the resend is shown 3 s later: the move goes on."""
    sim = Sim(interlock=interlock, position=0, confirm_delay=None)
    sim.move(80)
    sim.confirm_delay = 3
    sim.tick(CONFIRM_TIMEOUT)
    assert sim.log == [(0, OPEN, ON), (CONFIRM_TIMEOUT, OPEN, ON)]
    sim.tick(3)
    assert (sim.position, sim.shown) == (0, Direction.OPENING)
    sim.tick(2 * CONFIRM_TIMEOUT)
    assert isinstance(sim.controller.goal, Move)
    sim.tick(60)
    assert sim.log[-1] == (CONFIRM_TIMEOUT + 3 + 16, OPEN, OFF)
    assert sim.physical == {OPEN: OFF, CLOSE: OFF}


def test_lost_off_is_sent_again_past_twice_the_timeout() -> None:
    """The off is not given up: it goes out every 5 s, also after 10 s."""
    sim = Sim(interlock=True, position=0)
    sim.press(OPEN, ON)
    sim.tick(2)
    sim.confirm_delay = None
    sim.stop()
    sim.tick(4 * CONFIRM_TIMEOUT)
    offs = [(2 + k * CONFIRM_TIMEOUT, OPEN, OFF) for k in range(5)]
    assert sim.commands_since(2) == offs
    assert sim.controller.goal == Stop(frozenset((OPEN, CLOSE)))
    sim.confirm_delay = 0
    sim.tick(CONFIRM_TIMEOUT)
    assert sim.physical == {OPEN: OFF, CLOSE: OFF}
    assert sim.shown is None


def test_late_report_after_a_lost_command_sends_it_again() -> None:
    """Late echo of our on at 3 s, our off of 1 s lost: the off goes again at 11 s.

    Not followed as a wall button: the last order of the user wins.
    """
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.open()
    sim.tick(1)
    sim.stop()
    sim.tick(2)
    sim.report_raw(OPEN, ON)
    sim.tick(CONFIRM_TIMEOUT)
    assert sim.commands_since(1.5) == [(1 + CONFIRM_TIMEOUT, OPEN, OFF)]
    assert isinstance(sim.controller.goal, Stop)


def test_stop_again_does_not_repeat_an_off_not_shown() -> None:
    """HA still shows on after our off: a new stop does not send it again.

    Nor does a change shown by HA; it goes out 5 s after it was sent.
    """
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.press(OPEN, ON)
    sim.tick(2)
    sim.stop()
    sim.tick(1)
    sim.stop()
    sim.press(CLOSE, ON)
    assert sim.commands_since(0) == [(2, OPEN, OFF)]
    assert sim.controller.next_deadline(sim.now) == 2 + CONFIRM_TIMEOUT


def test_command_still_wanted_after_a_hung_call_is_due_at_once() -> None:
    """A call that hangs for 10 s: its command is due again when the call gives up.

    The caller sends at most one command per relay at a time; `next_deadline` says that
    the next one is due now.
    """
    sim = Sim(interlock=True, position=0)
    controller = sim.controller
    sim.press(OPEN, ON)
    controller.request_stop(0)
    assert controller.next_command(0) == (OPEN, OFF)
    controller.command_sent(OPEN, OFF, 0)
    assert controller.next_command(0) is None
    assert controller.next_deadline(0) == CONFIRM_TIMEOUT
    assert controller.next_command(CONFIRM_TIMEOUT) == (OPEN, OFF)
    assert controller.next_deadline(CONFIRM_TIMEOUT) == CONFIRM_TIMEOUT


def test_pending_off_survives_a_change_from_outside() -> None:
    """An off we sent and HA does not show yet is still wanted after a press of the other.

    Our close is switched off by a stop and its off is lost; the wall then switches open
    on: the off of close goes again 5 s after it was sent, also without interlock.
    """
    for interlock in (True, False):
        sim = Sim(interlock=interlock, position=50)
        sim.close()
        sim.tick(1)
        sim.confirm_delay = None
        sim.stop()
        sim.confirm_delay = 0
        sim.tick(1)
        sim.press(OPEN, ON)
        assert sim.controller.goal == Stop(frozenset((CLOSE,)))
        sim.tick(CONFIRM_TIMEOUT)
        assert sim.commands_since(1) == [
            (1, CLOSE, OFF),
            (1 + CONFIRM_TIMEOUT, CLOSE, OFF),
        ]
        assert sim.physical == {OPEN: ON, CLOSE: OFF}


def test_same_state_again_is_no_report() -> None:
    """A state equal to the last one shown changes nothing, even with a command pending."""
    sim = Sim(interlock=True, position=0, confirm_delay=None)
    sim.open()
    sim.tick(1)
    sim.stop()
    sim.controller.relay_reported(OPEN, OFF, sim.now)
    assert sim.shown is None
    sim.tick(30)
    assert sim.commands_since(0) == [(0, OPEN, ON), (1, OPEN, OFF)]


@pytest.mark.parametrize("confirm_delay", [0, 0.3])
def test_read_glitch_of_the_idle_relay(confirm_delay: float) -> None:
    """While opening, HA shows close on for 0.25 s without it moving: a stop.

    Only the open relay goes off, and it stays off when close shows off again: on and off
    of the idle relay is taken as a press and a release at the wall, so the position
    counts 0.25 s of closing. Nothing more is sent. Without interlock nothing is sent at
    all (`test_wall_relay_is_never_commanded_without_interlock`).
    """
    sim = Sim(interlock=True, position=20, confirm_delay=confirm_delay)
    sim.move(80)
    sim.tick(3)
    sim.report_raw(CLOSE, ON)
    sim.tick(0.25)
    sim.report_raw(CLOSE, OFF)
    sim.tick(30)
    assert sim.log == [(0, OPEN, ON), (3, OPEN, OFF)]
    assert sim.shown is None
    assert sim.position == 34
