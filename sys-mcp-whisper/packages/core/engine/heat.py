"""Dynamic heat, persona ceiling and effective heat (task-FFW-P28-C3).

Three pure arithmetic functions layered on the untouched kernel
``router.calculate_heat`` / ``router.evaluate_compute_tier`` (47 references;
this card only calls them).  Like the kernel, nothing here reads a clock, does
IO or touches the environment: ``resident_days`` is computed by the caller and
handed in.  The coefficients are ``PLACEHOLDER_*`` structural values from
``domain.config`` (real numbers to be decided in P4 after load-testing), so
the function bodies carry no magic numbers.

Effective heat feeds ``evaluate_compute_tier`` with the same 10/50 thresholds
as before; a high Tension value upgrades to ``hot`` and deliberately bypasses
the persona modifier -- a cold persona (0.4) must not be able to push a
tension-driven emergency back down.
"""

from __future__ import annotations

from typing import Final

from ..domain.config import (
    PLACEHOLDER_IPC_BONUS_RATE,
    PLACEHOLDER_RESIDENT_BONUS_RATE,
    PLACEHOLDER_TENSION_THRESHOLD,
)
from .router import calculate_heat, evaluate_compute_tier

# 封顶是为了防止 100 天驻留把热度顶到无意义的量级, 不是业务数值 (业务数值是
# PLACEHOLDER_RESIDENT_BONUS_RATE, 待 P4 压测后裁决)。
_RESIDENT_DAYS_CAP: Final[int] = 30


def calculate_dynamic_heat(
    msg_freq: int,
    mention_count: int,
    context_turns: int,
    *,
    resident_days: int,
    ipc_affiliation: float,
) -> float:
    """Kernel heat plus resident-days and IPC affiliation bonuses.

    ``resident_days`` is capped at ``_RESIDENT_DAYS_CAP`` (see the constant's
    comment) and negative inputs are treated as 0 for both bonuses, matching
    ``calculate_heat``'s negative-clamping convention.  With ``resident_days=0``
    and ``ipc_affiliation=0.0`` this degrades exactly to the kernel.
    """
    base = calculate_heat(msg_freq, mention_count, context_turns)
    resident_bonus = PLACEHOLDER_RESIDENT_BONUS_RATE * min(
        max(0, resident_days), _RESIDENT_DAYS_CAP
    )
    ipc_bonus = PLACEHOLDER_IPC_BONUS_RATE * max(0.0, ipc_affiliation)
    return base + resident_bonus + ipc_bonus


def apply_persona_ceiling(dynamic_heat: float, persona_heat_modifier: float) -> float:
    """Scale a dynamic heat by the persona's heat modifier (cold 0.4 / warm 1.0 / hot 1.5)."""
    return dynamic_heat * persona_heat_modifier


def calculate_effective_heat(
    msg_freq: int,
    mention_count: int,
    context_turns: int,
    *,
    resident_days: int,
    ipc_affiliation: float,
    persona_heat_modifier: float,
    tension_value: float,
) -> tuple[float, str]:
    """Effective heat and its compute tier.

    ``tension_value >= PLACEHOLDER_TENSION_THRESHOLD`` forces the ``hot`` tier
    and bypasses the persona modifier entirely (the emergency must not be
    scaled back down by a cold persona); the returned heat is at least 100.0
    in that branch.  Otherwise the persona modifier applies and the tier comes
    from the kernel's unchanged 10/50 thresholds.
    """
    dynamic = calculate_dynamic_heat(
        msg_freq,
        mention_count,
        context_turns,
        resident_days=resident_days,
        ipc_affiliation=ipc_affiliation,
    )
    effective = apply_persona_ceiling(dynamic, persona_heat_modifier)
    if tension_value >= PLACEHOLDER_TENSION_THRESHOLD:
        return (max(effective, 100.0), "hot")
    return (effective, evaluate_compute_tier(effective))
