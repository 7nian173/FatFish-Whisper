"""merge_cross_domain_state tests, rewritten for P2.8 (ADR-008 section 1.6).

Before P2.8 the merge blended four quantities: the three hormone lanes and
``ipc.affiliation``.  ADR-008 section 1.6 moved the hormone inflow to the
stimulus delta, so the merge now transfers IPC only and hands back the cold
side's pulse untouched.  The old assertions on ``merged.hormones`` are gone by
design -- ADR-008 section 4.2 asks for a rewrite against the new contract, not
a repair of the old expectation.

Every call unpacks ``(snapshot, pulse)`` and asserts on both halves: a merge
that returned the right snapshot with the wrong pulse would otherwise pass in
silence.

``weight`` is still the HOT-side weight (ruling 2026-11-06 00:27: global 0.7 +
local 0.3).  The tier lock survives the rewrite: ``compute_tier`` is asserted
separately, because a rebuild that fell back to the Pydantic default would
silently reset it to ``warm``.
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
from packages.core.engine import merge_cross_domain_state
from packages.core.engine.dynamics import TimeDecayError

TS = datetime(2026, 11, 6, 1, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {"anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL)}


def make_snapshot(
    tier: str,
    *,
    affiliation: float = 0.0,
    scope_id: str = "scope-cold",
    version: int = 1,
    is_conflict: bool = True,
) -> S1Snapshot:
    """A full local snapshot.  Hormones are no longer a snapshot field."""
    return S1Snapshot(
        version=version,
        scope_id=scope_id,
        timestamp=TS,
        last_decay_at=TS,
        is_conflict=is_conflict,
        compute_tier=tier,
        ipc=IPCState(
            affiliation=affiliation,
            dominance=12.5,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"warmth": TraitState(score=70, visibility="unlocked")},
        emotions=EMOTIONS,
    )


def make_pulse(
    *,
    dopamine: float = 50.0,
    cortisol: float = 50.0,
    oxytocin: float = 50.0,
    delayed_impact: dict[str, float] | None = None,
) -> PulseState:
    return PulseState(
        dopamine=dopamine,
        cortisol=cortisol,
        oxytocin=oxytocin,
        delayed_impact={} if delayed_impact is None else delayed_impact,
    )


def test_weight_default_blends_ipc_only() -> None:
    cold = make_snapshot("cold", affiliation=-10.0)
    hot = make_snapshot("hot", affiliation=30.0)

    merged, merged_pulse = merge_cross_domain_state(
        cold, make_pulse(), hot, make_pulse(), now=hot.last_decay_at
    )

    # affiliation: -10*0.7 + 30*0.3 = -7 + 9 = 2.0
    assert merged.ipc.affiliation == pytest.approx(2.0)
    # The pulse is NOT part of the blend any more (ADR-008 section 1.6).
    assert merged_pulse.dopamine == pytest.approx(50.0)
    assert merged_pulse.cortisol == pytest.approx(50.0)
    assert merged_pulse.oxytocin == pytest.approx(50.0)


def test_weight_zero_returns_cold_affiliation_exactly() -> None:
    cold = make_snapshot("cold", affiliation=-25.0)
    hot = make_snapshot("hot", affiliation=50.0)

    merged, _ = merge_cross_domain_state(
        cold, make_pulse(), hot, make_pulse(), weight=0.0, now=hot.last_decay_at
    )

    assert merged.ipc.affiliation == pytest.approx(-25.0)


def test_weight_one_returns_hot_affiliation_exactly() -> None:
    cold = make_snapshot("cold", affiliation=-25.0)
    hot = make_snapshot("hot", affiliation=50.0)

    merged, _ = merge_cross_domain_state(
        cold, make_pulse(), hot, make_pulse(), weight=1.0, now=hot.last_decay_at
    )

    assert merged.ipc.affiliation == pytest.approx(50.0)


@pytest.mark.parametrize("bad_weight", [1.5, -0.1])
def test_out_of_range_weight_raises_not_clamped(bad_weight: float) -> None:
    cold = make_snapshot("cold")
    hot = make_snapshot("hot")
    with pytest.raises(ValueError):
        merge_cross_domain_state(
            cold, make_pulse(), hot, make_pulse(), weight=bad_weight, now=hot.last_decay_at
        )


def test_non_merged_fields_taken_from_cold_verbatim() -> None:
    # Cross-domain by definition: a different scope_id is NOT an error.
    cold = make_snapshot("cold", scope_id="scope-global", version=3)
    hot = make_snapshot("hot", scope_id="scope-local", version=1)
    cold_pulse = make_pulse(dopamine=11.0, delayed_impact={"dopamine": 5.0})

    merged, merged_pulse = merge_cross_domain_state(
        cold, cold_pulse, hot, make_pulse(dopamine=99.0), now=hot.last_decay_at
    )

    # compute_tier asserted SEPARATELY: the only lock against a rebuild that
    # silently resets the tier to "warm".
    assert merged.compute_tier == "cold"
    assert merged.version == cold.version
    assert merged.scope_id == cold.scope_id
    assert merged.timestamp == cold.timestamp
    assert merged.last_decay_at == cold.last_decay_at
    assert merged.is_conflict == cold.is_conflict
    assert merged.traits == cold.traits
    assert merged.emotions == cold.emotions
    assert merged.ipc.dominance == cold.ipc.dominance
    # delayed_impact lives on PulseState and is not blended: it survives from
    # the cold side verbatim.
    assert merged_pulse.delayed_impact == {"dopamine": 5.0}
    assert merged_pulse.dopamine == pytest.approx(11.0)


def test_inputs_are_not_mutated() -> None:
    cold = make_snapshot("cold", affiliation=-10.0)
    hot = make_snapshot("hot", affiliation=30.0)
    cold_pulse = make_pulse(dopamine=40.0)
    hot_pulse = make_pulse(dopamine=80.0)
    cold_dump = cold.model_dump()
    hot_dump = hot.model_dump()
    cold_pulse_dump = cold_pulse.model_dump()
    hot_pulse_dump = hot_pulse.model_dump()

    merge_cross_domain_state(cold, cold_pulse, hot, hot_pulse, weight=0.3, now=hot.last_decay_at)

    assert cold.model_dump() == cold_dump
    assert hot.model_dump() == hot_dump
    assert cold_pulse.model_dump() == cold_pulse_dump
    assert hot_pulse.model_dump() == hot_pulse_dump


def test_result_is_a_fresh_object_not_an_input() -> None:
    cold = make_snapshot("cold")
    hot = make_snapshot("hot")
    cold_pulse = make_pulse()
    merged, merged_pulse = merge_cross_domain_state(
        cold, cold_pulse, hot, make_pulse(), now=hot.last_decay_at
    )
    assert merged is not cold
    assert merged is not hot
    assert merged.ipc is not cold.ipc
    assert merged_pulse is not cold_pulse


HOT_LDA = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
NOW_24H = HOT_LDA + timedelta(hours=24)


def make_aware_snapshot(
    tier: str,
    *,
    last_decay_at: datetime,
    affiliation: float = 0.0,
) -> S1Snapshot:
    """Aware-datetime variant of ``make_snapshot`` (naive/aware must not mix)."""
    return S1Snapshot(
        version=1,
        scope_id="scope-x",
        timestamp=last_decay_at,
        last_decay_at=last_decay_at,
        is_conflict=True,
        compute_tier=tier,
        ipc=IPCState(
            affiliation=affiliation,
            dominance=12.5,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"warmth": TraitState(score=70, visibility="unlocked")},
        emotions=EMOTIONS,
    )


def test_24h_hot_decay_blends_decayed_affiliation_hand_computed() -> None:
    # DEFAULT_RATES decays hormones 1.0/h towards 50 and affiliation 0.5/h
    # towards 0.  hot affiliation 30.0 over 24h -> 30 - 12 = 18.0;
    # affiliation: 0.7 * (-10.0) + 0.3 * 18.0 = -7.0 + 5.4 = -1.6.
    cold = make_aware_snapshot("cold", last_decay_at=HOT_LDA, affiliation=-10.0)
    hot = make_aware_snapshot("hot", last_decay_at=HOT_LDA, affiliation=30.0)

    merged, _ = merge_cross_domain_state(cold, make_pulse(), hot, make_pulse(), now=NOW_24H)

    assert merged.ipc.affiliation == pytest.approx(-1.6)


def test_only_hot_side_is_decayed_cold_side_untouched_by_time() -> None:
    # Reverse lock: the cold side's last_decay_at sits 1000h in the past while
    # the hot side is 24h stale.  The cold values must arrive VERBATIM -- decay
    # is the hot side's job only; merge must never decay cold.
    cold_lda = HOT_LDA - timedelta(hours=1000)
    cold = make_aware_snapshot("cold", last_decay_at=cold_lda, affiliation=-30.0)
    hot = make_aware_snapshot("hot", last_decay_at=HOT_LDA, affiliation=30.0)

    merged, merged_pulse = merge_cross_domain_state(
        cold, make_pulse(dopamine=13.0), hot, make_pulse(dopamine=80.0), now=NOW_24H
    )

    # Affiliation: 0.7 * -30.0 + 0.3 * 18.0 = -21.0 + 5.4 = -15.6.  If cold
    # had been decayed its -30.0 would clamp at 0.0 and give 5.4.
    assert merged.ipc.affiliation == pytest.approx(-15.6)
    # Hormones are no longer blended, so the cold pulse's 13.0 arrives as is --
    # not 0.7 * 13.0 + 0.3 * 56.0.
    assert merged_pulse.dopamine == pytest.approx(13.0)


def test_negative_delta_t_raises_time_decay_error() -> None:
    cold = make_aware_snapshot("cold", last_decay_at=HOT_LDA)
    hot = make_aware_snapshot("hot", last_decay_at=HOT_LDA)
    earlier = HOT_LDA - timedelta(hours=1)

    with pytest.raises(TimeDecayError):
        merge_cross_domain_state(cold, make_pulse(), hot, make_pulse(), now=earlier)


def test_merged_tier_tracks_cold_snap_not_decayed_default() -> None:
    # Cross-tier lock: when the cold side's tier is NOT "cold", the merged tier
    # must still equal cold.compute_tier.  Taking it from the decayed side
    # would let a "hot" cold domain drift and silently stop calling Track B.
    cold = make_aware_snapshot("hot", last_decay_at=HOT_LDA)
    hot = make_aware_snapshot("hot", last_decay_at=HOT_LDA)

    merged, _ = merge_cross_domain_state(cold, make_pulse(), hot, make_pulse(), now=NOW_24H)

    assert merged.compute_tier == "hot"


def test_result_last_decay_at_is_now() -> None:
    cold = make_aware_snapshot("cold", last_decay_at=HOT_LDA)
    hot = make_aware_snapshot("hot", last_decay_at=HOT_LDA)

    merged, _ = merge_cross_domain_state(cold, make_pulse(), hot, make_pulse(), now=NOW_24H)

    assert merged.last_decay_at == NOW_24H
    assert merged.compute_tier == "cold"


def test_hormones_are_not_blended_any_more() -> None:
    """ADR-008 section 1.6: the merge transfers IPC only.

    This is the discriminating lock for the rewrite.  The pre-P2.8 contract
    blended the three lanes, so a stale implementation would return
    0.7 * 40.0 + 0.3 * 80.0 = 52.0 here.  The cold side's own values must come
    back untouched instead.
    """
    cold = make_aware_snapshot("cold", last_decay_at=HOT_LDA)
    hot = make_aware_snapshot("hot", last_decay_at=HOT_LDA)
    cold_pulse = make_pulse(dopamine=40.0, cortisol=60.0, oxytocin=20.0)

    _, merged_pulse = merge_cross_domain_state(
        cold,
        cold_pulse,
        hot,
        make_pulse(dopamine=80.0, cortisol=20.0, oxytocin=40.0),
        now=NOW_24H,
    )

    assert merged_pulse.dopamine == pytest.approx(40.0)
    assert merged_pulse.cortisol == pytest.approx(60.0)
    assert merged_pulse.oxytocin == pytest.approx(20.0)


def test_returned_pulse_is_a_deep_copy_of_the_cold_side() -> None:
    cold = make_aware_snapshot("cold", last_decay_at=HOT_LDA)
    hot = make_aware_snapshot("hot", last_decay_at=HOT_LDA)
    cold_pulse = make_pulse(delayed_impact={"cortisol": 2.0})

    _, merged_pulse = merge_cross_domain_state(cold, cold_pulse, hot, make_pulse(), now=NOW_24H)

    assert merged_pulse == cold_pulse
    assert merged_pulse is not cold_pulse
    # The nested dict is copied too: mutating the result cannot reach the input.
    merged_pulse.delayed_impact["cortisol"] = 99.0
    assert cold_pulse.delayed_impact == {"cortisol": 2.0}
