"""calculate_dynamic_heat / apply_persona_ceiling / calculate_effective_heat.

Every expected value below was re-measured against the untouched kernel
(router.calculate_heat + evaluate_compute_tier) before this file was written
(card section 7: the run numbers, not the card's, are authoritative).  The
coefficients live in domain.config as PLACEHOLDER_* structural values, so the
constant-pinning tests exist to catch anyone inlining the numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.core.domain.config import (
    PLACEHOLDER_IPC_BONUS_RATE,
    PLACEHOLDER_PERSONA_HEAT_MODIFIER,
    PLACEHOLDER_RESIDENT_BONUS_RATE,
    PLACEHOLDER_TENSION_THRESHOLD,
)
from packages.core.engine.heat import (
    apply_persona_ceiling,
    calculate_dynamic_heat,
    calculate_effective_heat,
)
from packages.core.engine.router import calculate_heat

D = calculate_dynamic_heat


@pytest.mark.parametrize(
    ("msg_freq", "mention_count", "context_turns", "resident_days", "ipc_affiliation",
     "expected_dynamic", "expected_tier"),
    [
        (0, 0, 0, 0, 0.0, 0.0, "cold"),
        (3, 0, 0, 0, 0.0, 12.0, "warm"),
        (0, 4, 0, 0, 0.0, 50.0, "hot"),
        (5, 1, 3, 0, 0.0, 34.0, "warm"),
        (5, 1, 3, 3, 0.0, 34.3, "warm"),
        (5, 1, 3, 0, 50.0, 39.0, "warm"),
        (5, 1, 3, 7, 100.0, 44.7, "warm"),
        (5, 1, 3, 100, 0.0, 37.0, "warm"),  # capped at 30 resident days = +3.0
    ],
)
def test_eight_row_measured_table(
    msg_freq: int,
    mention_count: int,
    context_turns: int,
    resident_days: int,
    ipc_affiliation: float,
    expected_dynamic: float,
    expected_tier: str,
) -> None:
    dynamic = D(msg_freq, mention_count, context_turns,
                resident_days=resident_days, ipc_affiliation=ipc_affiliation)
    assert dynamic == pytest.approx(expected_dynamic)
    _, tier = calculate_effective_heat(
        msg_freq, mention_count, context_turns,
        resident_days=resident_days, ipc_affiliation=ipc_affiliation,
        persona_heat_modifier=1.0, tension_value=0.0,
    )
    assert tier == expected_tier


@pytest.mark.parametrize("modifier", [0.5, 1.0, 1.5])
def test_persona_three_tiers_on_dynamic_34(modifier: float) -> None:
    effective, tier = calculate_effective_heat(
        5, 1, 3, resident_days=0, ipc_affiliation=0.0,
        persona_heat_modifier=modifier, tension_value=0.0,
    )
    assert effective == pytest.approx(34.0 * modifier)
    # Measured against the kernel: 13.6 / 17.0 / 34.0 are all warm (thresholds
    # <10 cold / <50 warm), only 51.0 crosses into hot.
    assert tier == ("warm" if modifier in (0.5, 1.0) else "hot")


@pytest.mark.parametrize("modifier", [0.5, 1.0, 1.5])
def test_hard_gate_msg_only_base_40(modifier: float) -> None:
    # msg_freq=10 saturates norm_msg -> base 40 (ADR-02 section 3.1's original
    # boundary): the hot persona modifier alone pushes pure message spam into
    # Hot -- the ADR-008 section 2.4 acknowledged reversal.
    effective, tier = calculate_effective_heat(
        10, 0, 0, resident_days=0, ipc_affiliation=0.0,
        persona_heat_modifier=modifier, tension_value=0.0,
    )
    assert effective == pytest.approx(40.0 * modifier)
    assert tier == ("warm" if modifier in (0.5, 1.0) else "hot")


@pytest.mark.parametrize("tension", [79.9, 80.0, 80.1])
def test_tension_threshold_boundary(tension: float) -> None:
    kwargs: dict = dict(
        resident_days=0, ipc_affiliation=0.0,
        persona_heat_modifier=PLACEHOLDER_PERSONA_HEAT_MODIFIER["cold"],
        tension_value=tension,
    )
    effective, tier = calculate_effective_heat(5, 1, 3, **kwargs)
    if tension >= PLACEHOLDER_TENSION_THRESHOLD:
        # Forced hot bypasses the cold persona modifier entirely.
        assert tier == "hot"
        assert effective >= 100.0
    else:
        # 34.0 (dynamic) * 0.4 (cold) = 13.6 -- was 17.0 when cold was 0.5.
        assert (effective, tier) == (pytest.approx(13.6), "warm")


def test_negative_inputs_clamped_to_zero() -> None:
    assert D(3, 0, 0, resident_days=-5, ipc_affiliation=-20.0) == pytest.approx(12.0)


def test_resident_days_cap_30_and_100_identical() -> None:
    a = D(5, 1, 3, resident_days=30, ipc_affiliation=0.0)
    b = D(5, 1, 3, resident_days=100, ipc_affiliation=0.0)
    assert a == b == pytest.approx(37.0)


def test_zero_inputs_degrade_exactly_to_the_kernel() -> None:
    assert D(3, 0, 0, resident_days=0, ipc_affiliation=0.0) == calculate_heat(3, 0, 0)


def test_resident_bonus_uses_the_placeholder_constant() -> None:
    # One extra resident day adds exactly PLACEHOLDER_RESIDENT_BONUS_RATE.
    day0 = D(5, 1, 3, resident_days=0, ipc_affiliation=0.0)
    day1 = D(5, 1, 3, resident_days=1, ipc_affiliation=0.0)
    assert day1 - day0 == pytest.approx(PLACEHOLDER_RESIDENT_BONUS_RATE)


def test_ipc_bonus_uses_the_placeholder_constant() -> None:
    # One unit of affiliation adds exactly PLACEHOLDER_IPC_BONUS_RATE.
    ipc0 = D(5, 1, 3, resident_days=0, ipc_affiliation=0.0)
    ipc1 = D(5, 1, 3, resident_days=0, ipc_affiliation=1.0)
    assert ipc1 - ipc0 == pytest.approx(PLACEHOLDER_IPC_BONUS_RATE)


def test_apply_persona_ceiling_is_exact_multiplication() -> None:
    # cold 0.5 -> 0.4 (human ruling 2026-11-06).
    #
    # 34.0 * 0.4 is 13.600000000000001 in IEEE-754, so the old inputs no
    # longer give an exact decimal. Using 25.0 instead keeps an EXACT expected
    # value (25.0 * 0.4 == 10.0) without writing the expected value as the
    # product of the same two numbers -- `== 34.0 * 0.4` would be tautological
    # (it reproduces the implementation, so a clamp/round could never fail it).
    assert apply_persona_ceiling(25.0, PLACEHOLDER_PERSONA_HEAT_MODIFIER["cold"]) == 10.0
    assert apply_persona_ceiling(25.0, PLACEHOLDER_PERSONA_HEAT_MODIFIER["hot"]) == 37.5
    assert apply_persona_ceiling(34.0, PLACEHOLDER_PERSONA_HEAT_MODIFIER["hot"]) == 51.0


def test_cold_persona_at_saturated_dynamic_heat_stays_warm() -> None:
    """The exact cell that forced cold 0.5 -> 0.4 (human ruling 2026-11-06).

    ``calculate_heat(10, 4, 20) == 100.0`` is the saturation ceiling.  Under
    the old 0.5 coefficient that scaled to exactly 50.0, and
    ``evaluate_compute_tier`` puts 50.0 on the HOT side (ADR-02: "50 归 Hot"),
    so the coldest persona -- meant to be the strongest damper -- was the one
    keeping a maxed-out conversation in Hot.  At 0.4 the damped value is 40.0,
    strictly inside [10, 50), so the persona actually damps.

    Pinned as a value + tier pair rather than a bare arithmetic identity so
    that a future coefficient change cannot silently re-open the hole.
    """
    dynamic = calculate_dynamic_heat(10, 4, 20, resident_days=0, ipc_affiliation=0.0)
    assert dynamic == 100.0

    effective, tier = calculate_effective_heat(
        10, 4, 20,
        resident_days=0, ipc_affiliation=0.0,
        persona_heat_modifier=PLACEHOLDER_PERSONA_HEAT_MODIFIER["cold"],
        tension_value=0.0,
    )
    assert effective == 40.0
    assert tier == "warm"

    # Reverse direction: the same saturated dynamic heat must still reach hot
    # for the neutral and outgoing personas, so 0.4 did not blunt the gate.
    for persona in ("warm", "hot"):
        _, persona_tier = calculate_effective_heat(
            10, 4, 20,
            resident_days=0, ipc_affiliation=0.0,
            persona_heat_modifier=PLACEHOLDER_PERSONA_HEAT_MODIFIER[persona],
            tension_value=0.0,
        )
        assert persona_tier == "hot"


def test_tension_forced_hot_survives_cold_persona() -> None:
    # The bypass is deliberate: (max(effective, 100.0), "hot") regardless of
    # how low the persona modifier is.
    effective, tier = calculate_effective_heat(
        0, 0, 0, resident_days=0, ipc_affiliation=0.0,
        persona_heat_modifier=PLACEHOLDER_PERSONA_HEAT_MODIFIER["cold"],
        tension_value=PLACEHOLDER_TENSION_THRESHOLD,
    )
    assert (effective, tier) == (100.0, "hot")


def test_kernel_functions_untouched_signature_wiring() -> None:
    # heat.py must call the shared kernel, not re-implement it.
    heat_source = Path(__import__("packages.core.engine.heat", fromlist=["x"]).__file__)
    src = heat_source.read_text(encoding="utf-8")
    assert "calculate_heat(" in src
    assert "evaluate_compute_tier(" in src
