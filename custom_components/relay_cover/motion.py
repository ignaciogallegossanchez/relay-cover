"""Movement of a cover driven by an open relay and a close relay (no Home Assistant imports).

Every input carries `now`, a monotonic time in seconds. The controller never talks to the
relays: the caller asks `next_command`, reports it with `command_sent` and sends it, and
passes on with `relay_reported` what Home Assistant shows of each relay when it changes.

What Home Assistant shows is the only truth about a relay: the position starts and stops
with it. Of each relay only the last command sent is kept: it is not repeated while Home
Assistant does not show it, and goes out again if Home Assistant still does not show it
`CONFIRM_TIMEOUT` after it was sent. A command lost on the way and a call that raised are
the same case.

The goal holds only orders from Home Assistant: move, switch some relays off, or nothing.
A change from outside gives the goal up and the position follows the relays; with
interlock, a relay switched on while the other one is on makes the older one go off.
"""

from dataclasses import dataclass
from enum import StrEnum
import logging
import math

from .travel import CLOSED, OPEN, Direction, TravelCalculator

_LOGGER = logging.getLogger(__name__)

# Pause between one relay going off and the other one coming on, with interlock
# (Tasmota's default ShutterMotorStop is 500 ms for the same purpose)
DEAD_TIME = 0.5
# Longest time a relay may take to carry out and report a command. After it a command not
# shown goes out again; an order whose relay is still not shown on after twice this time
# (one resend) is given up
CONFIRM_TIMEOUT = 5.0
# With interlock, longest wait for Home Assistant to show off the relay we turned off before
# turning the other one on; after it the reversal is given up and the cover stays stopped
REVERSAL_TIMEOUT = 2.0


class Relay(StrEnum):
    """The two relays of a cover."""

    OPEN = "open"
    CLOSE = "close"

    @property
    def other(self) -> Relay:
        """Return the opposite relay."""
        return Relay.CLOSE if self is Relay.OPEN else Relay.OPEN

    @property
    def direction(self) -> Direction:
        """Return the direction this relay moves the cover in."""
        return Direction.OPENING if self is Relay.OPEN else Direction.CLOSING

    @classmethod
    def for_direction(cls, direction: Direction) -> Relay:
        """Return the relay that moves the cover in a direction."""
        return cls.OPEN if direction is Direction.OPENING else cls.CLOSE


class PositionUnknownError(Exception):
    """A position in the middle was requested while the position is unknown."""


class RelayUnavailableError(Exception):
    """A movement was requested that needs a relay that is unavailable."""


@dataclass(frozen=True, slots=True)
class Command:
    """A command sent to a relay."""

    value: bool
    sent_at: float


@dataclass(frozen=True, slots=True)
class Move:
    """An order to go to a target: the relay of its direction goes on."""

    direction: Direction
    target: float
    # The relay of the other direction goes off too
    other_off: bool

    @property
    def relay(self) -> Relay:
        """Return the relay that moves the cover in the direction of the order."""
        return Relay.for_direction(self.direction)

    @property
    def relays(self) -> frozenset[Relay]:
        """Return the relays the order switches."""
        if self.other_off:
            return frozenset(Relay)
        return frozenset((self.relay,))


@dataclass(frozen=True, slots=True)
class Stop:
    """Relays to switch off: a stop, the arrival of a move, or the interlock."""

    relays: frozenset[Relay]


# None: no relay is switched
type Goal = Move | Stop | None


class RelayState:
    """What Home Assistant shows of a relay and the last command sent to it."""

    def __init__(self) -> None:
        """Start with an unknown, unavailable relay."""
        self.available = False
        self.shown: bool | None = None
        self.on_since = -math.inf
        # Last off: sent by us or shown by Home Assistant
        self.off_since = -math.inf
        # Last command sent, until Home Assistant shows a change
        self.command: Command | None = None

    def pending(self, now: float) -> Command | None:
        """Return the last command while Home Assistant may still show it."""
        command = self.command
        if command is None or now >= command.sent_at + CONFIRM_TIMEOUT:
            return None
        return command

    def expected(self, now: float) -> bool | None:
        """Return the value the relay is on its way to: the pending command or the shown one."""
        pending = self.pending(now)
        return self.shown if pending is None else pending.value

    def may_be_on(self, now: float) -> bool:
        """Return True when the relay is shown on or an on we sent is pending."""
        return bool(self.shown) or bool(self.expected(now))

    def switching_off(self, now: float) -> bool:
        """Return True while an off we sent is not shown yet."""
        pending = self.pending(now)
        return pending is not None and not pending.value

    def sent(self, value: bool, now: float) -> None:
        """Record a command just sent."""
        self.command = Command(value, now)
        if not value:
            self.off_since = now

    def report(self, value: bool, now: float) -> bool:
        """Take a change shown by Home Assistant; return True if it came from outside.

        While a command is pending a different value is taken as an earlier command shown
        late, not as a change from outside: the pending one goes out again if it is not
        shown in time.
        """
        pending = self.pending(now)
        if value and not self.shown:
            self.on_since = now
        elif self.shown and not value:
            self.off_since = max(self.off_since, now)
        self.available = True
        self.shown = value
        if pending is None or pending.value == value:
            self.command = None
        return pending is None


