"""Tests for packages/core/engine/judge.py (AC-J1..AC-J11).

Written before the implementation on purpose: the first run must fail with an
ImportError and only turns green once judge.py exists.

All test snapshots use explicit values; no test locks a default config number
except where the plan itself pins it (rule names / priorities, epsilon 0.05).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from packages.core.domain.config import (
    DEFAULT_ENGINE_CONFIG,
    ConflictConfig,
    ConflictRule,
    EngineConfig,
)
from packages.core.domain.global_context import DELTA_ELIGIBLE_PATHS, PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.dynamics import DecayRates, apply_time_decay
from packages.core.engine.judge import (
    ConflictVerdict,
    HormoneDeltas,
    is_conflict,
)

TS = datetime(2026, 11, 4, 12, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {
    "anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL),
    "joy": EmotionValue(intensity=10.0, ttl_expires_at=TTL),
}


def make_pulse(
    *,
    dopamine: float = 50.0,
    cortisol: float = 50.0,
    oxytocin: float = 50.0,
) -> PulseState:
    """The pulse carrier the three hormone lanes moved to in P2.8."""
    return PulseState(dopamine=dopamine, cortisol=cortisol, oxytocin=oxytocin)


def make_snapshot(
    *,
    affiliation: float = 0.0,
    dominance: float = 0.0,
) -> S1Snapshot:
    return S1Snapshot(
        version=1,
        scope_id="scope-1",
        timestamp=TS,
        last_decay_at=TS,
        is_conflict=False,
        ipc=IPCState(
            affiliation=affiliation,
            dominance=dominance,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={
            "tsundere": TraitState(score=85, visibility="unlocked"),
            "shyness": TraitState(score=30, visibility="dormant"),
        },
        emotions=EMOTIONS,
    )


def test_rule_hit_returns_true() -> None:
    # AC-J2: dopamine +1.0 and cortisol +1.0, epsilon 0.05 -> rule 1 matches.
    verdict = is_conflict(
        make_snapshot(),
        HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": 1.0}),
    )
    assert verdict == ConflictVerdict(
        conflicted=True,
        matched_rules=("dopamine_up_and_cortisol_up",),
    )


def test_no_rule_hit_returns_false() -> None:
    # AC-J3: dopamine up but cortisol down -> no rule matches.
    verdict = is_conflict(
        make_snapshot(),
        HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": -1.0}),
    )
    assert verdict == ConflictVerdict(conflicted=False, matched_rules=())


def test_invalid_field_path_raises() -> None:
    # AC-J4: a rule with a path outside the S1Snapshot field whitelist is
    # rejected at construction time (before any evaluation).
    dirty = EngineConfig(
        conflict=ConflictConfig(
            rules=(
                ConflictRule(
                    name="dirty_rule",
                    conditions=(("hormones.nothing", "up"),),
                    priority=10,
                ),
            ),
            epsilon=0.05,
        ),
        dynamics=DEFAULT_ENGINE_CONFIG.dynamics,
        assembly=DEFAULT_ENGINE_CONFIG.assembly,
    )
    with pytest.raises(ValueError, match="invalid field_path"):
        is_conflict(make_snapshot(), HormoneDeltas(values={}), dirty)


def test_epsilon_boundary_is_exclusive() -> None:
    # AC-J5: delta == +epsilon and delta == -epsilon both count as flat.
    eps = DEFAULT_ENGINE_CONFIG.conflict.epsilon
    up_at_boundary = is_conflict(
        make_snapshot(),
        HormoneDeltas(
            values={"pulse.dopamine": eps, "pulse.cortisol": eps},
        ),
    )
    assert up_at_boundary.conflicted is False

    down_at_boundary = is_conflict(
        make_snapshot(),
        HormoneDeltas(
            values={"pulse.dopamine": -eps, "pulse.cortisol": -eps},
        ),
    )
    assert down_at_boundary.conflicted is False

    just_above = is_conflict(
        make_snapshot(),
        HormoneDeltas(
            values={"pulse.dopamine": eps + 1e-9, "pulse.cortisol": eps + 1e-9},
        ),
    )
    assert just_above.conflicted is True


def test_multiple_rules_matched_priority_order() -> None:
    # AC-J6: both rules hit at once -> matched_rules in priority order.
    verdict = is_conflict(
        make_snapshot(),
        HormoneDeltas(
            values={
                "pulse.dopamine": 1.0,
                "pulse.cortisol": 1.0,
                "ipc.affiliation": 1.0,
                "ipc.dominance": -1.0,
            }
        ),
    )
    assert verdict.conflicted is True
    assert verdict.matched_rules == (
        "dopamine_up_and_cortisol_up",
        "affiliation_up_and_dominance_down",
    )


def test_missing_delta_defaults_to_flat() -> None:
    # AC-J7: a delta key absent from values counts as 0 (flat), no error.
    verdict = is_conflict(make_snapshot(), HormoneDeltas(values={}))
    assert verdict == ConflictVerdict(conflicted=False, matched_rules=())

    half = is_conflict(
        make_snapshot(),
        HormoneDeltas(values={"pulse.dopamine": 1.0}),  # cortisol missing
    )
    assert half.conflicted is False


def test_pure_function_no_input_mutation() -> None:
    # AC-J8: neither the snapshot nor the deltas object changes.
    snapshot = make_snapshot()
    deltas = HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": 1.0})
    before_snapshot = snapshot.model_dump()
    before_values = dict(deltas.values)
    is_conflict(snapshot, deltas)
    assert snapshot.model_dump() == before_snapshot
    assert deltas.values == before_values


def test_deltas_must_come_from_decay() -> None:
    # AC-J9 (semantic equivalent of "decay before evaluation"): deltas taken
    # from a real apply_time_decay run only ever regress towards baseline, so
    # they must not trigger a conflict; reversing their direction must.
    prev = make_snapshot(affiliation=10.0, dominance=20.0)
    prev_pulse = make_pulse(dopamine=60.0, cortisol=55.0, oxytocin=50.0)
    rates = DecayRates(
        hormone_per_hour={"dopamine": 1.0, "cortisol": 1.0, "oxytocin": 1.0},
        affiliation_per_hour=0.5,
    )
    decayed, decayed_pulse = apply_time_decay(prev, prev_pulse, timedelta(hours=1), rates)
    real_deltas = {
        "pulse.dopamine": decayed_pulse.dopamine - prev_pulse.dopamine,
        "pulse.cortisol": decayed_pulse.cortisol - prev_pulse.cortisol,
        "pulse.oxytocin": decayed_pulse.oxytocin - prev_pulse.oxytocin,
        "ipc.affiliation": decayed.ipc.affiliation - prev.ipc.affiliation,
    }
    assert real_deltas == {
        "pulse.dopamine": -1.0,
        "pulse.cortisol": -1.0,
        "pulse.oxytocin": 0.0,
        "ipc.affiliation": -0.5,
    }

    verdict = is_conflict(decayed, HormoneDeltas(values=real_deltas))
    assert verdict == ConflictVerdict(conflicted=False, matched_rules=())

    reversed_deltas = HormoneDeltas(
        values={
            "pulse.dopamine": 1.0,
            "pulse.cortisol": 1.0,
        }
    )
    reversed_verdict = is_conflict(decayed, reversed_deltas)
    assert reversed_verdict.conflicted is True
    assert reversed_verdict.matched_rules == ("dopamine_up_and_cortisol_up",)


def test_default_config_is_used_when_omitted() -> None:
    # The default argument is DEFAULT_ENGINE_CONFIG, so rule behaviour above
    # is data-driven; swapping the config swaps the verdict without code change.
    with pytest.raises(ValueError):
        EngineConfig(
            conflict=ConflictConfig(
                rules=(),  # invalid: rejected by ConflictConfig itself
                epsilon=0.05,
            ),
            dynamics=DEFAULT_ENGINE_CONFIG.dynamics,
            assembly=DEFAULT_ENGINE_CONFIG.assembly,
        )

    only_rule2 = EngineConfig(
        conflict=ConflictConfig(
            rules=(DEFAULT_ENGINE_CONFIG.conflict.rules[1],),
            epsilon=0.05,
        ),
        dynamics=DEFAULT_ENGINE_CONFIG.dynamics,
        assembly=DEFAULT_ENGINE_CONFIG.assembly,
    )
    verdict = is_conflict(
        make_snapshot(),
        HormoneDeltas(
            values={
                "pulse.dopamine": 1.0,
                "pulse.cortisol": 1.0,
                "ipc.affiliation": 1.0,
                "ipc.dominance": -1.0,
            }
        ),
        only_rule2,
    )
    assert verdict.matched_rules == ("affiliation_up_and_dominance_down",)


def test_verdict_is_frozen() -> None:
    verdict = is_conflict(
        make_snapshot(),
        HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": 1.0}),
    )
    with pytest.raises(AttributeError):
        verdict.conflicted = False  # type: ignore[misc]


def test_hormone_deltas_is_frozen() -> None:
    deltas = HormoneDeltas(values={"pulse.dopamine": 1.0})
    # Pydantic frozen=True raises ValidationError (not an AttributeError subclass),
    # unlike dataclass frozen=True which raises FrozenInstanceError.
    with pytest.raises(ValidationError):
        deltas.values = {}  # type: ignore[misc]


def test_invalid_hormone_key_raises_error() -> None:
    # Forward: an undefined key (typo "dopamin") must raise at construction time.
    with pytest.raises(ValidationError, match="unknown delta key: dopamin"):
        HormoneDeltas(values={"dopamin": 5.0})

    # Reverse guard: a legal dotted path must construct successfully with the
    # exact payload preserved -- otherwise the test would be a "rejects
    # everything" tautology.
    deltas = HormoneDeltas(values={"pulse.dopamine": 1.0})
    assert deltas.values == {"pulse.dopamine": 1.0}

    # First-call semantics (plan-judge.md R2 table, row 3): an empty dict is
    # legal -- every field counts as 0.0 (flat), is_conflict stays False.
    empty = HormoneDeltas(values={})
    assert empty.values == {}


def test_delta_whitelist_is_the_five_eligible_paths() -> None:
    """The judge's accepted key set is a contract surface, so pin it.

    Until P2.8 the judge derived the whitelist from ``S1Snapshot``; ADR-008
    section 1.8 replaced that derivation with the literal
    ``DELTA_ELIGIBLE_PATHS``, because a derived set could not express the two
    things the contract needs: "``timestamp`` is a snapshot field but not a
    delta", and "the hormones are deltas but no longer snapshot fields".

    Both halves are asserted: the count (a dropped path must not be masked by
    an added one) and the exact membership.
    """
    expected = frozenset(
        {
            "pulse.dopamine",
            "pulse.cortisol",
            "pulse.oxytocin",
            "ipc.affiliation",
            "ipc.dominance",
        }
    )
    assert sorted(DELTA_ELIGIBLE_PATHS) == sorted(expected)
    assert len(DELTA_ELIGIBLE_PATHS) == 5

    # Reverse guard: a legal key must still reach the rule table, i.e. the
    # constant is not merely declared but actually consulted by the judge.
    verdict = is_conflict(
        make_snapshot(),
        HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": 1.0}),
    )
    assert verdict.conflicted is True
    assert verdict.matched_rules == ("dopamine_up_and_cortisol_up",)
