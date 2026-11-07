"""Compute-tier gating tests (task-FFW-P25-T3).

Covers the human rulings of 2026-11-06:

* ``COLD_NARRATION`` is the verbatim human-fixed cold-tier narration;
* ``should_call_track_b`` -- only "hot" is allowed on Track B (natural name
  semantics), unknown tiers raise instead of silently degrading;
* ``sanitize_tier_state`` -- cold forces ``is_conflict=False`` so the
  assembler can never read a stale True, input snapshot never mutated;
* cold-tier assembly returns the narration verbatim -- before any lexicon
  lookup or three-segment assembly -- for both ``pure`` values;
* hot/warm snapshots walk the full three-segment path, proven with the same
  snapshot differing only in ``compute_tier``.

The engine purity AST guards in test_dynamics.py automatically scan
router.py's new imports (typing.Final, ..domain.state.S1Snapshot are both
whitelisted), so no duplicate guard lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine import (
    COLD_NARRATION,
    VALID_TIERS,
    assemble_prompt,
    sanitize_tier_state,
    should_call_track_b,
)

TS = datetime(2026, 11, 6, 0, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {"anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL)}


def make_snapshot(tier: str, *, conflicted: bool = False) -> S1Snapshot:
    return S1Snapshot(
        version=1,
        scope_id="scope-gate",
        timestamp=TS,
        last_decay_at=TS,
        is_conflict=conflicted,
        compute_tier=tier,
        ipc=IPCState(
            affiliation=0.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={
            "tsundere": TraitState(score=85, visibility="unlocked"),
            "shyness": TraitState(score=30, visibility="dormant"),
        },
        emotions=EMOTIONS,
    )


# ---------------------------------------------------------------------------
# 1. COLD_NARRATION: verbatim human ruling, length cap
# ---------------------------------------------------------------------------


def test_cold_narration_is_verbatim_human_ruling() -> None:
    assert COLD_NARRATION == "[系统提示：未唤醒] 角色当前处于未深度参与的旁观状态。"
    assert len(COLD_NARRATION) <= 40


def test_valid_tiers_shape() -> None:
    assert VALID_TIERS == ("cold", "warm", "hot")


# ---------------------------------------------------------------------------
# 2. should_call_track_b: only hot calls Track B (asserted separately)
# ---------------------------------------------------------------------------


def test_should_call_track_b_hot_is_true() -> None:
    assert should_call_track_b("hot") is True


def test_should_call_track_b_cold_is_false() -> None:
    assert should_call_track_b("cold") is False


def test_should_call_track_b_warm_is_false() -> None:
    assert should_call_track_b("warm") is False


# ---------------------------------------------------------------------------
# 3. unknown tier raises; legal values do not (anti-tautology)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_tier", ["Cold", "hot ", "warmm", ""])
def test_should_call_track_b_rejects_unknown_tiers(bad_tier: str) -> None:
    with pytest.raises(ValueError):
        should_call_track_b(bad_tier)


def test_should_call_track_b_legal_values_do_not_raise() -> None:
    for tier in VALID_TIERS:
        should_call_track_b(tier)  # must not raise


# ---------------------------------------------------------------------------
# 4./5./8. cold-tier assembly: narration only, no segments, lexicon-free
# ---------------------------------------------------------------------------


def test_cold_assembly_returns_narration_without_segment_labels() -> None:
    prompt = assemble_prompt(make_snapshot("cold", conflicted=True), pure=True)
    assert prompt == COLD_NARRATION
    assert "[主场]" not in prompt
    assert "[拉扯]" not in prompt
    assert "[旁边杵着]" not in prompt


def test_cold_assembly_ignores_lexicon_entirely() -> None:
    """Empty lexicon: no exception, same narration -- proof it never looks up."""
    assert assemble_prompt(make_snapshot("cold"), lexicon={}, pure=True) == COLD_NARRATION


def test_cold_assembly_unsealed_variant_returns_same_narration() -> None:
    assert assemble_prompt(make_snapshot("cold"), pure=False) == COLD_NARRATION


# ---------------------------------------------------------------------------
# 6. hot vs warm on the same snapshot: only the tier differs
# ---------------------------------------------------------------------------


def test_hot_and_warm_tiers_walk_full_assembly() -> None:
    base = make_snapshot("warm")
    hot = base.model_copy(update={"compute_tier": "hot"})
    for tier_snapshot in (hot, base):
        prompt = assemble_prompt(tier_snapshot, pure=False)
        assert prompt != COLD_NARRATION
        assert "[主场]" in prompt
        assert "[旁边杵着]" in prompt


def test_hot_vs_warm_identical_output() -> None:
    base = make_snapshot("warm")
    hot = base.model_copy(update={"compute_tier": "hot"})
    assert assemble_prompt(hot, pure=False) == assemble_prompt(base, pure=False)


# ---------------------------------------------------------------------------
# 7. sanitize_tier_state: cold forces is_conflict=False, input untouched
# ---------------------------------------------------------------------------


def test_sanitize_cold_forces_is_conflict_false() -> None:
    original = make_snapshot("cold", conflicted=True)
    sanitized = sanitize_tier_state(original)
    assert sanitized.is_conflict is False
    assert sanitized.compute_tier == "cold"


def test_sanitize_hot_and_warm_keep_is_conflict_true() -> None:
    for tier in ("hot", "warm"):
        snapshot = make_snapshot(tier, conflicted=True)
        assert sanitize_tier_state(snapshot).is_conflict is True


def test_sanitize_does_not_mutate_its_input() -> None:
    original = make_snapshot("cold", conflicted=True)
    sanitize_tier_state(original)
    assert original.is_conflict is True


def test_sanitize_non_cold_returns_same_object() -> None:
    snapshot = make_snapshot("warm", conflicted=True)
    assert sanitize_tier_state(snapshot) is snapshot