class MotionController:
    """State machine of a cover with two maintained relays and a position by time."""

    def __init__(
        self,
        name: str,
        opening_time: float,
        closing_time: float,
        interlock: bool,
        end_margin: float,
    ) -> None:
        """Initialize with the travel times and the options; the position is unknown.

        `name` tells the covers apart in the log. `end_margin` is the extra run time of an
        order to an end, as a fraction of the full travel in that direction.
        """
        self.name = name
        self.interlock = interlock
        self.end_margin = end_margin
        self.calc = TravelCalculator(opening_time, closing_time, None)
        self.relays = {relay: RelayState() for relay in Relay}
        self.goal: Goal = None
        self.goal_since = -math.inf

    @property
    def available(self) -> bool:
        """Return True while some relay can be commanded, so that it can be stopped."""
        return any(state.available for state in self.relays.values())

    def position(self, now: float) -> int | None:
        """Return the position to show, 0 closed to 100 open."""
        position = self.calc.position(now)
        return None if position is None else round(position)

    def shown_direction(self) -> Direction | None:
        """Return the direction to show: the motor's, or that of an order waiting to start."""
        if self.calc.direction is not None:
            return self.calc.direction
        return self.goal.direction if isinstance(self.goal, Move) else None

    def restore_position(self, position: float | None) -> None:
        """Set the position saved before a restart."""
        self.calc.set_position(position)

    def request_move(self, target: float, now: float) -> None:
        """Ask the cover to go to a position; asking for the current one stops it.

        An end is run to with the margin at the ends. Asking for the end the cover stands
        still at does nothing. Without margin, asking for the end the cover is at stops it,
        unless it is running to that end.
        """
        self.advance(now)
        current = self.calc.position(now)
        if current == target and self.shown_direction() is None:
            return
        if target in (OPEN, CLOSED) and (
            self.end_margin > 0 or current is None or round(current) != target
        ):
            direction = Direction.OPENING if target == OPEN else Direction.CLOSING
        elif current is None:
            raise PositionUnknownError
        elif round(current) == target:
            shown = self.shown_direction()
            if shown is None or shown.end != target:
                self.request_stop(now)
            return
        else:
            direction = Direction.OPENING if target > current else Direction.CLOSING
        if not self._reachable(direction):
            raise RelayUnavailableError
        other = Relay.for_direction(direction).other
        # Without interlock only a relay our goal switches is ours to switch off
        other_off = self.interlock or other in self._goal_relays()
        self._set_goal(Move(direction, target, other_off), now)
        # Without interlock, with both relays on, the order says which one is followed
        self._sync_motion(now)

    def request_stop(self, now: float) -> None:
        """Ask the cover to stop: both relays go off, also one switched on from outside."""
        self.advance(now)
        self._set_goal(Stop(frozenset(Relay)), now)

    def relay_reported(self, relay: Relay, value: bool | None, now: float) -> None:
        """Take what Home Assistant shows of a relay; None means unavailable or unknown."""
        self.advance(now)
        state = self.relays[relay]
        if value is None:
            # The last value shown is kept: the motor is taken to go on as it was
            state.available = False
            return
        if value == state.shown:
            # Back from unavailable, or the entity re-added after a rename: no change
            state.available = True
            return
        if state.report(value, now):
            self._changed_from_outside(relay, value, now)
        self._sync_motion(now)

    def command_sent(self, relay: Relay, value: bool, now: float) -> None:
        """Record a command the caller is sending now."""
        self.relays[relay].sent(value, now)

    def advance(self, now: float) -> None:
        """Catch up with time: give up a move that cannot happen, or stop at the target."""
        move = self.goal
        if not isinstance(move, Move):
            return
        if _due(self._reversal_give_up_at(), now):
            _LOGGER.warning(
                "%s: a relay still reports on after being turned off; not reversing",
                self.name,
            )
        elif _due(self._order_expires_at(), now):
            _LOGGER.warning(
                "%s: the relay of the order does not turn on; the order is given up",
                self.name,
            )
        elif not _due(self.stop_at, now):
            return
        # Given up or arrived: the relays of the order go off
        self._set_goal(Stop(move.relays), now)

    def next_command(self, now: float) -> tuple[Relay, bool] | None:
        """Return the next relay command to send, turning off before turning on."""
        desired = self._desired(now)
        for value in (False, True):
            for relay, state in self.relays.items():
                if (
                    desired[relay] is value
                    and state.available
                    and state.expected(now) is not value
                ):
                    return relay, value
        return None

    def next_deadline(self, now: float) -> float | None:
        """Return when `advance` or `next_command` has something new to do."""
        if self.next_command(now) is not None:
            # Left by the caller for later, after a call that hung
            return now
        # Applied by `advance`: one already due (it became due after the last call) is due now
        due = [self.stop_at, self._reversal_give_up_at(), self._order_expires_at()]
        deadlines = [max(d, now) for d in due if d is not None]
        # A command not shown in time goes out again
        deadlines += [
            pending.sent_at + CONFIRM_TIMEOUT
            for state in self.relays.values()
            if (pending := state.pending(now)) is not None
        ]
        move = self.goal
        if self.interlock and isinstance(move, Move):
            # End of the dead time; only worth waking up for while still ahead
            wake = self.relays[move.relay.other].off_since + DEAD_TIME
            if now < wake:
                deadlines.append(wake)
        return min(deadlines, default=None)

    @property
    def stop_at(self) -> float | None:
        """Return when the movement under way reaches the target of the order.

        An end gets the margin at the ends on top, also after a full travel: the motor may
        start late or run slower than measured.
        """
        move, started_at = self.goal, self.calc.changed_at
        if not isinstance(move, Move) or self.calc.direction is not move.direction:
            return None
        run = self.calc.time_to(move.target, move.direction, started_at)
        if move.target == move.direction.end:
            run += self.end_margin * self.calc.travel_time(move.direction)
        return started_at + run

    def _reversal_give_up_at(self) -> float | None:
        """Return when to give up waiting for the other relay to be shown off."""
        move = self.goal
        if not self.interlock or not isinstance(move, Move):
            return None
        this, other = self.relays[move.relay], self.relays[move.relay.other]
        command = other.command
        if this.shown or not other.shown or command is None or command.value:
            return None
        # Each new order waits again, so a second try is never left without a timer
        return max(self.goal_since, command.sent_at) + REVERSAL_TIMEOUT

    def _order_expires_at(self) -> float | None:
        """Return when to give up an order whose relay is not shown on: one resend at most.

        Never left waiting: a relay that answers again much later must not start on its own.
        """
        move = self.goal
        if not isinstance(move, Move) or self.relays[move.relay].shown:
            return None
        return self.goal_since + 2 * CONFIRM_TIMEOUT

    def _reachable(self, direction: Direction) -> bool:
        """Return True when the relays needed to move in a direction can be commanded."""
        relay = Relay.for_direction(direction)
        this, other = self.relays[relay], self.relays[relay.other]
        if not this.available:
            return False
        # Without interlock the other relay is not looked at
        return other.available or not self.interlock

    def _goal_relays(self) -> frozenset[Relay]:
        match self.goal:
            case Move() | Stop() as goal:
                return goal.relays
            case None:
                return frozenset()

    def _set_goal(self, goal: Goal, now: float) -> None:
        self.goal = goal
        self.goal_since = now

    def _changed_from_outside(self, relay: Relay, value: bool, now: float) -> None:
        """Give up the goal after a change from outside; the relays are left as they are.

        With interlock, a relay switched on while the other one may be on makes the other
        one go off, ours or not. An off we sent to the other relay and Home Assistant does
        not show yet is still wanted. A relay going off while the move under way runs on
        the other one leaves the move alone.
        """
        other = self.relays[relay.other]
        if other.switching_off(now) or (
            value and self.interlock and other.may_be_on(now)
        ):
            self._set_goal(Stop(frozenset((relay.other,))), now)
        elif value or not self._move_runs_on_other_than(relay):
            self._set_goal(None, now)

    def _move_runs_on_other_than(self, relay: Relay) -> bool:
        """Return True when the move under way runs on a relay other than this one."""
        move = self.goal
        return (
            isinstance(move, Move)
            and move.relay is not relay
            and bool(self.relays[move.relay].shown)
        )

    def _motor(self) -> Direction | None:
        on = [relay for relay, state in self.relays.items() if state.shown]
        match on:
            case [relay]:
                return relay.direction
            case [_, _] if not self.interlock:
                # Both may stay on: the relay of our order, else the one switched on last
                if isinstance(self.goal, Move):
                    return self.goal.direction
                return max(on, key=lambda relay: self.relays[relay].on_since).direction
            case _:
                return None

    def _sync_motion(self, now: float) -> None:
        motor = self._motor()
        if motor is self.calc.direction:
            return
        self.calc.stop(now)
        if motor is not None:
            self.calc.start(motor, now)

    def _desired(self, now: float) -> dict[Relay, bool | None]:
        """Return what each relay should be; None leaves it as it is."""
        match self.goal:
            case None:
                return dict.fromkeys(Relay)
            case Stop(relays=relays):
                return {relay: False if relay in relays else None for relay in Relay}
            case Move() as move:
                return {
                    move.relay: self._run_value(move.relay, now),
                    move.relay.other: False if move.other_off else None,
                }

    def _run_value(self, relay: Relay, now: float) -> bool | None:
        """Return what the relay of a move should be: on, or None to wait."""
        if not self.interlock:
            return True
        other = self.relays[relay.other]
        # Never on together, and the dead time after the other went off
        if (
            other.may_be_on(now)
            or not other.available
            or now < other.off_since + DEAD_TIME
        ):
            return None
        return True


def _due(at: float | None, now: float) -> bool:
    return at is not None and now >= at
