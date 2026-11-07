"""Lock every ``Field`` constraint declared in the domain package (task-FFW-P0-GUARD).

Why this file exists: before it, the domain package declared 41 field constraints
(``ge`` / ``le`` / ``min_length`` / ``max_length`` / ``pattern``) but only 10 of
them were actually guarded by a test -- relaxing the other 31 kept the whole suite
green.  The table now holds 55 items / 24 previously guarded: P0-C6 added
``RoutingResult`` with two ``min_length`` constraints and P2.8-C1 added
``global_context`` with ten, both arriving already guarded.  The historical
31 stays 31 -- that is history, not a running total.

The sharpest evidence was an asymmetry *inside one model*:
``IPCState.affiliation`` had ``ge=-100, le=100`` and was guarded, while
``IPCState.dominance`` had the same constraint and was not.

Two layers, deliberately separate:

1. ``EXPECTED_CONSTRAINTS`` is a hand-written table and the single source of
   truth.  A pure "discover whatever the code says" lock would be worthless:
   change ``le=100`` to ``le=10**9`` and the lock follows the code and stays
   green.  Here the code must match the table, and the table is pinned to the
   contract / P0 rulings by a comment on every entry.
2. Every constraint is also exercised *behaviourally*, through pydantic, with a
   value on each side of the boundary.  Metadata comparison alone cannot prove
   that pydantic still honours the metadata, and a "reject everything"
   implementation would pass a reject-only assertion -- so each case asserts the
   in-bound value constructs **and** the out-of-bound value raises.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import UTC, datetime
from typing import Any, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from packages.core.domain.contracts import (
    ActionIntent,
    CandidateProposal,
    EmotionalEvent,
    MCPRequest,
    MCPRequestContext,
    MCPResponse,
    ProposalItem,
    RoutingResult,
    UserProfileRef,
)
from packages.core.domain.global_context import (
    BaselineState,
    GlobalContext,
    PulseState,
    TensionState,
)
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)

_DOMAIN_PACKAGE = "packages.core.domain"
_CONSTRAINT_KEYS = ("ge", "le", "gt", "lt", "min_length", "max_length", "pattern")

_AWARE = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Reflection: discover models and extract their declared constraints
# ---------------------------------------------------------------------------


def _discover_domain_models() -> list[type[BaseModel]]:
    """Collect every pydantic model defined under the domain package.

    Modules come from ``pkgutil.iter_modules`` rather than a hand-written
    tuple.  P2.5's N4 raised the idea; the ruling was that the right moment
    is when a third domain module appears.  ``global_context`` is that third
    module, and the hand-written list had already proven it drops newcomers:
    that whole module was invisible to every lock in this file when it
    landed, while the frozen roster here silently disagreed with the code.

    ``contracts.py`` does ``from .state import S1Snapshot``, so a plain walk
    yields the same class twice; dedupe by ``id()`` and keep the module filter
    so imported third-party models cannot sneak in.
    """
    unique: dict[int, type[BaseModel]] = {}
    domain_package = importlib.import_module(_DOMAIN_PACKAGE)
    for info in pkgutil.iter_modules(domain_package.__path__):
        module = importlib.import_module(f"{_DOMAIN_PACKAGE}.{info.name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if not hasattr(obj, "model_fields"):
                continue
            if not obj.__module__.startswith("packages"):
                continue
            unique.setdefault(id(obj), obj)
    return list(unique.values())


def _extract_constraints(model: type[BaseModel], field_name: str) -> dict[str, Any]:
    """Pull ``ge/le/gt/lt/min_length/max_length/pattern`` out of a field."""
    extracted: dict[str, Any] = {}
    for item in model.model_fields[field_name].metadata:
        for key in _CONSTRAINT_KEYS:
            value = getattr(item, key, None)
            if value is not None:
                extracted[key] = value
    return extracted


_MODEL_BY_NAME: dict[str, type[BaseModel]] = {
    model.__name__: model for model in _discover_domain_models()
}

_EXTRACTED: dict[str, dict[str, Any]] = {}
for _model in _discover_domain_models():
    for _field_name in _model.model_fields:
        _constraints = _extract_constraints(_model, _field_name)
        if _constraints:
            _EXTRACTED[f"{_model.__name__}.{_field_name}"] = _constraints


# ---------------------------------------------------------------------------
# The expected table -- the single source of truth
#
# Keys are "Model.field", values are the constraint dict that must be declared.
# Each entry carries the origin of its numbers.  Adding a constrained field
# without adding it here turns the reverse-direction lock red on purpose.
# ---------------------------------------------------------------------------

EXPECTED_CONSTRAINTS: dict[str, dict[str, Any]] = {
    # contract 3.1 -- request_id is the host-generated id echoed back in 3.3.
    "MCPRequest.request_id": {"min_length": 1},
    # contract 3.1 -- user_id scopes the per-user IPC state.
    "MCPRequest.user_id": {"min_length": 1},
    # contract 3.1 -- scope_id is the dual-track isolation key (private/group).
    "MCPRequest.scope_id": {"min_length": 1},
    # contract 3.3 -- an action intent without an emotion label is meaningless.
    "ActionIntent.emotion": {"min_length": 1},
    # contract 3.3 -- sticker tag drives the host-side sticker lookup.
    "ActionIntent.sticker_tag": {"min_length": 1},
    # contract 3.3 -- response echoes the request id, so it cannot be empty.
    "MCPResponse.request_id": {"min_length": 1},
    # P2.5 task-FFW-P25-T4 -- typing_delay_ms is an output-direction field:
    # the engine computes the delay and the host only reads it.  Two items
    # (ge, le), not one: the 0..5000 window is a pair of bounds.
    "MCPResponse.typing_delay_ms": {"ge": 0, "le": 5000},
    # contract 3.4 -- proposal category groups the evolution proposal.
    "ProposalItem.category": {"min_length": 1},
    # contract 3.4 -- dotted config path; lowercase segments joined by ".".
    "ProposalItem.target": {"pattern": r"^[a-z_]+(\.[a-z_]+)+$"},
    # contract 3.4 -- a proposal must state its human-readable rationale.
    "ProposalItem.reason": {"min_length": 1},
    # contract 3.4 -- confidence is a probability.
    "ProposalItem.confidence": {"ge": 0.0, "le": 1.0},
    # contract 3.4 -- candidate id is the approval handle.
    "CandidateProposal.candidate_id": {"min_length": 1},
    # contract 3.4 -- baseline hash is compared before write to detect drift.
    "CandidateProposal.baseline_hash": {"min_length": 1},
    # contract 3.4 -- a candidate with zero proposals is not a candidate.
    "CandidateProposal.proposals": {"min_length": 1},
    # P0-C4 ruling -- the degraded-router tag cache is keyed by user_id.
    "UserProfileRef.user_id": {"min_length": 1},
    # P0-C4 ruling -- at most 8 fallback tags (word list itself belongs to P3).
    "UserProfileRef.cached_tags": {"max_length": 8},
    # plan section 5 (G5) -- event id is producer-generated, must be non-empty.
    "EmotionalEvent.id": {"min_length": 1},
    # plan section 5 (G5) -- emotion key; the vocabulary belongs to P3.
    "EmotionalEvent.emotion_key": {"min_length": 1},
    # plan section 5 (G5) -- same 0..100 scale as EmotionValue.intensity.
    "EmotionalEvent.intensity": {"ge": 0, "le": 100},
    # plan section 5 (G5) -- summary is 1..200 code points, no strip check.
    "EmotionalEvent.summary": {"min_length": 1, "max_length": 200},
    # plan section 5 (G5) -- scope_id drives the private/group filter.
    "EmotionalEvent.scope_id": {"min_length": 1},
    # contract 3.2 ipc -- affiliation decays toward 0 inside [-100, 100].
    "IPCState.affiliation": {"ge": -100, "le": 100},
    # contract 3.2 ipc -- dominance shares the affiliation scale by symmetry.
    "IPCState.dominance": {"ge": -100, "le": 100},
    # contract 3.2 ipc -- relationship stage label, non-empty.
    "IPCState.relationship_stage": {"min_length": 1},
    # contract 3.2 ipc -- ceiling bounds evolution proposals, non-empty.
    "IPCState.relationship_ceiling": {"min_length": 1},
    # contract 3.2 hormones -- global pool on the 0..100 scale, baseline 50.
    "PulseState.dopamine": {"ge": 0.0, "le": 100.0},
    "PulseState.cortisol": {"ge": 0.0, "le": 100.0},
    "PulseState.oxytocin": {"ge": 0.0, "le": 100.0},
    # contract 3.2 traits -- trait axis score on the 0..100 scale.
    "TraitState.score": {"ge": 0, "le": 100},
    # contract 3.2 emotions -- surface intensity on the same 0..100 scale.
    "EmotionValue.intensity": {"ge": 0, "le": 100},
    # contract 3.2 snapshot -- structure version starts at 1.
    "S1Snapshot.version": {"ge": 1},
    # contract 3.2 snapshot -- snapshot is scoped to one conversation domain.
    "S1Snapshot.scope_id": {"min_length": 1},
    # plan-p2-routing section 1 -- idempotency key for one routing round; an
    # empty key cannot be compared, so non-empty is structural, not cosmetic.
    "RoutingResult.route_id": {"min_length": 1},
    # plan-p2-routing section 1 -- required posture label; values outside the
    # vocabulary are rejected by the model's own validator, this row only pins
    # that the field cannot be blank.
    "RoutingResult.scenario_tag": {"min_length": 1},
    # ADR-008 section 5 / P2.8-C1 -- a heat modifier of zero would mute the
    # persona entirely, so the bound is strict rather than inclusive.
    "BaselineState.persona_heat_modifier": {"gt": 0.0},
    # P2.8-C1 -- the commit counter only moves forward, never negative.
    "GlobalContext.version": {"ge": 0},
    # ADR-008 section 3 -- tension is accumulated pressure on the same
    # 0..100 scale as the hormone axes it feeds later.
    "TensionState.value": {"ge": 0.0, "le": 100.0},
}


# ---------------------------------------------------------------------------
# Minimal valid instances -- one builder per constrained model
#
# model_construct() is deliberately avoided: it skips validation, which is
# exactly the thing under test here.
# ---------------------------------------------------------------------------


def _build_ipc(**overrides: Any) -> IPCState:
    payload: dict[str, Any] = {
        "affiliation": 10.0,
        "dominance": 5.0,
        "relationship_stage": "friend",
        "relationship_ceiling": "lover",
    }
    return IPCState(**{**payload, **overrides})


def _build_trait(**overrides: Any) -> TraitState:
    payload: dict[str, Any] = {"score": 50, "visibility": "unlocked"}
    return TraitState(**{**payload, **overrides})


def _build_emotion(**overrides: Any) -> EmotionValue:
    payload: dict[str, Any] = {"intensity": 50.0, "ttl_expires_at": _AWARE}
    return EmotionValue(**{**payload, **overrides})


def _build_snapshot(**overrides: Any) -> S1Snapshot:
    payload: dict[str, Any] = {
        "version": 1,
        "scope_id": "chat_8842",
        "timestamp": _AWARE,
        "last_decay_at": _AWARE,
        "is_conflict": False,
        "ipc": _build_ipc(),
        "traits": {"humor": _build_trait()},
        "emotions": {"joy": _build_emotion()},
    }
    return S1Snapshot(**{**payload, **overrides})


def _build_request(**overrides: Any) -> MCPRequest:
    payload: dict[str, Any] = {
        "request_id": "req_1",
        "user_id": "user_9527",
        "scope_id": "chat_8842",
        "message": "hi",
        "persona_card": "card",
        "chat_mode": "pure",
        "context": MCPRequestContext(last_interaction_at=_AWARE, current_time=_AWARE),
    }
    return MCPRequest(**{**payload, **overrides})


def _build_action_intent(**overrides: Any) -> ActionIntent:
    payload: dict[str, Any] = {"emotion": "annoyed", "sticker_tag": "angry"}
    return ActionIntent(**{**payload, **overrides})


def _build_response(**overrides: Any) -> MCPResponse:
    payload: dict[str, Any] = {
        "request_id": "req_1",
        "status": "success",
        "system_head": "seal text",
        "tail_message": "injection text",
        "state_snapshot": _build_snapshot(),
        "action_intent": _build_action_intent(),
    }
    return MCPResponse(**{**payload, **overrides})


def _build_proposal_item(**overrides: Any) -> ProposalItem:
    payload: dict[str, Any] = {
        "category": "threshold",
        "target": "ipc.affiliation",
        "old_value": 60,
        "new_value": 55,
        "reason": "too generous lately",
        "confidence": 0.5,
    }
    return ProposalItem(**{**payload, **overrides})


def _build_candidate(**overrides: Any) -> CandidateProposal:
    payload: dict[str, Any] = {
        "candidate_id": "cand_1",
        "baseline_hash": "sha256:0",
        "proposals": [_build_proposal_item()],
        "status": "pending_approval",
    }
    return CandidateProposal(**{**payload, **overrides})


def _build_profile_ref(**overrides: Any) -> UserProfileRef:
    payload: dict[str, Any] = {"user_id": "user_9527", "cached_tags": ("calm",)}
    return UserProfileRef(**{**payload, **overrides})


def _build_routing_result(**overrides: Any) -> RoutingResult:
    payload: dict[str, Any] = {
        "scenario_tag": "neutral",
        "source": "track_a",
        "route_id": "route_1",
        "matched_scope": "private",
    }
    return RoutingResult(**{**payload, **overrides})


def _build_event(**overrides: Any) -> EmotionalEvent:
    payload: dict[str, Any] = {
        "id": "evt_1",
        "emotion_key": "anger",
        "intensity": 40.0,
        "severity": "medium",
        "summary": "called me stupid",
        "visibility_scope": "private",
        "scope_id": "chat_8842",
        "created_at": _AWARE,
    }
    return EmotionalEvent(**{**payload, **overrides})


def _build_pulse(**overrides: Any) -> PulseState:
    payload: dict[str, Any] = {"dopamine": 50.0, "cortisol": 50.0, "oxytocin": 50.0}
    return PulseState(**{**payload, **overrides})


def _build_baseline(**overrides: Any) -> BaselineState:
    payload: dict[str, Any] = {"persona": "warm"}
    return BaselineState(**{**payload, **overrides})


def _build_tension(**overrides: Any) -> TensionState:
    payload: dict[str, Any] = {"value": 10.0}
    return TensionState(**{**payload, **overrides})


def _build_global_context(**overrides: Any) -> GlobalContext:
    payload: dict[str, Any] = {"version": 1}
    return GlobalContext(**{**payload, **overrides})


_BUILDERS: dict[str, Any] = {
    "IPCState": _build_ipc,
    "PulseState": _build_pulse,
    "TraitState": _build_trait,
    "EmotionValue": _build_emotion,
    "S1Snapshot": _build_snapshot,
    "MCPRequest": _build_request,
    "ActionIntent": _build_action_intent,
    "MCPResponse": _build_response,
    "ProposalItem": _build_proposal_item,
    "CandidateProposal": _build_candidate,
    "UserProfileRef": _build_profile_ref,
    "EmotionalEvent": _build_event,
    "RoutingResult": _build_routing_result,
    "BaselineState": _build_baseline,
    "TensionState": _build_tension,
    "GlobalContext": _build_global_context,
}


# ---------------------------------------------------------------------------
# Boundary values
#
# Cases the generic generator cannot derive (a legal pattern sample cannot be
# synthesised from the regex, and a one-element list needs a real element) are
# spelled out below.  Everything else is derived from the bound + annotation.
# ---------------------------------------------------------------------------

_EXPLICIT_CASES: dict[str, dict[str, tuple[Any, Any]]] = {
    # "ipc.affiliation" is a lowercase dotted path; "IPC.affiliation" has an
    # uppercase segment, which the anchored pattern rejects.
    "ProposalItem.target": {"pattern": ("ipc.affiliation", "IPC.affiliation")},
    # proposals is list[ProposalItem]: one real item passes, [] is too short.
    "CandidateProposal.proposals": {"min_length": ([_build_proposal_item()], [])},
    # scenario_tag is min_length=1 *and* vocabulary-checked (P0-C6), so the
    # generic single-character sample ("a") is rejected for the second reason.
    # The in-bound sample therefore has to be a real tag.
    "RoutingResult.scenario_tag": {"min_length": ("neutral", "")},
}


def _epsilon(bound: Any) -> Any:
    return 0.01 if isinstance(bound, float) else 1


def _field_annotation(model_name: str, field_name: str) -> Any:
    return _MODEL_BY_NAME[model_name].model_fields[field_name].annotation


def _boundary_pair(model_name: str, field_name: str, key: str, bound: Any) -> tuple[Any, Any]:
    """Return ``(must_construct, must_raise)`` for one constraint."""
    table_key = f"{model_name}.{field_name}"
    if table_key in _EXPLICIT_CASES and key in _EXPLICIT_CASES[table_key]:
        return _EXPLICIT_CASES[table_key][key]

    # ge/le are inclusive: the bound itself is legal and one step outside is
    # not.  gt/lt are exclusive, so the pair inverts: the bound is the
    # *rejected* value and one step inside is the legal one.  Collapsing the
    # two families would silently assert the wrong thing -- this branch was
    # written for inclusive bounds, and no exclusive bound existed until
    # P2.8-C1R added BaselineState.persona_heat_modifier {gt: 0.0}.
    if key == "ge":
        return bound, bound - _epsilon(bound)
    if key == "gt":
        return bound + _epsilon(bound), bound
    if key == "le":
        return bound, bound + _epsilon(bound)
    if key == "lt":
        return bound - _epsilon(bound), bound

    annotation = _field_annotation(model_name, field_name)
    if key in {"min_length", "max_length"}:
        if annotation is str:
            if key == "min_length":
                return "a", ""
            return "a" * bound, "a" * (bound + 1)
        if get_origin(annotation) is tuple:
            if key == "min_length":
                return ("a",), ()
            return ("a",) * bound, ("a",) * (bound + 1)

    raise AssertionError(
        f"no boundary generator for {table_key}.{key} (annotation={annotation!r}); "
        "add an entry to _EXPLICIT_CASES"
    )


def _constraint_cases() -> list[tuple[str, str]]:
    return [
        (table_key, constraint_key)
        for table_key, constraints in EXPECTED_CONSTRAINTS.items()
        for constraint_key in constraints
    ]


# ---------------------------------------------------------------------------
# 1. Non-vacuity of the extractor
# ---------------------------------------------------------------------------


def test_extractor_is_not_vacuous() -> None:
    """The reflection must actually see the constraints, not return nothing."""
    discovered_items = sum(len(value) for value in _EXTRACTED.values())
    assert discovered_items >= 44, (
        f"reflection only found {discovered_items} constraint items (< 44); "
        "the extractor stopped working, every lock below would be vacuous"
    )
    assert discovered_items == 49, (
        f"expected 49 declared constraints, reflection found {discovered_items}; "
        "either a constraint was added/removed (update EXPECTED_CONSTRAINTS) "
        "or the extractor broke"
    )
    assert len(_EXTRACTED) == 37, (
        f"expected 37 constrained fields, reflection found {len(_EXTRACTED)}"
    )


def test_every_table_entry_is_extractable() -> None:
    """No phantom rows: each table key must resolve to a real declared constraint."""
    missing_keys = [key for key in EXPECTED_CONSTRAINTS if key not in _EXTRACTED]
    assert not missing_keys, f"table rows with no constraint on the model: {missing_keys}"

    for table_key, expected in EXPECTED_CONSTRAINTS.items():
        model_name, field_name = table_key.split(".")
        assert model_name in _MODEL_BY_NAME, f"{table_key}: no such domain model"
        assert field_name in _MODEL_BY_NAME[model_name].model_fields, (
            f"{table_key}: no such field on {model_name}"
        )
        assert _EXTRACTED[table_key] == expected, (
            f"{table_key}: declared {_EXTRACTED[table_key]!r} != table {expected!r}"
        )


def test_every_declared_constraint_is_in_the_table() -> None:
    """Reverse direction: a new constraint cannot slip in unrecorded."""
    untracked = [key for key in _EXTRACTED if key not in EXPECTED_CONSTRAINTS]
    assert not untracked, (
        f"constraints declared but absent from EXPECTED_CONSTRAINTS: {untracked}; "
        "add them to the table with a citation"
    )


# ---------------------------------------------------------------------------
# 2. Behaviour: every constraint is exercised on both sides of its boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table_key", "constraint_key"),
    _constraint_cases(),
    ids=[f"{key}::{ckey}" for key, ckey in _constraint_cases()],
)
def test_constraint_holds_at_its_boundary(table_key: str, constraint_key: str) -> None:
    model_name, field_name = table_key.split(".")
    bound = EXPECTED_CONSTRAINTS[table_key][constraint_key]
    accept_value, reject_value = _boundary_pair(model_name, field_name, constraint_key, bound)
    build = _BUILDERS[model_name]

    # Positive side: the in-bound value must construct.  Without this the
    # "reject everything" implementation would look correct.
    accepted = build(**{field_name: accept_value})
    assert isinstance(accepted, BaseModel), (
        f"{table_key}.{constraint_key}: in-bound value {accept_value!r} did not construct"
    )

    # Negative side: one step outside must raise on that very field.
    with pytest.raises(ValidationError) as excinfo:
        build(**{field_name: reject_value})
    error_locations = [error["loc"] for error in excinfo.value.errors()]
    assert any(location and location[0] == field_name for location in error_locations), (
        f"{table_key}.{constraint_key}: rejecting {reject_value!r} raised on "
        f"{error_locations} instead of {field_name}"
    )


# The 31 constraint items that had no guard before this card.  Recorded verbatim
# (field + constraint key) so the card's "all 31 locked" criterion is machine
# checkable instead of being asserted in prose.  Provenance: the main-controller
# scan that relaxed each one and still got 144 passed.
_PREVIOUSLY_UNGUARDED: tuple[tuple[str, str], ...] = (
    ("MCPRequest.request_id", "min_length"),
    ("MCPRequest.user_id", "min_length"),
    ("MCPRequest.scope_id", "min_length"),
    ("ActionIntent.emotion", "min_length"),
    ("ActionIntent.sticker_tag", "min_length"),
    ("MCPResponse.request_id", "min_length"),
    ("ProposalItem.category", "min_length"),
    ("ProposalItem.target", "pattern"),
    ("ProposalItem.reason", "min_length"),
    ("CandidateProposal.candidate_id", "min_length"),
    ("CandidateProposal.baseline_hash", "min_length"),
    ("CandidateProposal.proposals", "min_length"),
    ("EmotionalEvent.id", "min_length"),
    ("EmotionalEvent.emotion_key", "min_length"),
    ("EmotionalEvent.intensity", "ge"),
    ("EmotionalEvent.intensity", "le"),
    ("EmotionalEvent.scope_id", "min_length"),
    ("IPCState.dominance", "ge"),
    ("IPCState.dominance", "le"),
    ("IPCState.relationship_stage", "min_length"),
    ("IPCState.relationship_ceiling", "min_length"),
    ("PulseState.dopamine", "ge"),
    ("PulseState.dopamine", "le"),
    ("PulseState.cortisol", "ge"),
    ("PulseState.cortisol", "le"),
    ("PulseState.oxytocin", "ge"),
    ("PulseState.oxytocin", "le"),
    ("TraitState.score", "ge"),
    ("TraitState.score", "le"),
    ("S1Snapshot.version", "ge"),
    ("S1Snapshot.scope_id", "min_length"),
)


def test_all_31_previously_unguarded_constraints_are_locked() -> None:
    """Every item the scan found unguarded is in the table and in the matrix."""
    assert len(_PREVIOUSLY_UNGUARDED) == 31, (
        f"the unguarded list must stay at the 31 items the scan reported, "
        f"got {len(_PREVIOUSLY_UNGUARDED)}"
    )
    cases = set(_constraint_cases())
    for table_key, constraint_key in _PREVIOUSLY_UNGUARDED:
        assert table_key in EXPECTED_CONSTRAINTS, (
            f"{table_key} was unguarded and is still missing from EXPECTED_CONSTRAINTS"
        )
        assert constraint_key in EXPECTED_CONSTRAINTS[table_key], (
            f"{table_key}.{constraint_key} was unguarded and is still missing from the table"
        )
        assert (table_key, constraint_key) in cases, (
            f"{table_key}.{constraint_key} has no boundary case in the matrix"
        )


def test_guarded_and_unguarded_partition_the_table() -> None:
    """18 previously guarded + 31 unguarded must equal the 49 declared items."""
    all_items = {item for item in _constraint_cases()}
    unguarded = set(_PREVIOUSLY_UNGUARDED)
    previously_guarded = all_items - unguarded
    assert len(previously_guarded) == 18, (
        f"expected 18 previously-guarded items, found {len(previously_guarded)}: "
        f"{sorted(previously_guarded)}"
    )
    assert len(unguarded & previously_guarded) == 0
    assert len(all_items) == 49


def test_boundary_matrix_covers_all_49_constraints() -> None:
    """The parametrised matrix must cover every item in the table, not a subset."""
    total_items = sum(len(constraints) for constraints in EXPECTED_CONSTRAINTS.values())
    assert total_items == 49, f"table has {total_items} constraint items, expected 49"
    assert len(_constraint_cases()) == total_items, (
        "the parametrised matrix does not cover every table item"
    )
    assert len({key for key, _ in _constraint_cases()}) == 37


# ---------------------------------------------------------------------------
# ``model_config`` is a constraint too -- the four ``Field`` keys above cannot
# see it, so ``frozen`` would otherwise drift unwatched.  P0-C6 §7.4 requires
# the frozen mutation to turn *this* file red as well as test_emotional_event.
# ---------------------------------------------------------------------------

# Four immutable domain models, each for its own reason (P0-C5 ruling,
# P0-C6 section 4, P2.8-C1):
#   EmotionalEvent    -- an already-happened fact, append-only.
#   RoutingResult     -- a cross-layer DTO, so a downstream consumer cannot
#                        rewrite the routing decision it was handed.
#   GlobalDelta       -- one submission is an immutable receipt; if it could
#                        still be edited afterwards, the version comparison
#                        commit_delta performs would compare nothing.
#   SuppressionDelta  -- same receipt argument, plus it is the placeholder
#                        output of P3's validate_dual_axis: freezing it now
#                        stops that card widening it first and asking later.
# Everything else stays mutable on purpose; the domain snapshots are rebuilt
# rather than edited in place, but that is a discipline, not a frozen field.
_EXPECTED_FROZEN: frozenset[str] = frozenset(
    {"EmotionalEvent", "GlobalDelta", "RoutingResult", "SuppressionDelta"}
)


def test_frozen_config_is_locked_for_every_domain_model() -> None:
    """``frozen`` must match the table above -- in both directions."""
    measured = {
        name
        for name, model in _MODEL_BY_NAME.items()
        if model.model_config.get("frozen") is True
    }
    assert measured == _EXPECTED_FROZEN, (
        "frozen=True models drifted: "
        f"missing={sorted(_EXPECTED_FROZEN - measured)} "
        f"unexpected={sorted(measured - _EXPECTED_FROZEN)}"
    )


def test_frozen_models_reject_attribute_assignment() -> None:
    """Behavioural half: ``frozen=True`` in the config must actually bite.

    Metadata comparison alone would stay green if pydantic stopped honouring
    ``frozen``; assigning to a real instance proves it still does.
    """
    routing = _build_routing_result()
    with pytest.raises(ValidationError):
        routing.risk_flag = "conflict"

    event = _build_event()
    with pytest.raises(ValidationError):
        event.intensity = 1.0
