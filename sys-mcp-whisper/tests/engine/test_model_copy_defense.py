"""Round-trip defence: production writes must never bypass field validation.

pydantic v2's ``model_copy(update=...)`` does not validate: it writes the given
values straight into ``__dict__``.  Measured before this card changed anything::

    TensionState(value=50).model_copy(update={"value": 110})  -> value == 110.0
    TensionState.model_validate({"value": 110})               -> ValidationError

The engine had three such writes (``router.py``: cold-tier sanitisation, lazy
settlement, cross-domain merge).  All three now round-trip through
``model_validate(obj.model_dump() | {...})``, matching the precedent that
``engine/reconciliation.py`` set from the start.

These tests are behavioural, not structural: each one injects an out-of-range
value into an INPUT object (via ``model_construct``, which is the only way to
build a deliberately illegal model) and asserts that the FUNCTION refuses to
produce an illegal OUTPUT.  A structural test would only prove the call reads
"model_validate" today; this proves the model's own bounds actually fire.

The last two cases lock the checker script's rule in both directions, so a
future edit cannot quietly widen the whitelist or start flagging
``model_copy(deep=True)`` (which never introduces a new value and is legal).
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.core.domain.global_context import PulseState
from packages.core.domain.state import IPCState, S1Snapshot
from packages.core.engine.router import (
    merge_cross_domain_state,
    sanitize_tier_state,
    settle_before_tier_upgrade,
)

TS = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
CHECKER = Path(__file__).resolve().parents[3] / "scripts" / "check_model_copy.py"


def _snapshot(*, tier: str = "cold", affiliation: float = 40.0) -> S1Snapshot:
    """A snapshot built with ``model_validate`` -- every value legal."""
    return S1Snapshot.model_validate(
        {
            "version": 1,
            "scope_id": "chat_8842",
            "timestamp": TS,
            "last_decay_at": TS,
            "is_conflict": False,
            "compute_tier": tier,
            "ipc": {
                "affiliation": affiliation,
                "dominance": 0.0,
                "relationship_stage": "friend",
                "relationship_ceiling": "lover",
            },
            "traits": {},
            "emotions": {},
        }
    )


def _illegal_snapshot(*, tier: str = "cold", affiliation: float) -> S1Snapshot:
    """A snapshot whose ``ipc.affiliation`` is out of the -100..100 range.

    ``model_construct`` skips validation, which is exactly what an upstream bug
    looks like once it has already produced a bad object.
    """
    ipc = IPCState.model_construct(
        affiliation=affiliation,
        dominance=0.0,
        relationship_stage="friend",
        relationship_ceiling="lover",
    )
    return S1Snapshot.model_construct(
        version=1,
        scope_id="chat_8842",
        timestamp=TS,
        last_decay_at=TS,
        is_conflict=False,
        compute_tier=tier,
        ipc=ipc,
        traits={},
        emotions={},
    )


# ---------------------------------------------------------------------------
# 1: merge -- the blend can leave the legal range
# ---------------------------------------------------------------------------


def test_merge_rejects_an_out_of_range_blend_instead_of_stamping_it() -> None:
    """``0.7*150 + 0.3*30 = 114`` -- the round trip must refuse to build that.

    Under ``model_copy(update=...)`` the 114.0 was written silently; this is the
    case the C4 mutation (M3) restores, and it must turn red here.
    """
    cold = _illegal_snapshot(affiliation=150.0)
    hot = _snapshot(affiliation=30.0)

    with pytest.raises(ValidationError) as excinfo:
        merge_cross_domain_state(cold, PulseState(), hot, PulseState(), now=TS)

    errors = excinfo.value.errors()
    assert errors[0]["loc"] == ("ipc", "affiliation")
    assert "less than or equal to 100" in errors[0]["msg"]


def test_merge_still_accepts_an_in_range_blend() -> None:
    """The rejection above must not have made the legal path unusable."""
    cold = _snapshot(affiliation=40.0)
    hot = _snapshot(affiliation=80.0)

    merged, _pulse = merge_cross_domain_state(cold, PulseState(), hot, PulseState(), now=TS)

    assert merged.ipc.affiliation == pytest.approx(0.7 * 40.0 + 0.3 * 80.0)
    assert merged.ipc.affiliation == pytest.approx(52.0)
    assert merged.compute_tier == "cold"
    assert merged.last_decay_at == TS


# ---------------------------------------------------------------------------
# 2: settle -- the stamped tier must be a legal one
# ---------------------------------------------------------------------------


def test_settle_returns_a_validated_snapshot() -> None:
    settled, _pulse = settle_before_tier_upgrade(_snapshot(), PulseState(), TS, "warm")

    assert settled.compute_tier == "warm"
    assert settled.last_decay_at == TS
    # Re-validation is a no-op on a legal object: the round trip must be lossless.
    assert S1Snapshot.model_validate(settled.model_dump()) == settled


def test_settle_refuses_an_unknown_tier_before_touching_the_snapshot() -> None:
    with pytest.raises(ValueError, match="unknown compute tier"):
        settle_before_tier_upgrade(_snapshot(), PulseState(), TS, "boiling")


# ---------------------------------------------------------------------------
# 3: sanitize -- the constant False is legal, but the idiom is uniform
# ---------------------------------------------------------------------------


def test_sanitize_returns_a_validated_snapshot() -> None:
    sanitized = sanitize_tier_state(_snapshot(tier="cold"))

    assert sanitized.is_conflict is False
    assert S1Snapshot.model_validate(sanitized.model_dump()) == sanitized


def test_sanitize_passes_non_cold_tiers_through_unchanged() -> None:
    warm = _snapshot(tier="warm")
    assert sanitize_tier_state(warm) is warm


def test_sanitize_does_not_mutate_its_input() -> None:
    cold = _snapshot(tier="cold")
    before = cold.model_dump()
    sanitize_tier_state(cold)
    assert cold.model_dump() == before


# ---------------------------------------------------------------------------
# 4: the checker script -- both directions
# ---------------------------------------------------------------------------


def _load_checker():
    spec = importlib.util.spec_from_file_location("_check_model_copy", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checker_flags_an_update_call() -> None:
    """The rule fires on ``model_copy(update=...)``."""
    checker = _load_checker()
    tree = ast.parse(
        "def f(model):\n    return model.model_copy(update={'field': 1})\n"
    )
    violations = checker.find_unvalidated_updates(tree, "fake/module.py")
    assert len(violations) == 1
    assert "model_copy(update=...)" in violations[0]
    assert "model_validate" in violations[0]


def test_checker_allows_plain_and_deep_copies() -> None:
    """``model_copy()`` and ``model_copy(deep=True)`` carry no new value."""
    checker = _load_checker()
    tree = ast.parse(
        "def f(model):\n"
        "    a = model.model_copy()\n"
        "    b = model.model_copy(deep=True)\n"
        "    return a, b\n"
    )
    assert checker.find_unvalidated_updates(tree, "fake/module.py") == []


def test_the_checker_actually_scans_the_production_tree() -> None:
    """Non-vacuity guard: the scan must visit a plausible number of files.

    Without this, a filter bug that makes ``iter_py_files`` return nothing turns
    ``test_the_production_tree_is_clean`` into a tautology -- ``[] == []`` passes
    no matter what the tree contains.

    That bug was real (found in captain review of task-FFW-P28-C4): the nested-git
    filter ran ``any((parent / ".git").exists() for parent in path.parents if
    parent != ROOT)`` without the ``ROOT in parent.parents`` qualifier, so the
    workspace-root ``.git`` swallowed every file -- 0 scanned out of 25 in
    ``packages/``, and an injected ``model_copy(update={...})`` still reported PASS.

    The floor is deliberately loose (``>= 20``) so ordinary file moves do not
    redden it; its job is to catch "scanned nothing", not to pin an exact count.
    """
    checker = _load_checker()
    scanned = [
        path
        for root_name in checker.SCAN_ROOTS
        for path in checker.iter_py_files(checker.ROOT / root_name)
    ]
    assert len(scanned) >= 20, (
        f"check_model_copy scanned only {len(scanned)} files: the scan set has "
        f"collapsed, so 'production tree is clean' proves nothing"
    )
    # Every scanned file must live under one of the declared roots.
    for path in scanned:
        rel = path.relative_to(checker.ROOT).as_posix()
        assert any(rel.startswith(name + "/") for name in checker.SCAN_ROOTS), rel


def test_the_production_tree_is_clean() -> None:
    """No ``model_copy(update=...)`` anywhere under packages/ or apps/."""
    checker = _load_checker()
    assert checker.scan() == []


def test_the_whitelist_is_empty_and_documented() -> None:
    """An exemption must be a recorded ruling, never a convenience.

    The roster is empty today; if it ever gains an entry, this test makes the
    author state why -- the assertion message is the reminder.
    """
    checker = _load_checker()
    assert not checker.ALLOWED_UPDATE_SITES, (
        "a new model_copy(update=...) exemption was added; each entry needs a "
        "one-line justification next to it, and the card that granted it"
    )
