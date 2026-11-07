"""Heat computation locks (task-FFW-P25-T2).

Covers the ADR spec for ``packages/core/engine/router.py``:

* the nine-cell table from ADR section 4.1, asserted cell by cell;
* the three metric saturation ceilings and their tier consequences
  (msg_freq alone can never reach hot -- a design consequence, not an
  implementation accident);
* the cold/warm and warm/hot boundaries pinned from both sides;
* purity (same input -> same output) and range ``[0.0, 100.0]``.

Purity guards over ``packages/core/engine/`` (import whitelist, banned clock
calls, AST based in ``tests/engine/test_dynamics.py``) pick up ``router.py``
automatically once the file exists -- no duplicate guard is written here.
The two non-tautology mutation checks (card 4.4) run as manual verifications
recorded in the task report: per C8R, mutations must never rewrite the
production tree from a test process, so they are executed against a
temporary copy outside the suite.
"""

from __future__ import annotations

import pytest

from packages.core.engine import calculate_heat, evaluate_compute_tier

# ---------------------------------------------------------------------------
# Card 4.2: the nine-cell table, one cell per assertion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("msg_freq", "mention_count", "context_turns", "expected_heat", "expected_tier"),
    [
        (0, 0, 0, 0.0, "cold"),
        (2, 0, 0, 8.0, "cold"),
        (3, 0, 0, 12.0, "warm"),
        (0, 1, 0, 12.0, "warm"),
        (0, 3, 0, 37.0, "warm"),
        (0, 4, 0, 50.0, "hot"),
        (10, 0, 5, 42.0, "warm"),
        (10, 2, 10, 70.0, "hot"),
        (5, 1, 3, 34.0, "warm"),
    ],
)
def test_heat_table_cell(
    msg_freq: int, mention_count: int, context_turns: int, expected_heat: float, expected_tier: str
) -> None:
    heat = calculate_heat(msg_freq, mention_count, context_turns)
    assert heat == expected_heat, (msg_freq, mention_count, context_turns)
    assert evaluate_compute_tier(heat) == expected_tier


# ---------------------------------------------------------------------------
# Card 4.1.1: tier boundaries pinned from both sides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heat", "expected"),
    [(9.0, "cold"), (10.0, "warm"), (11.0, "warm"), (49.0, "warm"), (50.0, "hot"), (51.0, "hot")],
)
def test_tier_boundary_six_cells(heat: float, expected: str) -> None:
    assert evaluate_compute_tier(heat) == expected


def test_tier_boundary_values_come_from_calculate_heat() -> None:
    """The pinned literals 10.0 and 50.0 must be real calculate_heat outputs."""
    assert calculate_heat(0, 0, 20) == 10.0  # 1 * min(20*5, 100) // 10
    assert evaluate_compute_tier(calculate_heat(0, 0, 20)) == "warm"
    assert calculate_heat(0, 4, 0) == 50.0
    assert evaluate_compute_tier(calculate_heat(0, 4, 0)) == "hot"


# ---------------------------------------------------------------------------
# Card 4.1.2: the three independent metric ceilings
# ---------------------------------------------------------------------------


def test_msg_freq_alone_caps_at_warm() -> None:
    """Design consequence: msg_freq alone saturates at 40.0 -- never hot."""
    assert calculate_heat(10, 0, 0) == 40.0
    assert evaluate_compute_tier(calculate_heat(10, 0, 0)) == "warm"
    assert calculate_heat(1000, 0, 0) == 40.0
    assert evaluate_compute_tier(calculate_heat(1000, 0, 0)) == "warm"


def test_mention_alone_reaches_hot() -> None:
    assert calculate_heat(0, 4, 0) == 50.0
    assert evaluate_compute_tier(calculate_heat(0, 4, 0)) == "hot"


def test_context_turns_alone_lands_on_the_boundary() -> None:
    assert calculate_heat(0, 0, 20) == 10.0
    assert evaluate_compute_tier(calculate_heat(0, 0, 20)) == "warm"


# ---------------------------------------------------------------------------
# Card 4.1.3-4.1.7: saturation, zero, range, purity, non-tautology
# ---------------------------------------------------------------------------


def test_saturation_truncation() -> None:
    assert calculate_heat(100, 0, 0) == calculate_heat(10, 0, 0) == 40.0
    assert calculate_heat(0, 100, 0) == calculate_heat(0, 4, 0) == 50.0
    assert calculate_heat(0, 0, 100) == calculate_heat(0, 0, 20) == 10.0


