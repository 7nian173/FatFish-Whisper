"""settle_before_tier_upgrade tests, rewritten for P2.8 (ADR-008 section 1.7).

Lazy settlement: a cold snapshot's pending decay is applied exactly once, at
the moment of a tier upgrade out of cold.  Every non-upgrade path must return
the very same objects (``is`` identity) for BOTH carriers -- that is the "not
on every request" guarantee.

P2.8 moved the three hormone lanes to ``GlobalContext.pulse`` (ADR-008 section
1.7), so the function now takes a pulse in and returns ``(snapshot, pulse)``.
Each test unpacks both and asserts on both: the hormones settle on the pulse
while ``ipc.affiliation`` settles on the snapshot, and a version that settled
only one carrier would keep the other at its pre-idle value in silence.

Anchor note (P2.5 Bug 1, fixed 2026-11-06): ``apply_time_decay`` is a pure
function with no clock, so it stamps ``last_decay_at = snapshot.timestamp``.
``settle_before_tier_upgrade`` owns the caller-supplied ``now`` and must
re-stamp the anchor to it; otherwise the NEXT upgrade recomputes the span
already settled, over-counting by ``now - timestamp`` every round.  The
fixture keeps ``timestamp != last_decay_at`` so a wrong anchor is visible.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from packages.core.domain.global_context import PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine import settle_before_tier_upgrade

TS = datetime(2026, 1, 1, tzinfo=UTC)  # snapshot timestamp
LDA = datetime(2026, 1, 2, tzinfo=UTC)  # last decayed one day after creation
NOW_7D = LDA + timedelta(days=7)  # the human acceptance span
NOW_24H = LDA + timedelta(hours=24)
TTL = datetime(2030, 1, 1, tzinfo=UTC)


def make_cold_snapshot(*, affiliation: float = 21.0, tier: str = "cold") -> S1Snapshot:
    return S1Snapshot(
        version=1,
        scope_id="scope-cold",
        timestamp=TS,
        last_decay_at=LDA,
        is_conflict=True,
        compute_tier=tier,
        ipc=IPCState(
            affiliation=affiliation,
            dominance=12.5,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"warmth": TraitState(score=70, visibility="unlocked")},
        emotions={"anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL)},
    )


def make_cold_pulse(
    *, dopamine: float = 80.0, cortisol: float = 60.0, oxytocin: float = 40.0
) -> PulseState:
    return PulseState(dopamine=dopamine, cortisol=cortisol, oxytocin=oxytocin)


def test_cold_to_hot_after_seven_idle_days_settles_then_upgrades() -> None:
    cold = make_cold_snapshot()
    pulse = make_cold_pulse()
    upgraded, upgraded_pulse = settle_before_tier_upgrade(cold, pulse, NOW_7D, "hot")

    assert upgraded.compute_tier == "hot"
    # 168h at 1.0/h towards 50: dopamine 80, cortisol 60, oxytocin 40 all cap
    # at 50.0 -- this settles on the PULSE, not on the snapshot.
    assert upgraded_pulse.dopamine == 50.0
    assert upgraded_pulse.cortisol == 50.0
    assert upgraded_pulse.oxytocin == 50.0
    # 168h at 0.5/h towards 0: affiliation 21.0 caps at 0.0 -- on the SNAPSHOT.
    assert upgraded.ipc.affiliation == 0.0
    # The settlement owns `now`, so the anchor advances to it (P2.5 Bug 1).
    assert upgraded.last_decay_at == NOW_7D
    assert upgraded.last_decay_at != TS


def test_cold_to_warm_settles_with_the_same_math() -> None:
    cold = make_cold_snapshot(affiliation=-10.0)
    pulse = make_cold_pulse(dopamine=80.0, cortisol=40.0, oxytocin=50.0)
    upgraded, upgraded_pulse = settle_before_tier_upgrade(cold, pulse, NOW_24H, "warm")

    assert upgraded.compute_tier == "warm"
    # 24h at 1.0/h towards 50: 80 - 24 = 56.0; 40 + min(24, 10) = 50.0 (cap).
    assert upgraded_pulse.dopamine == 56.0
    assert upgraded_pulse.cortisol == 50.0
    assert upgraded_pulse.oxytocin == 50.0
    # 24h at 0.5/h towards 0: -10 + min(12, 10) = 0.0 (cap).
    assert upgraded.ipc.affiliation == 0.0
    assert upgraded.last_decay_at == NOW_24H


def test_cold_to_cold_same_tier_returns_same_objects_without_settling() -> None:
    cold = make_cold_snapshot()
    pulse = make_cold_pulse()
    result, result_pulse = settle_before_tier_upgrade(cold, pulse, NOW_7D, "cold")

    assert result is cold
    assert result_pulse is pulse
    assert result.last_decay_at == LDA
    assert result_pulse.dopamine == 80.0


def test_warm_to_hot_does_not_settle() -> None:
    warm = make_cold_snapshot(tier="warm")
    pulse = make_cold_pulse()
    result, result_pulse = settle_before_tier_upgrade(warm, pulse, NOW_7D, "hot")

    assert result is warm
    assert result_pulse is pulse
    assert result.compute_tier == "warm"
    assert result_pulse.dopamine == 80.0


def test_hot_to_cold_downgrade_does_not_settle() -> None:
    hot = make_cold_snapshot(tier="hot")
    pulse = make_cold_pulse()
    result, result_pulse = settle_before_tier_upgrade(hot, pulse, NOW_7D, "cold")

    assert result is hot
    assert result_pulse is pulse
    assert result.compute_tier == "hot"
    assert result.last_decay_at == LDA


def test_unknown_new_tier_raises_value_error() -> None:
    cold = make_cold_snapshot()
    with pytest.raises(ValueError, match="unknown compute tier"):
        settle_before_tier_upgrade(cold, make_cold_pulse(), NOW_7D, "whatever")


def test_negative_delta_t_raises_value_error() -> None:
    cold = make_cold_snapshot()
    with pytest.raises(ValueError, match="clock ran backwards"):
        settle_before_tier_upgrade(cold, make_cold_pulse(), LDA - timedelta(hours=1), "hot")


def test_inputs_are_never_modified() -> None:
    cold = make_cold_snapshot()
    pulse = make_cold_pulse()
    before = cold.model_dump(mode="json")
    pulse_before = pulse.model_dump(mode="json")

    settle_before_tier_upgrade(cold, pulse, NOW_7D, "hot")
    settle_before_tier_upgrade(cold, pulse, NOW_7D, "warm")
    with contextlib.suppress(ValueError):
        settle_before_tier_upgrade(cold, pulse, NOW_7D, "whatever")

    assert cold.model_dump(mode="json") == before
    assert pulse.model_dump(mode="json") == pulse_before


def test_settled_pulse_carries_delayed_impact_verbatim() -> None:
    """The pulse is rebuilt field by field; a dropped dict would be silent.

    ``delayed_impact`` is not decayed (it holds in-flight HPA impulses), but it
    must survive the rebuild.  A rebuild that omitted it would fall back to the
    empty default and lose every pending impulse at the moment of a tier
    upgrade -- exactly the window where they matter most.
    """
    cold = make_cold_snapshot()
    pulse = make_cold_pulse(dopamine=70.0)

    _, settled_pulse = settle_before_tier_upgrade(cold, pulse, NOW_24H, "warm")

    assert settled_pulse.delayed_impact == {}
    assert settled_pulse is not pulse


def test_two_consecutive_settlements_do_not_double_count_the_span() -> None:
    """The anchor must advance, or the next upgrade re-settles the same span.

    P2.5 Bug 1 regression lock.  ``apply_time_decay`` is pure and has no clock,
    so it stamps ``snapshot.timestamp``.  Had ``settle_before_tier_upgrade``
    passed that through, round 2 would compute ``now2 - timestamp`` instead of
    ``now2 - now1`` and silently decay the same idle window twice.

    Measured before the fix (timestamp Jan 1, last_decay_at Jan 2): round 1
    over 7 idle days stamped Jan 1 back onto the snapshot, then round 2 over
    one more day computed a 3-day delta -- over-count exactly ``now1 - timestamp``.

    Only the anchor is asserted on the snapshot, on purpose: a saturated
    hormone cannot distinguish the two regimes.  The pulse is checked too, so
    that the anchor fix cannot be applied to one carrier only.
    """
    cold = make_cold_snapshot(affiliation=21.0)
    pulse = make_cold_pulse()

    first, first_pulse = settle_before_tier_upgrade(cold, pulse, NOW_7D, "hot")
    assert first.last_decay_at == NOW_7D
    assert first_pulse.dopamine == 50.0

    # Round 2 starts from the settled state, back in cold (a re-entry into a
    # quiet domain) one day later.
    back_to_cold = first.model_copy(update={"compute_tier": "cold"})
    later = NOW_7D + timedelta(days=1)
    second, second_pulse = settle_before_tier_upgrade(back_to_cold, first_pulse, later, "hot")

    # The discriminating assertion: with the pass-through bug this stays at TS
    # (Jan 1), so round 2 would have decayed 8 days instead of 1.
    assert second.last_decay_at == later
    # Saturated values are unaffected either way -- assert them to document
    # that the double-count is silent rather than visible in the numbers.
    assert second_pulse.dopamine == 50.0
    assert second_pulse.cortisol == 50.0
