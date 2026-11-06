"""Random sequences against the movement controller with six relay models.

Each relay has a physical state and a state in Home Assistant; the controller only hears
of a relay when its state in Home Assistant changes, and only gets `advance` at the
deadlines it asks for, like the entity.

Relay models:
- immediate: carries out and reports at once;
- late_report: carries out at once, reports 0.3 or 2 s later (normal relays report in
  tenths of a second; later reports are a limit of the README);
- late_execution: carries out (and reports) 0.05, 0.3 or 2 s later, in order;
- polling: carries out at once, Home Assistant reads it every 5 s;
- optimistic: carries out at once; Home Assistant shows our commands at once, as the
  integration writes them, and never sees a wall press;
- lost: like immediate, but one call in five is lost (nothing happens, Home Assistant
  shows no change) and one in ten raises (nothing happens either; the entity logs it).
"""

import argparse
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
import heapq
import itertools
import logging
import random

from custom_components.relay_cover.motion import (
    DEAD_TIME,
    MotionController,
    Move,
    PositionUnknownError,
    Relay,
    RelayUnavailableError,
    Stop,
)

EPS = 1e-6
STEPS_PER_RUN = 40
SETTLE_TIME = 300.0


class Model(StrEnum):
    """How a simulated relay carries out and reports commands."""

    IMMEDIATE = "immediate"
    LATE_REPORT = "late_report"
    LATE_EXECUTION = "late_execution"
    POLLING = "polling"
    OPTIMISTIC = "optimistic"
    LOST = "lost"


class Call(StrEnum):
    """What became of a relay call in the lost model."""

    DONE = "done"
    LOST = "lost"
    RAISED = "raised"


class Family(StrEnum):
    """Kind of random sequence."""

    # All models, with wall presses, outages and report glitches
    INTERLOCK = "interlock"
    # Immediate relays without presses or glitches: position and target are checked
    POSITION = "position"


type Event = tuple[float, int, str, tuple[object, ...]]


@dataclass
class Device:
    """A relay: what it really does and what Home Assistant shows."""

    physical: bool = False
    ha: bool | None = False
    off_at: float = -1e9
    # The last physical change came from a press Home Assistant has not seen
    unseen_press: bool = False
    last_press_at: float = -1e9
    last_on_sent_at: float = -1e9
    # The last physical change was a press to the value HA already showed: no event
    masked_press: bool = False
    # Last value HA showed other than unavailable: what the controller knows
    last_seen: bool = False
    # The relay carries out its commands in the order received
    busy_until: float = 0.0


