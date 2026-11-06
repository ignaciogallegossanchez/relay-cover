"""Relay simulator for the pure tests of the movement controller."""

import math

from custom_components.relay_cover.motion import DEAD_TIME, MotionController, Relay
from custom_components.relay_cover.travel import Direction

OPENING_TIME = 20.0
CLOSING_TIME = 30.0


class Sim:
    """Drive a MotionController against two simulated relays with a virtual clock.

    The relays switch at once; `confirm_delay` is how late Home Assistant shows it. None
    loses our commands: the relay does not switch and Home Assistant shows no change. As in
    Home Assistant, a report reaches the controller only when the relay's state there
    changes.
    """

    def __init__(
        self,
        interlock: bool,
        position: float | None = 0,
        confirm_delay: float | None = 0,
        end_margin: float = 0,
        opening_time: float = OPENING_TIME,
        closing_time: float = CLOSING_TIME,
    ) -> None:
        """Start with both relays off and reported; no margin at the ends by default."""
        self.controller = MotionController(
            "test", opening_time, closing_time, interlock, end_margin
        )
        self.controller.restore_position(position)
        self.now = 0.0
        self.confirm_delay = confirm_delay
        self.interlock = interlock
        self.physical = dict.fromkeys(Relay, False)
        self.in_ha: dict[Relay, bool | None] = dict.fromkeys(Relay, False)
        self.off_at = dict.fromkeys(Relay, -math.inf)
        # Commands of ours that broke the interlock: other relay on or off for too short
        self.violations: list[tuple[float, Relay]] = []
        self.log: list[tuple[float, Relay, bool]] = []
        self._echoes: list[tuple[float, Relay]] = []
        for relay in Relay:
            self.controller.relay_reported(relay, False, self.now)

    def drive(self) -> None:
        """Send the commands the controller wants now, as the entity does."""
        sent: set[Relay] = set()
        while (command := self.controller.next_command(self.now)) is not None:
            relay, value = command
            if relay in sent:
                break
            sent.add(relay)
            self.controller.command_sent(relay, value, self.now)
            self.log.append((self.now, relay, value))
            if self.confirm_delay is None:
                continue
            if self.interlock and value:
                other = relay.other
                if self.physical[other] or self.now < self.off_at[other] + DEAD_TIME:
                    self.violations.append((self.now, relay))
            self._set_physical(relay, value)
            if self.confirm_delay == 0:
                self._report(relay, value)
            elif self.confirm_delay is not None:
                self._echoes.append((self.now + self.confirm_delay, relay))

    def tick(self, seconds: float) -> None:
        """Let time pass, firing deadlines and late echoes in order."""
        end = self.now + seconds
        while True:
            deadline = self.controller.next_deadline(self.now)
            echo_at = min((e[0] for e in self._echoes), default=None)
            nxt = min((t for t in (deadline, echo_at) if t is not None), default=None)
            if nxt is None or nxt > end:
                break
            self.now = nxt
            due = [e for e in self._echoes if e[0] <= self.now]
            self._echoes = [e for e in self._echoes if e[0] > self.now]
            self.controller.advance(self.now)
            for _, relay in due:
                # The device reports what it is doing now, not what it was asked
                self._report(relay, self.physical[relay])
            self.drive()
        # Like the entity: the controller only hears of time at the deadlines it asked for
        self.now = end

    def press(self, relay: Relay, value: bool) -> bool:
        """Switch a relay from outside (wall button or another app).

        Return False when Home Assistant already showed that value: nothing is reported.
        """
        self._set_physical(relay, value)
        reported = self._report(relay, value)
        self.drive()
        return reported

    def report_raw(self, relay: Relay, value: bool | None) -> None:
        """Feed a report without changing the physical relay (late echo, unavailable)."""
        self.in_ha[relay] = value
        self.controller.relay_reported(relay, value, self.now)
        self.drive()

    def _set_physical(self, relay: Relay, value: bool) -> None:
        if self.physical[relay] and not value:
            self.off_at[relay] = self.now
        self.physical[relay] = value

    def _report(self, relay: Relay, value: bool) -> bool:
        if self.in_ha[relay] == value:
            # Home Assistant does not fire state_changed for the same value
            return False
        self.in_ha[relay] = value
        self.controller.relay_reported(relay, value, self.now)
        return True

    def open(self) -> None:
        """Open fully."""
        self.move(100)

    def close(self) -> None:
        """Close fully."""
        self.move(0)

    def move(self, target: int) -> None:
        """Go to a position."""
        self.controller.request_move(target, self.now)
        self.drive()

    def stop(self) -> None:
        """Stop."""
        self.controller.request_stop(self.now)
        self.drive()

    @property
    def position(self) -> int | None:
        """Return the shown position."""
        return self.controller.position(self.now)

    @property
    def shown(self) -> Direction | None:
        """Return the shown direction."""
        return self.controller.shown_direction()

    def commands_since(self, since: float) -> list[tuple[float, Relay, bool]]:
        """Return the commands sent from a moment on."""
        return [c for c in self.log if c[0] >= since]
