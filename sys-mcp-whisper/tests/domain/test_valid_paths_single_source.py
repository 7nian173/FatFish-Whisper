"""The delta whitelist must have exactly one declaration (ADR-008 section 1.8).

History: the whitelist used to be *derived* -- ``S1Snapshot.valid_paths()``
walked ``model_fields`` and both ``domain/contracts.py`` and ``engine/judge.py``
called it.  P2.8 replaced that with a literal constant,
``global_context.DELTA_ELIGIBLE_PATHS``, for two reasons the old design could
not satisfy:

* the derivation could not be narrowed -- it returned every snapshot field,
  including ``timestamp`` and ``scope_id``, which cannot be summed; and
* once the hormones left ``S1Snapshot`` (section 1.7) the derivation stopped
  covering three of the five paths a delta may legitimately address.

The risk this file guards is the same one it guarded before: two check sites
that agree today by luck.  So the assertions are structural (AST), not merely
behavioural -- a test that compared two outputs would pass against two
independent copies that happen to match.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.core.domain import contracts
from packages.core.domain.contracts import RoutingResult
from packages.core.domain.global_context import DELTA_ELIGIBLE_PATHS
from packages.core.engine import judge

SITES = {
    "contracts": contracts,
    "judge": judge,
}


def test_the_whitelist_is_a_literal_of_five_paths() -> None:
    assert isinstance(DELTA_ELIGIBLE_PATHS, frozenset)
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


def test_the_snapshot_is_not_the_whitelist_any_more() -> None:
    """The two inventories must not be confused again.

    ``valid_paths()`` stays as a structural inventory (13 paths since
    P2.8-C3 added ``last_active_at``; it was 12 after the hormone block
    left), and it is deliberately *not* the delta whitelist.
    """
    from packages.core.domain.state import S1Snapshot

    structural = S1Snapshot.valid_paths()

    assert len(structural) == 13
    assert "timestamp" in structural
    assert "timestamp" not in DELTA_ELIGIBLE_PATHS
    assert "pulse.dopamine" not in structural
    assert "pulse.dopamine" in DELTA_ELIGIBLE_PATHS


@pytest.mark.parametrize("site_name", sorted(SITES))
def test_every_check_site_reads_the_constant(site_name: str) -> None:
    """AST lock: each site must name ``DELTA_ELIGIBLE_PATHS``, not rebuild it.

    A behavioural test cannot see a locally rebuilt set that happens to agree
    today -- that is exactly the R9 / GUARD trap.  Requiring the name makes the
    second source of truth impossible to introduce silently.
    """
    module = ast.parse(Path(SITES[site_name].__file__).read_text(encoding="utf-8"))

    names = {node.id for node in ast.walk(module) if isinstance(node, ast.Name)}
    assert "DELTA_ELIGIBLE_PATHS" in names, f"{site_name} must reference the constant"

    rebuilt = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_valid_field_paths"
    ]
    assert rebuilt == [], f"{site_name} must not carry its own whitelist builder"

    loops = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.For, ast.While))
        and isinstance(node.iter, ast.Attribute)
        and node.iter.attr == "model_fields"
    ]
    assert loops == [], f"{site_name} must not walk model_fields to build a whitelist"


def test_no_site_still_calls_the_removed_helper() -> None:
    for site_name, module in SITES.items():
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "_valid_field_paths(" not in source, f"{site_name} still calls the removed helper"


def test_both_check_sites_reject_the_same_unknown_key() -> None:
    """End-to-end agreement at the call sites, not just at the constant.

    The two consumers are ``RoutingResult.stimulus_delta`` (domain) and
    ``HormoneDeltas.values`` (engine).
    """
    bad_key = {"hormones.dopamin": 1.0}  # retired spelling, missing the final 'e'

    with pytest.raises(ValidationError, match=r"unknown delta key: hormones\.dopamin"):
        judge.HormoneDeltas(values=bad_key)

    with pytest.raises(ValidationError, match=r"unknown delta key: hormones\.dopamin"):
        RoutingResult(
            stimulus_delta=bad_key,
            scenario_tag="neutral",
            source="track_a",
            route_id="r1",
            matched_scope="private",
        )


def test_both_check_sites_reject_a_non_numeric_snapshot_path() -> None:
    """``timestamp`` is a legal snapshot path and an illegal delta path.

    This is the case the old derivation could not express: it accepted every
    snapshot field, so a stimulus could try to add to a ``datetime``.
    """
    bad_key = {"timestamp": 1.0}

    with pytest.raises(ValidationError, match=r"unknown delta key: timestamp"):
        judge.HormoneDeltas(values=bad_key)

    with pytest.raises(ValidationError, match=r"unknown delta key: timestamp"):
        RoutingResult(
            stimulus_delta=bad_key,
            scenario_tag="neutral",
            source="track_a",
            route_id="r1",
            matched_scope="private",
        )
