"""Random sequences with the six relay models, with fixed seeds.

Interlock violations must be 0 with relays that carry out commands at once (immediate,
late report, polling, optimistic, lost). With relays that carry them out late, both
relays may be on for a moment: an accepted risk, counted apart without failing. No
sequence may be left without a timer, lose the position, miss its target or end without
following what the relays do, with any model. The position family has no report
glitches: a glitch is two presses and gives the target up.

All counts: `uv run python -m tests.random_sim --seed 7 --runs 3000`.
"""

from collections import Counter

import pytest

from .random_sim import Model, run_many

RUNS_PER_FAMILY = 3000
SAFE_MODELS = (
    Model.IMMEDIATE,
    Model.LATE_REPORT,
    Model.POLLING,
    Model.OPTIMISTIC,
    Model.LOST,
)
RISKY_MODELS = (Model.LATE_EXECUTION,)


def _count(totals: Counter[str], kind: str, models: tuple[Model, ...]) -> int:
    return sum(totals[f"{kind}/{model}"] for model in models)


@pytest.mark.parametrize("seed", [20261104, 7, 1, 2])
def test_random_sequences(seed: int, record_property: pytest.RecordProperty) -> None:
    """Counts of sequences with each kind of problem, per relay model."""
    totals = run_many(seed, RUNS_PER_FAMILY)
    accepted = {
        kind: _count(totals, kind, RISKY_MODELS) for kind in ("violations", "masked")
    }
    record_property("accepted_risk", accepted)
    assert _count(totals, "violations", SAFE_MODELS) == 0
    assert _count(totals, "masked", SAFE_MODELS) == 0
    for kind in ("no_timer", "position_errors", "target_errors", "unfollowed"):
        assert _count(totals, kind, tuple(Model)) == 0, kind
