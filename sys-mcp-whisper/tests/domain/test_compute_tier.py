"""compute_tier field on S1Snapshot (task-FFW-P25-T1).

Written before the field exists: the first run must fail on the default-value
assertion, and only turns green once ``state.py`` carries the field.

The card's 防呆 note applies to case 5: ``extra="forbid"`` is already locked for
every domain model by the reflection lock in ``test_contracts.py``
(``test_every_domain_model_forbids_extra_fields``), which picks up
``compute_tier`` automatically.  Case 5 below is a *behavioural* supplement --
it names ``compute_tier2`` on the real snapshot -- not a duplicate lock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.core.domain.state import S1Snapshot

TS = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
LAST = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


def make_snapshot(**overrides: object) -> S1Snapshot:
    """Minimal valid snapshot; ``overrides`` replace or extend the payload."""
    payload: dict[str, object] = {
        "version": 1,
        "scope_id": "chat_8842",
        "timestamp": TS,
        "last_decay_at": LAST,
        "is_conflict": False,
        "ipc": {
            "affiliation": 40.0,
            "dominance": 60.0,
            "relationship_stage": "friend",
            "relationship_ceiling": "lover",
        },
        "traits": {"tsundere": {"score": 72, "visibility": "unlocked"}},
        "emotions": {},
    }
    payload.update(overrides)
    return S1Snapshot.model_validate(payload)


def test_compute_tier_defaults_to_warm() -> None:
    # Locks human ruling 2: the default is "warm", not the task sheet's "cold".
    # Every existing assemble_prompt assertion runs the normal path, so a "cold"
    # default would silently push all of them down the minimal branch.
    assert make_snapshot().compute_tier == "warm"
    assert S1Snapshot.model_fields["compute_tier"].default == "warm"


@pytest.mark.parametrize("tier", ["cold", "warm", "hot"])
def test_compute_tier_accepts_all_three_tiers(tier: str) -> None:
    assert make_snapshot(compute_tier=tier).compute_tier == tier


@pytest.mark.parametrize("bad", ["Cold", "warmm", "neutral", ""])
def test_compute_tier_rejects_unknown_tiers(bad: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_snapshot(compute_tier=bad)
    assert excinfo.value.errors()[0]["loc"] == ("compute_tier",)


@pytest.mark.parametrize("bad", [None, 0])
def test_compute_tier_rejects_non_strings(bad: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_snapshot(compute_tier=bad)
    assert excinfo.value.errors()[0]["loc"] == ("compute_tier",)


def test_compute_tier_does_not_relax_extra_forbid() -> None:
    # Behavioural supplement to the domain-wide reflection lock: adding a field
    # must not be read as a licence to widen extra="forbid" on S1Snapshot.
    with pytest.raises(ValidationError) as excinfo:
        make_snapshot(compute_tier2="hot")
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


def test_compute_tier_is_a_structural_snapshot_path() -> None:
    # S1Snapshot.valid_paths() is the structural inventory of the snapshot, not
    # the delta whitelist: ADR-008 section 1.8 moved the latter to
    # ``global_context.DELTA_ELIGIBLE_PATHS``.  compute_tier is persisted state
    # derived per request, so it belongs in the inventory; membership is
    # asserted separately because a count-only lock cannot see a swap.
    paths = S1Snapshot.valid_paths()
    assert len(paths) == 13, f"快照路径数变了: {sorted(paths)}"
    assert "compute_tier" in paths


def test_s1_snapshot_stays_mutable() -> None:
    # S1 is a mutable working snapshot; only EmotionalEvent and RoutingResult are
    # frozen.  Guard against copy-pasting frozen=True in with the new field.
    assert S1Snapshot.model_config.get("frozen") is False
