"""Regression lock for the P2.5-T6 dropped-tier bug, kept through P2.8.

``apply_time_decay`` rebuilds ``S1Snapshot`` field by field; before the fix
``compute_tier`` (the tenth field, added after the rebuild was written) was
left out of that list and every decay silently reset the tier to its Pydantic
default ``"warm"``.  Both non-default tiers are asserted here: locking only
"hot" would also pass an implementation written as ``if x: "hot" else "warm"``.

P2.8 (ADR-008 section 1.7) gave the function a second return value; the tier
lock is unchanged in intent but unpacks the tuple, because the snapshot is now
the first of two carriers and a test written against a bare return would not
even import.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from packages.core.domain.global_context import PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.dynamics import apply_time_decay

TS = datetime(2026, 1, 1, tzinfo=UTC)
TTL = datetime(2030, 1, 1, tzinfo=UTC)


def make_tiered_snapshot(tier: str) -> S1Snapshot:
    return S1Snapshot(
        version=1,
        scope_id="scope-decay",
        timestamp=TS,
        last_decay_at=TS,
        is_conflict=True,
        compute_tier=tier,
        ipc=IPCState(
            affiliation=12.0,
            dominance=12.5,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"warmth": TraitState(score=70, visibility="unlocked")},
        emotions={"anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL)},
    )


def make_pulse() -> PulseState:
    return PulseState(dopamine=80.0, cortisol=30.0, oxytocin=95.0)


@pytest.mark.parametrize("tier", ["hot", "cold"])
def test_apply_time_decay_carries_the_tier_through_verbatim(tier: str) -> None:
    snapshot = make_tiered_snapshot(tier)
    pulse = make_pulse()
    decayed, decayed_pulse = apply_time_decay(snapshot, pulse, timedelta(hours=24))
    assert decayed.compute_tier == tier
    # The pulse half must move as well: 24h at 1.0/h towards the 50 baseline
    # gives dopamine 80 -> 56.0, cortisol 30 -> 50.0 (distance 20 < step 24, so
    # it lands exactly on the baseline rather than overshooting), oxytocin
    # 95 -> 71.0.
    assert decayed_pulse.dopamine == pytest.approx(56.0)
    assert decayed_pulse.cortisol == pytest.approx(50.0)
    assert decayed_pulse.oxytocin == pytest.approx(71.0)
