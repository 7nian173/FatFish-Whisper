# ruff: noqa: RUF001
"""Contract tests for packages/core/domain (AC-D1..AC-D13).

This file was written before the domain sources on purpose: the first run of it
must fail (ImportError / collection error) and only turns green once
state.py / contracts.py / exceptions.py exist.

Test data strings below are copied verbatim from contracts 3.1-3.4, so they keep
the original full-width CJK punctuation that RUF001 flags as ambiguous.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.core.domain.contracts import (
    ActionIntent,
    CandidateProposal,
    MCPRequest,
    MCPRequestContext,
    MCPResponse,
    ProposalItem,
)
from packages.core.domain.exceptions import TimeDecayError
from packages.core.domain.global_context import PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "packages" / "core" / "domain"

# Single source of truth (task-FFW-P3-00): the in-suite allowlist copy used to
# drift from the authoritative table in ``30_沙盒区/scripts/check_imports.py``
# (e.g. ``re``/``collections``/``math``/``hashlib`` lingered here long after the
# domain sources stopped using them).  Both test files now load the gate script
# by path and read its ``ALLOWED_TOP`` directly, so the two can never disagree.
# importlib-by-path is chosen over a shared module because the script is not a
# package member and is not on sys.path; extracting a common module would add a
# new file both the script and the tests must depend on -- a bigger change than
# reading the existing authoritative table where it lives.
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_imports.py"
_SPEC = importlib.util.spec_from_file_location("check_imports", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GATE)

ALLOWED_TOP_LEVEL_MODULES = _GATE.ALLOWED_TOP["packages.core.domain"]

BANNED_ATTRIBUTE_CALLS = {"now", "utcnow", "today", "fromtimestamp"}

# Contract 3.1 example; only the "pure | roleplay" placeholder is resolved to one
# of its two legal values.
MCP_REQUEST_EXAMPLE = {
    "request_id": "uuid-1234",
    "user_id": "user_9527",
    "scope_id": "group_8888",
    "message": "你真是个笨蛋！",
    "persona_card": "你是一个傲娇的程序员，绝不轻易认输。",
    "chat_mode": "roleplay",
    "context": {
        "last_interaction_at": "2026-11-04T10:00:00Z",
        "current_time": "2026-11-04T10:05:00Z",
    },
}

# Contract 3.2 example, verbatim.
S1_SNAPSHOT_EXAMPLE = {
    "version": 102,
    "scope_id": "user_9527",
    "timestamp": "2026-11-04T10:05:00Z",
    "last_decay_at": "2026-11-04T10:00:00Z",
    "is_conflict": True,
    "ipc": {
        "affiliation": 60.0,
        "dominance": -10.0,
        "relationship_stage": "friend",
        "relationship_ceiling": "lover",
    },
    "traits": {
        "tsundere": {"score": 85, "visibility": "unlocked"},
        "shyness": {"score": 30, "visibility": "dormant"},
    },
    # C3 shape: each emotion is an EmotionValue, not a bare float.  Kept as a
    # plain JSON dict (option (a) of the card) so the constant stays a payload
    # that model_validate / dict() / {**} consumers can all share unchanged.
    "emotions": {
        "anger": {"intensity": 20.0, "ttl_expires_at": "2030-01-01T00:00:00Z"},
        "joy": {"intensity": 10.0, "ttl_expires_at": "2030-01-01T00:00:00Z"},
    },
}

PROPOSAL_ITEM_EXAMPLE = {
    "category": "threshold",
    "target": "threshold.shyness_unlock",
    "old_value": 80,
    "new_value": 75,
    "reason": "过去14天正向互动频繁，建议降低阈值。",
    "confidence": 0.92,
}

# Contract 3.4 example, verbatim.
CANDIDATE_PROPOSAL_EXAMPLE = {
    "candidate_id": "cand_001",
    "baseline_hash": "a1b2c3d4...",
    "proposals": [PROPOSAL_ITEM_EXAMPLE],
    "status": "pending_approval",
}


def build_response_example() -> dict:
    """Contract 3.3 example with the 3.2 placeholder replaced by the 3.2 example."""
    return {
        "request_id": "uuid-1234",
        "status": "success",
        "system_head": "【输出格式封印】：绝对不要使用任何括号或星号描写动作。",
        "tail_message": (
            "【潜意识感知】：\n"
            "[主场]：被叫笨蛋时，你感到一阵烦躁..."
        ),
        "state_snapshot": S1_SNAPSHOT_EXAMPLE,
        "action_intent": {"emotion": "annoyed", "sticker_tag": "angry/pout"},
    }


def check_source(src: str) -> list[str]:
    """Return every whitelist or ban violation found in one source string."""
    violations: list[str] = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_TOP_LEVEL_MODULES:
                    violations.append(f"import:{root}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_TOP_LEVEL_MODULES:
                violations.append(f"import:{root}")
        elif is_banned_time_call(node):
            violations.append(f"call:{node.func.attr}")
    return violations


def is_banned_time_call(node: ast.AST) -> bool:
    """True when the node is a call such as `x.now()` or `x.utcnow()`."""
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in BANNED_ATTRIBUTE_CALLS


def non_empty_domain_sources() -> list[Path]:
    """Every non-empty .py file under packages/core/domain."""
    return [path for path in sorted(DOMAIN_DIR.glob("*.py")) if path.stat().st_size > 0]


# --------------------------------------------------------------------------
# Positive cases
# --------------------------------------------------------------------------


def test_mcp_request_parses_contract_example() -> None:
    request = MCPRequest.model_validate(MCP_REQUEST_EXAMPLE)
    assert request.chat_mode == "roleplay"
    assert request.request_id == "uuid-1234"
    assert request.context.current_time.year == 2026


def test_s1_snapshot_parses_contract_example() -> None:
    snapshot = S1Snapshot.model_validate(S1_SNAPSHOT_EXAMPLE)
    assert snapshot.ipc.affiliation == 60.0
    assert snapshot.traits["tsundere"].score == 85
    assert snapshot.emotions["anger"].intensity == 20.0


def test_candidate_proposal_parses_contract_example() -> None:
    proposal = CandidateProposal.model_validate(CANDIDATE_PROPOSAL_EXAMPLE)
    assert proposal.proposals[0].confidence == 0.92
    assert proposal.status == "pending_approval"


def test_mcp_response_roundtrip() -> None:
    payload = build_response_example()
    response = MCPResponse.model_validate(payload)
    dumped = response.model_dump(mode="json")
    assert dumped["request_id"] == payload["request_id"]
    assert dumped["status"] == "success"
    assert dumped["system_head"]
    assert dumped["tail_message"]
    assert dumped["state_snapshot"]["ipc"]["affiliation"] == 60.0


def test_affiliation_boundaries_accepted() -> None:
    for value in (-100, 100):
        payload = dict(S1_SNAPSHOT_EXAMPLE)
        payload["ipc"] = {**S1_SNAPSHOT_EXAMPLE["ipc"], "affiliation": value}
        assert S1Snapshot.model_validate(payload).ipc.affiliation == value


def test_empty_traits_dict_accepted() -> None:
    payload = {**S1_SNAPSHOT_EXAMPLE, "traits": {}}
    assert S1Snapshot.model_validate(payload).traits == {}


# --------------------------------------------------------------------------
# Negative and boundary cases
# --------------------------------------------------------------------------


def test_mcp_request_rejects_bad_chat_mode() -> None:
    payload = {**MCP_REQUEST_EXAMPLE, "chat_mode": "chat"}
    with pytest.raises(ValidationError):
        MCPRequest.model_validate(payload)


@pytest.mark.parametrize("affiliation", [100.5, -100.5])
def test_s1_snapshot_rejects_affiliation_out_of_range(affiliation: float) -> None:
    payload = dict(S1_SNAPSHOT_EXAMPLE)
    payload["ipc"] = {**S1_SNAPSHOT_EXAMPLE["ipc"], "affiliation": affiliation}
    with pytest.raises(ValidationError):
        S1Snapshot.model_validate(payload)


@pytest.mark.parametrize("confidence", [1.01, -0.1])
def test_candidate_proposal_rejects_confidence_out_of_range(confidence: float) -> None:
    item = {**PROPOSAL_ITEM_EXAMPLE, "confidence": confidence}
    payload = {**CANDIDATE_PROPOSAL_EXAMPLE, "proposals": [item]}
    with pytest.raises(ValidationError):
        CandidateProposal.model_validate(payload)


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        MCPRequest.model_validate({**MCP_REQUEST_EXAMPLE, "unexpected": 1})
    with pytest.raises(ValidationError):
        S1Snapshot.model_validate({**S1_SNAPSHOT_EXAMPLE, "unexpected": 1})
    with pytest.raises(ValidationError):
        CandidateProposal.model_validate({**CANDIDATE_PROPOSAL_EXAMPLE, "unexpected": 1})


def test_request_response_id_consistency() -> None:
    request = MCPRequest.model_validate(MCP_REQUEST_EXAMPLE)
    response = MCPResponse.model_validate(build_response_example())
    assert response.request_id == request.request_id

    drifted = {**build_response_example(), "request_id": "uuid-9999"}
    assert MCPResponse.model_validate(drifted).request_id != request.request_id


REQUIRED_FIELD_CASES = [
    (
        MCPRequestContext,
        {"last_interaction_at": "2026-11-04T10:00:00Z", "current_time": "2026-11-04T10:05:00Z"},
        "current_time",
    ),
    (MCPRequest, MCP_REQUEST_EXAMPLE, "context"),
    (
        IPCState,
        {
            "affiliation": 60.0,
            "dominance": -10.0,
            "relationship_stage": "friend",
            "relationship_ceiling": "lover",
        },
        "dominance",
    ),
    (TraitState, {"score": 85, "visibility": "unlocked"}, "visibility"),
    (S1Snapshot, S1_SNAPSHOT_EXAMPLE, "last_decay_at"),
    (ActionIntent, {"emotion": "annoyed", "sticker_tag": "angry/pout"}, "sticker_tag"),
    (MCPResponse, build_response_example(), "state_snapshot"),
    (ProposalItem, PROPOSAL_ITEM_EXAMPLE, "confidence"),
    (CandidateProposal, CANDIDATE_PROPOSAL_EXAMPLE, "baseline_hash"),
]


@pytest.mark.parametrize(
    ("model", "payload", "missing_field"),
    REQUIRED_FIELD_CASES,
    ids=[case[0].__name__ for case in REQUIRED_FIELD_CASES],
)
def test_required_field_missing_raises(model: type, payload: dict, missing_field: str) -> None:
    incomplete = {key: value for key, value in payload.items() if key != missing_field}
    assert missing_field not in incomplete
    with pytest.raises(ValidationError):
        model.model_validate(incomplete)


def test_pulse_state_has_no_required_fields() -> None:
    """PulseState is deliberately all-default, so it cannot appear above.

    HormoneState used to require all three lanes; the pulse layer (ADR-008
    section 1.3) gives each a 50.0 baseline so a caller only sends the lanes it
    actually moves.  Keeping the old row would mean asserting that dropping a
    required field raises -- against a model that has none, which is why the
    row was removed rather than "fixed".
    """
    required = [name for name, field in PulseState.model_fields.items() if field.is_required()]
    assert required == [], f"PulseState must stay all-default, got {required}"
    assert PulseState().model_dump() == {
        "dopamine": 50.0,
        "cortisol": 50.0,
        "oxytocin": 50.0,
        "delayed_impact": {},
    }


def test_s1_snapshot_field_names_match_contract() -> None:
    assert set(MCPRequestContext.model_fields) == {"last_interaction_at", "current_time"}
    assert set(MCPRequest.model_fields) == {
        "request_id",
        "user_id",
        "scope_id",
        "message",
        "persona_card",
        "chat_mode",
        "context",
    }
    assert set(IPCState.model_fields) == {
        "affiliation",
        "dominance",
        "relationship_stage",
        "relationship_ceiling",
    }
    assert set(PulseState.model_fields) == {
        "dopamine",
        "cortisol",
        "oxytocin",
        "delayed_impact",
    }
    assert set(TraitState.model_fields) == {"score", "visibility"}
    # P2.5 (task-FFW-P25-T1) added compute_tier right after is_conflict: the
    # orchestration layer derives it per request, domain only stores it.
    assert set(S1Snapshot.model_fields) == {
        "version",
        "scope_id",
        "timestamp",
        "last_active_at",
        "last_decay_at",
        "is_conflict",
        "compute_tier",
        "ipc",
        "traits",
        "emotions",
    }
    assert set(ActionIntent.model_fields) == {"emotion", "sticker_tag"}
    # P2.5 (task-FFW-P25-T4) added typing_delay_ms after action_intent: it is
    # an output-direction field the engine fills in, not a host input.
    assert set(MCPResponse.model_fields) == {
        "request_id",
        "status",
        "system_head",
        "tail_message",
        "state_snapshot",
        "action_intent",
        "typing_delay_ms",
    }
    assert set(ProposalItem.model_fields) == {
        "category",
        "target",
        "old_value",
        "new_value",
        "reason",
        "confidence",
    }
    assert set(CandidateProposal.model_fields) == {
        "candidate_id",
        "baseline_hash",
        "proposals",
        "status",
    }


def test_domain_import_whitelist() -> None:
    sources = non_empty_domain_sources()
    assert len(sources) >= 1, "no non-empty .py file under domain; this case must fail"

    for path in sources:
        violations = check_source(path.read_text(encoding="utf-8"))
        assert violations == [], f"{path.name} violates the domain whitelist: {violations}"

    assert check_source("import sqlite3\n") == ["import:sqlite3"]
    assert check_source("from pathlib import Path\n") == ["import:pathlib"]
    assert check_source("import datetime\ndatetime.datetime.now()\n") == ["call:now"]
    assert check_source("import datetime\ndatetime.datetime.utcnow()\n") == ["call:utcnow"]
    assert check_source("from .state import S1Snapshot\n") == []


def test_domain_sources_have_no_time_side_effects() -> None:
    sources = non_empty_domain_sources()
    assert len(sources) >= 1, "no non-empty .py file under domain; this case must fail"

    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        banned = [node.func.attr for node in ast.walk(tree) if is_banned_time_call(node)]
        assert banned == [], f"{path.name} calls the system clock: {banned}"


def test_time_decay_error_is_exception() -> None:
    error = TimeDecayError("delta_t must not be negative")
    assert isinstance(error, Exception)
    assert type(error).__name__ == "TimeDecayError"


# --- P0-C1 / G1: TraitState.visibility gains the third state "flickering" ---
# Ruling A (P0-主控裁决-20261105 §A): flickering goes to the tension slot only,
# never to the main slot, with zero assembler changes.  AC-G1-2..AC-G1-4 lock
# the type surface here; the behavioural locks (AC-G1-5/6) live in
# tests/engine/test_assembler.py because they must assert on real output.


def test_trait_visibility_accepts_flickering() -> None:
    # AC-G1-2: the third state constructs (the pre-change probe rejected it).
    trait = TraitState(score=85, visibility="flickering")
    assert trait.visibility == "flickering"
    assert trait.score == 85

    # All three states must stay constructible side by side.
    for value in ("unlocked", "flickering", "dormant"):
        assert TraitState(score=50, visibility=value).visibility == value


def test_trait_visibility_rejects_out_of_enum_values() -> None:
    # AC-G1-3: adding a third state must not open the enum up.
    for bad in ("hidden", "flicker", "", "UNLOCKED", "unlocked "):
        with pytest.raises(ValidationError):
            TraitState(score=85, visibility=bad)


def test_trait_field_names_unchanged_by_flickering() -> None:
    # AC-G1-4: adding an enum value must not add or drop a field.
    assert set(TraitState.model_fields) == {"score", "visibility"}
    assert TraitState.model_fields["visibility"].annotation is not None


# --- P0-C2 / G2: PulseState gains the delayed_impact companion dict ---
# Ruling D (P0-主控裁决-20261105 §D): HPA-axis latency lands as a *sibling*
# dict field, the three hormones stay bare floats.  AC-G2-2/G2-3 lock the
# type surface here; the decay-interaction locks (AC-G2-5/6) live in
# tests/engine/test_dynamics.py because they must call apply_time_decay.


def test_hormone_state_delayed_impact_defaults() -> None:
    # AC-G2-2: omitted delayed_impact defaults to an empty dict, so every
    # existing construction site stays valid without changes.
    hormones = PulseState(dopamine=50.0, cortisol=50.0, oxytocin=50.0)
    assert hormones.delayed_impact == {}


def test_hormone_state_delayed_impact_rejects_unknown_key() -> None:
    # AC-G2-3 forward: a misspelled key must fail construction loudly --
    # silently treating it as "no pending impact" would drop a real stimulus.
    with pytest.raises(ValidationError, match="unknown delayed_impact key: dopamin"):
        PulseState(
            dopamine=50.0,
            cortisol=50.0,
            oxytocin=50.0,
            delayed_impact={"dopamin": 5.0},
        )

    # AC-G2-3 reverse guard: the three legal keys must construct with the
    # payload preserved -- otherwise this test is a "rejects everything"
    # tautology.
    hormones = PulseState(
        dopamine=50.0,
        cortisol=50.0,
        oxytocin=50.0,
        delayed_impact={"dopamine": 5.0, "cortisol": 2.0, "oxytocin": 1.0},
    )
    assert hormones.delayed_impact == {"dopamine": 5.0, "cortisol": 2.0, "oxytocin": 1.0}


def test_hormone_state_delayed_impact_value_bounds() -> None:
    # Values follow the same 0..100 scale as the hormones they feed into.
    for bad in ({"dopamine": -1.0}, {"dopamine": 101.0}):
        with pytest.raises(ValidationError):
            PulseState(
                dopamine=50.0,
                cortisol=50.0,
                oxytocin=50.0,
                delayed_impact=bad,
            )

    # Boundary values are in-range.
    ok = PulseState(
        dopamine=50.0,
        cortisol=50.0,
        oxytocin=50.0,
        delayed_impact={"dopamine": 0.0, "cortisol": 100.0},
    )
    assert ok.delayed_impact == {"dopamine": 0.0, "cortisol": 100.0}


# --- extra="forbid" blanket lock (task-FFW-P0-C-LOCK) ---------------------
# t49 proved the hole: deleting ``extra="forbid"`` from UserProfileRef left the
# whole suite green, because only 3 of the 11 domain models had a test locking
# that rule.  One hand-written case per model would rot the moment a 12th model
# appears, so the list below is discovered by reflection instead.  The models
# are deliberately NOT named in this file: a literal roster silently misses the
# next model, which is exactly the failure this lock exists to prevent.

_DOMAIN_MODULES = ("packages.core.domain.contracts", "packages.core.domain.state")


def _discover_domain_models() -> list[type]:
    """Every pydantic model exported by the domain modules, deduplicated by id.

    ``contracts.py`` does ``from .state import S1Snapshot``, so the same class
    object is reachable through two module namespaces and a naive scan yields
    12 hits for 11 classes.  Deduplicating by identity (not by name) keeps the
    parametrized case count equal to the real model count, so a future field or
    model change cannot hide behind an accidental double entry.
    """
    unique: dict[int, type] = {}
    for module_name in _DOMAIN_MODULES:
        module = importlib.import_module(module_name)
        for obj in vars(module).values():
            if not inspect.isclass(obj):
                continue
            if not hasattr(obj, "model_fields"):
                continue
            if not getattr(obj, "__module__", "").startswith("packages"):
                continue
            unique.setdefault(id(obj), obj)
    return list(unique.values())


_DOMAIN_MODELS = _discover_domain_models()


def test_domain_model_discovery_is_not_vacuous() -> None:
    """Guard the lock itself: empty or duplicated discovery must fail loudly.

    Without this, a module rename or a moved class would leave the
    parametrized case below iterating over nothing, and a green run would mean
    "no models were checked" rather than "all models forbid extra fields".
    """
    assert len(_DOMAIN_MODELS) >= 10, f"反射只扫到 {len(_DOMAIN_MODELS)} 个模型，扫描口径已失效"
    assert len({id(model) for model in _DOMAIN_MODELS}) == len(_DOMAIN_MODELS)
    module_names = {model.__module__ for model in _DOMAIN_MODELS}
    assert module_names == set(_DOMAIN_MODULES), f"模型来源模块不全: {sorted(module_names)}"


@pytest.mark.parametrize("model", _DOMAIN_MODELS, ids=lambda model: model.__name__)
def test_every_domain_model_forbids_extra_fields(model: type) -> None:
    cfg = model.model_config
    assert cfg.get("extra") == "forbid", f"{model.__name__} 未声明 extra=forbid"


# --- EmotionValue (task-FFW-P0-C3, G3) -----------------------------------
# The seven ACs of the card live here: AC-G3-1 is the whole-suite run recorded
# in the report, AC-G3-2..AC-G3-7 are the cases below.  Every case carries a
# reverse assertion whenever it is a "rejects X" test, so none of them can pass
# by rejecting everything.

_TTL = datetime(2030, 1, 1, tzinfo=UTC)


def test_emotion_value_requires_ttl() -> None:
    # AC-G3-2: ttl_expires_at has no default -- omitting it must fail.
    with pytest.raises(ValidationError):
        EmotionValue(intensity=40)
    ok = EmotionValue(intensity=40, ttl_expires_at=_TTL)
    assert ok.intensity == 40
    assert ok.ttl_expires_at == _TTL


def test_emotion_value_bounds() -> None:
    # AC-G3-3: out-of-range rejected, both boundary values ACCEPTED.  Without
    # the accepted half this test would pass even if intensity rejected 0..100.
    for bad in (101, -1):
        with pytest.raises(ValidationError):
            EmotionValue(intensity=bad, ttl_expires_at=_TTL)
    low = EmotionValue(intensity=0, ttl_expires_at=_TTL)
    high = EmotionValue(intensity=100, ttl_expires_at=_TTL)
    assert low.intensity == 0
    assert high.intensity == 100


def test_s1_emotions_value_type() -> None:
    # AC-G3-4: the value type changed, the field name did not.  Both halves are
    # asserted so a structural change to dict[str, EmotionValue] cannot pass.
    snapshot = S1Snapshot.model_validate(S1_SNAPSHOT_EXAMPLE)
    assert set(snapshot.emotions) == {"anger", "joy"}
    for value in snapshot.emotions.values():
        assert isinstance(value, EmotionValue), f"emotions 值类型未升级: {type(value)!r}"
    assert snapshot.emotions["anger"].intensity == 20.0
    # P2.5 (task-FFW-P25-T1) added compute_tier right after is_conflict: the
    # orchestration layer derives it per request, domain only stores it.
    assert set(S1Snapshot.model_fields) == {
        "version",
        "scope_id",
        "timestamp",
        "last_active_at",
        "last_decay_at",
        "is_conflict",
        "compute_tier",
        "ipc",
        "traits",
        "emotions",
    }


def test_emotion_value_utc_only() -> None:
    # AC-G3-5: a naive datetime is a real incident source (domain never reads
    # the clock, so a caller-supplied naive value would silently drift by the
    # local offset).  Aware values must keep working.
    with pytest.raises(ValidationError):
        EmotionValue(intensity=40, ttl_expires_at=datetime(2030, 1, 1))
    with pytest.raises(ValidationError):
        EmotionValue(intensity=40, ttl_expires_at=_TTL, frozen_until=datetime(2030, 1, 2))
    ok = EmotionValue(intensity=40, ttl_expires_at=_TTL, frozen_until=_TTL)
    assert ok.ttl_expires_at.tzinfo is not None
    assert ok.frozen_until is not None and ok.frozen_until.tzinfo is not None


def test_emotion_value_frozen_until_optional() -> None:
    # AC-G3-6: frozen_until is the only optional field on the model.
    default = EmotionValue(intensity=55, ttl_expires_at=_TTL)
    assert default.frozen_until is None
    explicit = EmotionValue(intensity=55, ttl_expires_at=_TTL, frozen_until=_TTL)
    assert explicit.frozen_until == _TTL


def test_emotion_value_no_threshold_constants() -> None:
    # AC-G3-7: appendix H gives no extreme-value thresholds, so none may land
    # in the contract layer (it would be read as settled fact later).
    assert not {"is_extreme"} & set(EmotionValue.model_fields)

    # The other half of AC-G3-7: "state.py 不新增任何阈值常量".  Measured as
    # "state.py has no module-level assignments at all" -- stronger than a
    # name-pattern scan, and true today, so any future module constant (a
    # threshold or otherwise) trips this case and forces a conscious decision.
    source = (DOMAIN_DIR / "state.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_assignments = [
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
    ]
    assert module_assignments == [], f"state.py 出现模块级常量: {module_assignments}"


def test_s1_emotions_accepts_emotion_value_instances() -> None:
    # Bridge case: the dict may also be built from instances (not JSON), which
    # is what the test factories do.  Keeps both construction styles locked.
    payload = dict(S1_SNAPSHOT_EXAMPLE)
    payload["emotions"] = {"joy": EmotionValue(intensity=10.0, ttl_expires_at=_TTL)}
    snapshot = S1Snapshot.model_validate(payload)
    assert snapshot.emotions["joy"].intensity == 10.0
# --- P3 U6: system_head / tail_message semantic locks (task-FFW-P3-U6-LOCK) --
# U6 split the single injection string into a static head and a dynamic tail so
# the host can keep the head byte-identical at the front of every request and
# preserve KV-cache prefix hits.  Until now the only assertions on the two
# fields were test_mcp_response_roundtrip's "both are non-empty" pair, which
# survived three real regressions: swapped descriptions, a dynamic marker inside
# the static field, and a default that made the field optional.  The four cases
# below pin the semantics instead of the presence.


def test_system_head_and_tail_message_are_not_interchangeable() -> None:
    """The static head and the dynamic tail carry different roles.

    The head is the seal replayed byte-for-byte at the front of every request;
    the tail is the per-request phenomenological injection appended after the
    conversation.  Swapping their contents still parses -- both are non-empty
    strings -- so only a content assertion can tell the two halves apart.  The
    check runs against the raw example payload and against the parsed instance,
    because a field could be rewritten in either step.
    """
    payload = build_response_example()
    response = MCPResponse.model_validate(payload)

    for label, head, tail in (
        ("example payload", payload["system_head"], payload["tail_message"]),
        ("parsed response", response.system_head, response.tail_message),
    ):
        assert "【输出格式封印】" in head, f"{label}: head lost the format seal"
        assert "【潜意识感知】" in tail, f"{label}: tail lost the injection marker"
        assert "【潜意识感知】" not in head, f"{label}: head carries the dynamic marker"
        assert "【输出格式封印】" not in tail, f"{label}: tail carries the static seal"


def test_system_head_is_static_across_responses() -> None:
    """Static means byte equality across requests, not a shared marker.

    Two responses built from different request ids must reproduce the head
    exactly.  The tail is the dynamic half and is allowed to differ, so no
    equality is asserted on it; what it may not do is derive from the request
    id, because the tail is computed from state rather than from identity.  A
    per-request f-string tail would still look "dynamic" while quietly moving
    request-scoped text into the field the host caches.
    """
    first = MCPResponse.model_validate(
        {**build_response_example(), "request_id": "uuid-a"}
    )
    second = MCPResponse.model_validate(
        {**build_response_example(), "request_id": "uuid-b"}
    )

    # Without this the equality below could hold simply because nothing varied.
    assert first.request_id != second.request_id

    assert first.system_head == second.system_head
    assert first.system_head.encode("utf-8") == second.system_head.encode("utf-8")

    assert first.request_id not in first.system_head
    assert second.request_id not in second.system_head
    assert first.request_id not in first.tail_message
    assert second.request_id not in second.tail_message


def test_system_head_and_tail_message_are_required() -> None:
    """Both halves must be supplied; neither may fall back to an empty default.

    A defaulted head would let a host omit it and silently send an empty prefix;
    a defaulted tail would let the injection disappear without any error.  The
    metadata check is paired with a behavioural one so the lock survives a
    field being rebuilt at runtime rather than declared with a default.
    """
    for name in ("system_head", "tail_message"):
        field = MCPResponse.model_fields[name]
        assert field.is_required(), f"{name} must stay required, got a default"

    for name in ("system_head", "tail_message"):
        incomplete = {key: value for key, value in build_response_example().items() if key != name}
        assert name not in incomplete
        with pytest.raises(ValidationError):
            MCPResponse.model_validate(incomplete)


def test_system_head_description_mentions_static_and_tail_mentions_dynamic() -> None:
    """Each description must keep naming its own half's role.

    Swapping the two descriptions leaves both fields present, non-empty and
    parseable, so this case is the only thing the swap trips.  Rewording is
    expected to update this case in the same commit -- that is the point, not a
    false alarm to be suppressed.
    """
    head = MCPResponse.model_fields["system_head"].description
    tail = MCPResponse.model_fields["tail_message"].description
    assert head is not None
    assert tail is not None

    assert "静态" in head
    assert "人设" in head
    assert "动态" not in head
    assert "动态" in tail
    assert "现象学" in tail
    assert "静态" not in tail
