"""Pure tests of the travel calculator."""

import pytest

from custom_components.relay_cover.travel import (
    CLOSED,
    OPEN,
    Direction,
    TravelCalculator,
)


def test_position_is_linear_with_separate_times() -> None:
    """Opening 20 s (5 %/s) and closing 40 s (2.5 %/s), clamped at the ends."""
    calc = TravelCalculator(20, 40, 50)
    calc.start(Direction.OPENING, 0)
    assert calc.position(4) == 70
    assert calc.position(30) == OPEN
    calc.stop(4)
    assert calc.direction is None
    assert calc.position(100) == 70
    calc.start(Direction.CLOSING, 10)
    assert calc.position(18) == 50
    assert calc.position(100) == CLOSED


def test_unknown_position_becomes_known_after_full_travel() -> None:
    """From unknown only a full travel gives a position."""
    calc = TravelCalculator(20, 40, None)
    assert calc.time_to(OPEN, Direction.OPENING, 0) == 20
    calc.start(Direction.OPENING, 0)
    assert calc.position(19.9) is None
    assert calc.position(20) == OPEN
    calc.stop(10)
    assert calc.position(10) is None


@pytest.mark.parametrize(
    ("start", "target", "direction", "seconds"),
    [
        pytest.param(0, 30, Direction.OPENING, 6, id="up_30pct_of_20s"),
        pytest.param(80, 20, Direction.CLOSING, 24, id="down_60pct_of_40s"),
        pytest.param(100, 100, Direction.OPENING, 0, id="already_there"),
    ],
)
def test_time_to(
    start: float, target: float, direction: Direction, seconds: float
) -> None:
    """Seconds to a target, without any margin at the ends."""
    assert TravelCalculator(20, 40, start).time_to(target, direction, 0) == seconds


def test_changed_at_and_restore() -> None:
    """changed_at follows starts and stops; set_position replaces a stopped position."""
    calc = TravelCalculator(20, 40, None)
    calc.set_position(25)
    calc.start(Direction.OPENING, 3)
    assert calc.changed_at == 3
    calc.stop(5)
    assert calc.changed_at == 5
    assert calc.position(5) == 35