@dataclass
class Run:
    """One random sequence and what went wrong in it."""

    rng: random.Random
    interlock: bool
    model: Model
    opening_time: float
    closing_time: float
    end_margin: float
    position: float | None
    now: float = 0.0
    queue: list[Event] = field(default_factory=list)
    seq: itertools.count[int] = field(default_factory=itertools.count)
    # Our on with the other relay physically on or off for less than the dead time
    violations: list[tuple[float, str]] = field(default_factory=list)
    # Same, but the other relay was moved by a press nobody could see, or after our order
    blameless: list[tuple[float, str]] = field(default_factory=list)
    # Same, but a press on the same relay between our order and its execution
    masked: list[tuple[float, str]] = field(default_factory=list)
    position_errors: list[tuple[float, float, float]] = field(default_factory=list)
    target_errors: list[tuple[float, float, bool]] = field(default_factory=list)
    no_timer: list[tuple[float, str]] = field(default_factory=list)
    unfollowed: list[tuple[str, ...]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Start with both relays off and reported."""
        self.ctl = MotionController(
            "test",
            self.opening_time,
            self.closing_time,
            self.interlock,
            self.end_margin,
        )
        self.ctl.restore_position(self.position)
        self.dev = {relay: Device() for relay in Relay}
        self.physical_position = self.position
        for relay in Relay:
            self.ctl.relay_reported(relay, False, self.now)

    # Relays and Home Assistant

    def _schedule(self, at: float, kind: str, *args: object) -> None:
        heapq.heappush(self.queue, (at, next(self.seq), kind, args))

    def _ha_set(self, relay: Relay, value: bool | None) -> None:
        dev = self.dev[relay]
        if dev.ha == value:
            # Home Assistant fires no state_changed for the same value
            return
        dev.ha = value
        # Coming back from unavailable with the value of before reveals no press in between
        if value is not None and value == dev.physical and value != dev.last_seen:
            dev.unseen_press = False
            dev.masked_press = False
        if value is not None:
            dev.last_seen = value
        self.ctl.relay_reported(relay, value, self.now)
        self.drive()

    def _set_physical(
        self, relay: Relay, value: bool, ours: bool, sent_at: float | None = None
    ) -> None:
        dev = self.dev[relay]
        if value and not dev.physical and ours and self.interlock:
            other = self.dev[relay.other]
            too_soon = other.physical or self.now < other.off_at + DEAD_TIME - EPS
            if too_soon:
                sent = self.now if sent_at is None else sent_at
                item = (round(self.now, 3), relay.value)
                if other.unseen_press or other.last_press_at > sent - EPS:
                    self.blameless.append(item)
                elif dev.last_press_at > sent - EPS:
                    self.masked.append(item)
                else:
                    self.violations.append(item)
        if dev.physical and not value:
            dev.off_at = self.now
        dev.physical = value
        dev.unseen_press = not ours
        if ours:
            # A command of ours carried out later leaves the relay as the controller thinks
            dev.masked_press = False
        else:
            dev.last_press_at = self.now

    def send(self, relay: Relay, value: bool) -> Call:
        """The entity sends a command to a relay."""
        call = Call.DONE
        if self.model is Model.LOST:
            roll = self.rng.random()
            call = Call.LOST if roll < 0.2 else Call.RAISED if roll < 0.3 else call
        match self.model:
            case Model.IMMEDIATE | Model.LOST if call is Call.DONE:
                self._set_physical(relay, value, ours=True)
                self._ha_set(relay, value)
            case Model.LATE_REPORT:
                self._set_physical(relay, value, ours=True)
                self._schedule(self.now + self.rng.choice([0.3, 2]), "report", relay)
            case Model.LATE_EXECUTION:
                dev = self.dev[relay]
                at = max(dev.busy_until, self.now + self.rng.choice([0.05, 0.3, 2]))
                dev.busy_until = at
                self._schedule(at, "execute", relay, value, self.now)
            case Model.POLLING:
                self._set_physical(relay, value, ours=True)
            case Model.OPTIMISTIC:
                self._set_physical(relay, value, ours=True)
                self._ha_set(relay, value)
        return call

    def drive(self) -> None:
        """What `_async_apply` does: at most one command per relay; a raise is logged."""
        sent: set[Relay] = set()
        while (command := self.ctl.next_command(self.now)) is not None:
            relay, value = command
            if relay in sent:
                break
            sent.add(relay)
            self.ctl.command_sent(relay, value, self.now)
            if value:
                self.dev[relay].last_on_sent_at = self.now
            self.send(relay, value)
        self.check()

    def held(self) -> bool:
        """Return True while the motor runs on a relay no order of Home Assistant switches.

        Such a relay is left on, also at the end: it has no timer.
        """
        if (motor := self.ctl.calc.direction) is None:
            return False
        match self.ctl.goal:
            case Move() | Stop() as goal:
                return Relay.for_direction(motor) not in goal.relays
            case None:
                return True

    def check(self) -> None:
        """Never a cover moving or waiting without a timer (with both relays available)."""
        available = all(tracker.available for tracker in self.ctl.relays.values())
        if not available or self.ctl.next_command(self.now) is not None:
            return
        moving = self.ctl.calc.direction is not None
        if self.held():
            return
        if (
            moving
            and self.ctl.stop_at is None
            and self.ctl.next_deadline(self.now) is None
        ):
            self.no_timer.append((round(self.now, 3), "moving without a deadline"))
        elif (
            not moving
            and self.ctl.shown_direction() is not None
            and self.ctl.next_deadline(self.now) is None
        ):
            self.no_timer.append((round(self.now, 3), "direction without deadline"))

    def settle(self) -> None:
        """Relays available and 300 s without orders: what is visible ends followed."""
        for relay in Relay:
            if self.dev[relay].ha is None:
                self._ha_set(relay, self.dev[relay].physical)
        self.run_until(self.now + SETTLE_TIME)
        if self.held():
            # Left on until the person switches it off: switch it off from outside
            for relay in Relay:
                if self.dev[relay].physical:
                    self.press(relay, False)
            self.run_until(self.now + SETTLE_TIME)
        for relay in Relay:
            dev = self.dev[relay]
            if dev.masked_press or dev.ha != dev.physical:
                continue
            shown = self.ctl.relays[relay].shown
            if dev.physical or shown is not False:
                self.unfollowed.append(
                    (relay.value, f"physical={dev.physical}", f"shown={shown}")
                )
        if (direction := self.ctl.shown_direction()) is not None:
            self.unfollowed.append(("cover", f"shows {direction}"))

    # Clock

    def run_until(self, end: float) -> None:
        """Move the clock to `end`, firing queued events and controller deadlines."""
        same_time = 0
        while True:
            deadline = self.ctl.next_deadline(self.now)
            next_event = self.queue[0][0] if self.queue else None
            candidates = [
                t for t in (deadline, next_event) if t is not None and t <= end
            ]
            if not candidates:
                break
            at = min(candidates)
            same_time = same_time + 1 if at == self.now else 0
            if same_time > 50:
                self.no_timer.append((round(self.now, 3), "deadline never resolved"))
                break
            self._advance_physical(at)
            if next_event is not None and next_event == at:
                _, _, kind, args = heapq.heappop(self.queue)
                self._event(kind, *args)
            if deadline is not None and deadline <= at:
                # Like the entity timer: it fires even with an event at the same time
                self.ctl.advance(self.now)
                self.drive()
            self.check()
        self._advance_physical(end)

    def _event(self, kind: str, *args: object) -> None:
        match kind:
            case "report":
                (relay,) = args
                assert isinstance(relay, Relay)
                self._ha_set(relay, self.dev[relay].physical)
            case "execute":
                relay, value, sent_at = args
                assert isinstance(relay, Relay)
                assert isinstance(value, bool)
                assert isinstance(sent_at, float)
                self._set_physical(relay, value, ours=True, sent_at=sent_at)
                self._ha_set(relay, value)
            case "poll":
                for relay in Relay:
                    if self.dev[relay].ha is not None:
                        self._ha_set(relay, self.dev[relay].physical)
                self._schedule(self.now + 5, "poll")

    def _advance_physical(self, to: float) -> None:
        if self.physical_position is not None:
            on = [relay for relay in Relay if self.dev[relay].physical]
            if len(on) == 1:
                elapsed = to - self.now
                if on[0] is Relay.OPEN:
                    self.physical_position = min(
                        100.0,
                        self.physical_position + elapsed * 100 / self.opening_time,
                    )
                else:
                    self.physical_position = max(
                        0.0, self.physical_position - elapsed * 100 / self.closing_time
                    )
        self.now = to

    # Actions from outside

    def press(self, relay: Relay, value: bool) -> None:
        """Wall button: the relay changes physically; HA sees it unless it does not report."""
        dev = self.dev[relay]
        if value == dev.last_seen and value != dev.physical:
            dev.masked_press = True
        self._set_physical(relay, value, ours=False)
        if self.model in (Model.OPTIMISTIC, Model.POLLING):
            return
        self._ha_set(relay, value)

    def outage(self, relay: Relay) -> None:
        """The relay goes unavailable in HA, or comes back with its physical state."""
        if self.model is Model.OPTIMISTIC:
            return
        dev = self.dev[relay]
        self._ha_set(relay, None if dev.ha is not None else dev.physical)

    def bounce(self, relay: Relay) -> None:
        """The report of a relay flips and comes back at once."""
        if self.model is not Model.IMMEDIATE or self.dev[relay].ha is None:
            return
        value = self.dev[relay].physical
        self._ha_set(relay, not value)
        self._ha_set(relay, value)


def one_run(rng: random.Random, family: Family) -> Run:
    """Play one random sequence of 40 steps and let it settle."""
    position_family = family is Family.POSITION
    interlock = rng.random() < (0.5 if position_family else 0.7)
    if position_family:
        model, presses = Model.IMMEDIATE, False
        position: float | None = rng.choice([0.0, 37.0, 100.0])
    else:
        model, presses = rng.choice(list(Model)), True
        position = rng.choice([None, 0.0, 50.0, 100.0])
    run = Run(
        rng,
        interlock,
        model,
        opening_time=rng.choice([7.0, 17.0, 23.5]),
        closing_time=rng.choice([9.0, 31.0, 41.0]),
        end_margin=rng.choice([0.0, 0.2, 1.0]),
        position=position,
    )
    if model is Model.POLLING:
        run._schedule(5, "poll")  # noqa: SLF001
    for _ in range(STEPS_PER_RUN):
        # Without presses no report glitch either: a glitch is two presses
        action = rng.randrange(9 if presses else 5)
        if action <= 4 and not run.ctl.available:
            # Home Assistant skips actions on an unavailable entity
            action = -1
        target: float | None = None
        match action:
            case 0:
                target = 100.0
            case 1:
                target = 0.0
            case 2:
                run.ctl.request_stop(run.now)
                run.drive()
            case 3 | 4:
                target = float(rng.randrange(101))
            case 5 | 6:
                run.press(rng.choice(list(Relay)), rng.random() < 0.5)
            case 7:
                run.outage(rng.choice(list(Relay)))
            case 8:
                run.bounce(rng.choice(list(Relay)))
        if target is not None:
            try:
                run.ctl.request_move(target, run.now)
            except PositionUnknownError, RelayUnavailableError:
                target = None
            run.drive()
        long_wait = position_family and rng.random() < 0.4
        wait = 200.0 if long_wait else rng.choice([0, 0.1, 0.5, 1, 1.05, 3, 12])
        run.run_until(run.now + wait)
        if position_family:
            _check_position(run, target, long_wait)
    run.settle()
    return run


def _check_position(run: Run, target: float | None, long_wait: bool) -> None:
    estimated = run.ctl.calc.position(run.now)
    real = run.physical_position
    if estimated is not None and real is not None and abs(estimated - real) > EPS:
        run.position_errors.append((round(run.now, 3), estimated, real))
    if long_wait and target is not None and real is not None:
        # After a long wait without orders it must be still at the target
        still = not any(dev.physical for dev in run.dev.values())
        if not still or abs(real - target) > 0.5 + EPS:
            run.target_errors.append((target, real, still))


def run_many(master_seed: int, runs: int) -> Counter[str]:
    """Play `runs` sequences per family and count problems by kind and relay model."""
    master = random.Random(master_seed)
    totals: Counter[str] = Counter()
    for family in Family:
        for _ in range(runs):
            run = one_run(random.Random(master.randrange(2**32)), family)
            totals[f"runs/{family}/{run.model}"] += 1
            problems = {
                "violations": run.violations,
                "masked": run.masked,
                "blameless": run.blameless,
                "position_errors": run.position_errors,
                "target_errors": run.target_errors,
                "no_timer": run.no_timer,
                "unfollowed": run.unfollowed,
            }
            for name, items in problems.items():
                if items:
                    totals[f"{name}/{run.model}"] += 1
    return totals


def main() -> None:
    """Print the counts of a master seed."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--runs", type=int, default=3000)
    args = parser.parse_args()
    # The warning of a reversal given up is expected here
    logging.getLogger("custom_components.relay_cover.motion").setLevel(logging.ERROR)
    for key, count in sorted(run_many(args.seed, args.runs).items()):
        print(f"{key}: {count}")  # noqa: T201


if __name__ == "__main__":
    main()