def test_all_zero_inputs() -> None:
    assert calculate_heat(0, 0, 0) == 0.0
    assert evaluate_compute_tier(0.0) == "cold"


def test_heat_always_in_range() -> None:
    for msg_freq in (0, 1, 5, 10, 50):
        for mention_count in (0, 1, 4, 50):
            for context_turns in (0, 3, 20, 99):
                heat = calculate_heat(msg_freq, mention_count, context_turns)
                assert 0.0 <= heat <= 100.0


def test_calculate_heat_is_pure() -> None:
    first = calculate_heat(7, 2, 11)
    for _ in range(3):
        assert calculate_heat(7, 2, 11) == first
    assert calculate_heat(7, 2, 11) != calculate_heat(8, 2, 11)


def test_tier_covers_all_three_values() -> None:
    """Anti-tautology: all three tiers are actually reachable."""
    reached = {evaluate_compute_tier(h) for h in (0.0, 30.0, 90.0)}
    assert reached == {"cold", "warm", "hot"}
    assert evaluate_compute_tier(-5.0) == "cold"  # any heat < 10

# --- P2.5 Bug 3: input hygiene (human ruling 2026-11-06) -------------------
#
# Two different failure classes, deliberately handled differently:
#   * negative counts -> clamped to 0 (bad upstream arithmetic, but "no
#     activity" is a defensible reading)
#   * nan / inf      -> raise (the caller's math is broken; ADR-006 forbids
#     SILENTLY substituting a degraded heat)


@pytest.mark.parametrize(
    ("msg_freq", "mention_count", "context_turns"),
    [
        (-1, 0, 0),
        (0, -1, 0),
        (0, 0, -1),
        (-5, -5, -5),
        (-100, -100, -100),
    ],
)
def test_negative_counts_are_clamped_to_zero(
    msg_freq: int, mention_count: int, context_turns: int
) -> None:
    """A negative count means "nothing happened", not "negative heat".

    Before the fix ``calculate_heat(-1, 0, 0)`` returned ``-4.0``: the docstring
    promised ``[0.0, 100.0]`` and the function did not deliver it.  ``-5, -5, -5``
    returned ``-85.0``.  Clamping must be applied per-argument, not by
    ``max(0, heat)`` at the end -- the latter would let one negative input
    cancel out two positive ones.
    """
    assert calculate_heat(msg_freq, mention_count, context_turns) == 0.0


def test_clamping_is_per_argument_not_on_the_total() -> None:
    """Why ``max(0, heat)`` at the return would be wrong.

    ``(-10, 4, 0)``: msg_freq contributes ``-400/10 = -40``, mention ``+500/10
    = +50``.  Clamping the total would yield ``+10`` (warm) -- a group where
    nobody spoke in 5 minutes but someone was mentioned 4 times is NOT warm on
    the strength of a negative message count.  Clamping per argument yields
    ``+50`` (hot).  The two answers differ, so this is a real discriminator.
    """
    assert calculate_heat(-10, 4, 0) == 50.0
    assert calculate_heat(-10, 4, 0) != 10.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_non_finite_inputs_raise_instead_of_degrading(bad: float, position: int) -> None:
    """``nan`` / ``inf`` must raise -- never be read as idle or as hot.

    Measured before the fix: ``calculate_heat(nan, 0, 0)`` returned ``nan``,
    which falls through BOTH comparisons in ``evaluate_compute_tier`` and
    therefore comes out as ``hot`` -- the most expensive tier, chosen by
    accident.  ``inf`` returned ``40.0`` (warm).  ADR-006 5 forbids silently
    substituting a degraded heat, so the honest answer is to refuse.

    The error names the offending argument: a bare "invalid input" would leave
    the caller to guess which of three counters broke.
    """
    args = [0, 0, 0]
    args[position] = bad
    name = ("msg_freq", "mention_count", "context_turns")[position]
    with pytest.raises(ValueError, match=f"heat input {name} must be finite"):
        calculate_heat(*args)


def test_valid_inputs_are_unaffected_by_the_hygiene_layer() -> None:
    """Reverse guard: the clamp/raise layer must not perturb normal inputs.

    Without this, an implementation that raised on EVERY input would satisfy
    all the assertions above.
    """
    assert calculate_heat(3, 0, 0) == 12.0  # the ADR nine-cell table, row 3
    assert calculate_heat(0, 4, 0) == 50.0
    assert calculate_heat(10, 2, 10) == 70.0
    assert calculate_heat(0, 0, 0) == 0.0
