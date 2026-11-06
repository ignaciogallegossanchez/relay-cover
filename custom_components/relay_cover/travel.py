"""Position of a cover estimated from its travel times (no Home Assistant imports)."""

from enum import StrEnum
import math

CLOSED = 0.0
OPEN = 100.0


class Direction(StrEnum):
    """Direction in which the cover moves."""

    OPENING = "opening"
    CLOSING = "closing"

    @property
    def end(self) -> float:
        """Return the position this direction ends at."""
        return OPEN if self is Direction.OPENING else CLOSED


class TravelCalculator:
    """Linear position of a cover from the moment its motor started.

    Positions are floats from 0 (closed) to 100 (open), None while unknown. An unknown
    position becomes known once the cover has run a full travel towards an end.
    """

    def __init__(
        self, opening_time: float, closing_time: float, position: float | None
    ) -> None:
        """Initialize with the full travel times in seconds and the last known position."""
        self._travel_time = {
            Direction.OPENING: opening_time,
            Direction.CLOSING: closing_time,
        }
        self._position = position
        self._direction: Direction | None = None
        self._started_at = 0.0
        self._changed_at = -math.inf

    @property
    def direction(self) -> Direction | None:
        """Return the direction of the running movement, None when stopped."""
        return self._direction

    @property
    def changed_at(self) -> float:
        """Return when the motor last started or stopped."""
        return self._changed_at

    def position(self, now: float) -> float | None:
        """Return the estimated position at a given time."""
        if self._direction is None:
            return self._position
        elapsed = now - self._started_at
        full = self._travel_time[self._direction]
        if self._position is None:
            return self._direction.end if elapsed >= full else None
        step = elapsed * 100 / full
        if self._direction is Direction.OPENING:
            return min(OPEN, self._position + step)
        return max(CLOSED, self._position - step)

    def start(self, direction: Direction, now: float) -> None:
        """Record that the motor started moving in a direction."""
        self.stop(now)
        self._direction = direction
        self._started_at = now
        self._changed_at = now

    def stop(self, now: float) -> None:
        """Record that the motor stopped."""
        self._position = self.position(now)
        self._direction = None
        self._changed_at = now

    def travel_time(self, direction: Direction) -> float:
        """Return the seconds of a full travel in a direction."""
        return self._travel_time[direction]

    def set_position(self, position: float | None) -> None:
        """Replace the stopped position (restore after a restart)."""
        self._position = position

    def time_to(self, target: float, direction: Direction, now: float) -> float:
        """Return the seconds needed to reach a target moving in a direction.

        From an unknown position only an end can be reached: a full travel.
        """
        current = self.position(now)
        full = self.travel_time(direction)
        if current is None:
            return full
        return abs(target - current) * full / 100
