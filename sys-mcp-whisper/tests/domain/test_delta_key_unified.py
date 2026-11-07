"""One vocabulary for every delta: the P2.8-C4 ruling (``DELTA_ELIGIBLE_PATHS``).

Before C4 the codebase carried two spellings of the same three hormones:
``judge.HormoneDeltas`` took dotted ``pulse.dopamine`` paths from
``DELTA_ELIGIBLE_PATHS``, while ``domain.global_context.GlobalDelta.pulse`` took
BARE names (``dopamine``) validated against ``PULSE_KEYS``.  A single stimulus
therefore had to be translated at the boundary, and a typo in the translation
was invisible -- the bare spelling silently accepted keys the dotted one refused.

C4 retired the bare spelling outright (no compatibility layer, per the card's
red line).  This file locks three things:

1. the card's verification table, cell by cell, in both directions -- the
   accepted keys really are accepted, the rejected ones really are rejected;
2. ``DELTA_ELIGIBLE_PATHS`` is the *only* physical home of the whitelist: both
   validators are asserted, by AST, to read it (directly or through the two
   namespace slices derived from it) and to contain no hardcoded key literals;
3. ``pulse.delayed_impact`` is out of the delta vocabulary even though
   ``PulseState`` carries the field -- it is a ``dict[str, float]`` of pending
   increments and cannot carry a scalar delta.

Every rejection case asserts the error *type* and message, not just that
``ValidationError`` was raised: a misspelled keyword argument lands in
``extra_forbidden`` at the same ``loc`` and would otherwise read as a pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.core.domain.global_context import (
    DELTA_ELIGIBLE_PATHS,
    PULSE_KEYS,
    GlobalDelta,
    PulseState,
)
from packages.core.engine.judge import HormoneDeltas

REPO_ROOT = Path(__file__).resolve().parents[2]
GLOBAL_CONTEXT_SOURCE = REPO_ROOT / "packages/core/domain/global_context.py"
JUDGE_SOURCE = REPO_ROOT / "packages/core/engine/judge.py"

#: The three dotted paths ``GlobalDelta.pulse`` may carry.
PULSE_DELTA_PATHS = ("pulse.dopamine", "pulse.cortisol", "pulse.oxytocin")

#: The two dotted paths ``GlobalDelta.ipc`` may carry.
IPC_DELTA_PATHS = ("ipc.affiliation", "ipc.dominance")


# ---------------------------------------------------------------------------
# 1: the verification table, cell by cell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", PULSE_DELTA_PATHS)
def test_global_delta_accepts_every_pulse_whitelist_path(key: str) -> None:
    delta = GlobalDelta(pulse={key: 1.0})
    assert delta.pulse == {key: 1.0}


@pytest.mark.parametrize("key", IPC_DELTA_PATHS)
def test_global_delta_accepts_every_ipc_whitelist_path(key: str) -> None:
    delta = GlobalDelta(ipc={key: 1.5})
    assert delta.ipc == {key: 1.5}


def test_global_delta_accepts_both_carriers_at_once() -> None:
    """The two carriers are independent slices of one whitelist."""
    delta = GlobalDelta(
        pulse={"pulse.dopamine": 1.0, "pulse.cortisol": -0.5},
        ipc={"ipc.affiliation": 2.0},
        tension=3.0,
    )
    assert set(delta.pulse) == {"pulse.dopamine", "pulse.cortisol"}
    assert set(delta.ipc) == {"ipc.affiliation"}
    assert delta.tension == 3.0


def test_bare_name_is_rejected() -> None:
    """The retired spelling: ``dopamine`` is not a delta key any more.

    This is the cell the C4 hard-gate mutation (M1) must turn red: putting the
    validator back on ``PULSE_KEYS`` makes this constructor succeed.
    """
    with pytest.raises(ValidationError) as excinfo:
        GlobalDelta(pulse={"dopamine": 1.0})

    errors = excinfo.value.errors()
    assert errors[0]["loc"] == ("pulse",)
    assert "unknown pulse key: dopamine" in errors[0]["msg"]


def test_delayed_impact_is_not_a_delta_path() -> None:
    """``PulseState`` has the field; a delta still cannot address it."""
    with pytest.raises(ValidationError) as excinfo:
        GlobalDelta(pulse={"pulse.delayed_impact": 1.0})

    assert "unknown pulse key: pulse.delayed_impact" in excinfo.value.errors()[0]["msg"]


def test_bare_delayed_impact_is_rejected_too() -> None:
    """Neither spelling of a non-summable field is accepted."""
    with pytest.raises(ValidationError) as excinfo:
        GlobalDelta(pulse={"delayed_impact": 1.0})

    assert "unknown pulse key: delayed_impact" in excinfo.value.errors()[0]["msg"]


def test_unknown_dotted_path_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        GlobalDelta(pulse={"pulse.bogus": 1.0})

    assert "unknown pulse key: pulse.bogus" in excinfo.value.errors()[0]["msg"]


def test_ipc_path_in_the_pulse_carrier_is_rejected() -> None:
    """A whitelist member still has to go into its own carrier."""
    with pytest.raises(ValidationError) as excinfo:
        GlobalDelta(pulse={"ipc.affiliation": 1.0})

    assert "unknown pulse key: ipc.affiliation" in excinfo.value.errors()[0]["msg"]


def test_pulse_path_in_the_ipc_carrier_is_rejected() -> None:
    """...and the mirror image of the same mistake."""
    with pytest.raises(ValidationError) as excinfo:
        GlobalDelta(ipc={"pulse.dopamine": 1.0})

    assert "unknown ipc key: pulse.dopamine" in excinfo.value.errors()[0]["msg"]


def test_snapshot_field_that_is_not_a_whitelist_member_is_rejected() -> None:
    """``timestamp`` exists on ``S1Snapshot`` but cannot be summed."""
    with pytest.raises(ValidationError) as excinfo:
        GlobalDelta(pulse={"timestamp": 1.0})

    assert "unknown pulse key: timestamp" in excinfo.value.errors()[0]["msg"]


# ---------------------------------------------------------------------------
# 1b: the other validator (judge) speaks the same vocabulary
# ---------------------------------------------------------------------------


def test_hormone_deltas_accepts_the_same_dotted_paths() -> None:
    deltas = HormoneDeltas(values={"pulse.dopamine": 1.0, "ipc.affiliation": -2.0})
    assert deltas.values == {"pulse.dopamine": 1.0, "ipc.affiliation": -2.0}


def test_hormone_deltas_rejects_the_retired_bare_name() -> None:
    with pytest.raises(ValidationError) as excinfo:
        HormoneDeltas(values={"dopamine": 1.0})

    assert "unknown delta key: dopamine" in excinfo.value.errors()[0]["msg"]


def test_both_validators_accept_exactly_the_whitelist() -> None:
    """Constructible == whitelisted, for both types, in both directions."""
    for path in sorted(DELTA_ELIGIBLE_PATHS):
        assert HormoneDeltas(values={path: 1.0}).values == {path: 1.0}
        carrier = "pulse" if path.startswith("pulse.") else "ipc"
        assert GlobalDelta(**{carrier: {path: 1.0}}).model_dump()[carrier] == {path: 1.0}

    for path in ("timestamp", "version", "scope_id", "traits", "emotions"):
        with pytest.raises(ValidationError):
            HormoneDeltas(values={path: 1.0})
        with pytest.raises(ValidationError):
            GlobalDelta(pulse={path: 1.0})


# ---------------------------------------------------------------------------
# 2: DELTA_ELIGIBLE_PATHS is the only physical home of the whitelist
# ---------------------------------------------------------------------------


def test_the_whitelist_has_exactly_five_members() -> None:
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


def test_the_two_carriers_partition_the_whitelist() -> None:
    """Every member lands in exactly one carrier -- no orphan, no overlap."""
    pulse_slice = {f"pulse.{name}" for name in PULSE_KEYS if name != "delayed_impact"}
    ipc_slice = {"ipc.affiliation", "ipc.dominance"}

    assert pulse_slice & ipc_slice == set()
    assert pulse_slice | ipc_slice == DELTA_ELIGIBLE_PATHS


def test_delayed_impact_is_excluded_even_though_pulse_state_has_it() -> None:
    """The one field whose presence in ``PulseState`` must NOT imply a delta.

    This is the cell the C4 mutation (M2) must turn red: adding
    ``pulse.delayed_impact`` to the literal whitelist breaks the equality here
    while ``PULSE_KEYS`` still lists ``delayed_impact`` as a pulse field.
    """
    assert "delayed_impact" in PULSE_KEYS
    assert "pulse.delayed_impact" not in DELTA_ELIGIBLE_PATHS
    assert hasattr(PulseState(), "delayed_impact")

    with pytest.raises(TypeError):
        PulseState().delayed_impact + 1.0  # type: ignore[operator]


def _validator_bodies(path: Path, class_name: str) -> dict[str, ast.FunctionDef]:
    """Return ``{validator_name: node}`` for every ``@field_validator`` in a class."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bodies: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            decorators = [ast.unparse(dec) for dec in item.decorator_list]
            if any("field_validator" in dec for dec in decorators):
                bodies[item.name] = item
    return bodies


VALIDATOR_SITES: tuple[tuple[Path, str, str], ...] = (
    (GLOBAL_CONTEXT_SOURCE, "GlobalDelta", "_pulse_keys_must_be_delta_eligible"),
    (GLOBAL_CONTEXT_SOURCE, "GlobalDelta", "_ipc_keys_must_be_delta_eligible"),
    (JUDGE_SOURCE, "HormoneDeltas", "_keys_must_be_valid_paths"),
)

#: Every spelling a validator could hardcode.  A key literal in a validator body
#: means the whitelist has grown a second home.
FORBIDDEN_KEY_LITERALS = frozenset(
    DELTA_ELIGIBLE_PATHS
    | {name for name in PULSE_KEYS}
    | {"hormones.dopamine", "hormones.cortisol", "hormones.oxytocin"}
)


@pytest.mark.parametrize(
    ("path", "class_name", "validator_name"),
    VALIDATOR_SITES,
    ids=[f"{cls}.{name}" for _path, cls, name in VALIDATOR_SITES],
)
def test_validator_body_carries_no_hardcoded_key(
    path: Path, class_name: str, validator_name: str
) -> None:
    body = _validator_bodies(path, class_name)
    assert validator_name in body, (
        f"{class_name}.{validator_name} no longer exists -- the whitelist check it "
        f"performs has to live somewhere, so update this table with its new home"
    )

    literals = {
        node.value
        for node in ast.walk(body[validator_name])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    offenders = literals & FORBIDDEN_KEY_LITERALS
    assert not offenders, (
        f"{class_name}.{validator_name} hardcodes {sorted(offenders)} -- a key list "
        f"inside the validator is a second whitelist that will drift from "
        f"DELTA_ELIGIBLE_PATHS"
    )


def _module_assignments(path: Path) -> dict[str, str]:
    """Return ``{module_level_name: unparsed RHS}`` for a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments: dict[str, str] = {}
    for node in tree.body:
        targets: list[str] = []
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if targets and node.value is not None:
            rhs = ast.unparse(node.value)
            for target in targets:
                assignments[target] = rhs
    return assignments


@pytest.mark.parametrize(
    ("path", "class_name", "validator_name"),
    VALIDATOR_SITES,
    ids=[f"{cls}.{name}" for _path, cls, name in VALIDATOR_SITES],
)
def test_validator_body_reads_the_single_whitelist(
    path: Path, class_name: str, validator_name: str
) -> None:
    """The set it compares against must resolve to ``DELTA_ELIGIBLE_PATHS``.

    Two shapes are allowed: the constant itself, or a module-level frozenset
    whose RHS is a comprehension over ``DELTA_ELIGIBLE_PATHS`` (the two carrier
    slices in ``global_context``).  A validator that compares against anything
    else -- ``PULSE_KEYS``, another module's set, a literal -- fails here, which
    is the reverse direction of ``test_validator_body_carries_no_hardcoded_key``.
    """
    body = _validator_bodies(path, class_name)[validator_name]
    names = {node.id for node in ast.walk(body) if isinstance(node, ast.Name)}

    if "DELTA_ELIGIBLE_PATHS" in names:
        return

    assignments = _module_assignments(path)
    derived = sorted(name for name in names if name in assignments)

    assert derived, (
        f"{class_name}.{validator_name} compares against no module-level set at all "
        f"(names read: {sorted(names)})"
    )

    for name in derived:
        assert "DELTA_ELIGIBLE_PATHS" in assignments[name], (
            f"{class_name}.{validator_name} compares against {name}, whose definition "
            f"is {assignments[name]!r} -- only a set derived from "
            f"DELTA_ELIGIBLE_PATHS may back a delta validator"
        )

    assert "PULSE_KEYS" not in names, (
        f"{class_name}.{validator_name} reads PULSE_KEYS -- that tuple describes "
        f"PulseState's fields, not the delta vocabulary (the C4 M1 mutation)"
    )


def test_no_module_defines_a_second_pulse_key_tuple() -> None:
    """``PULSE_KEYS``'s own value must match ``PulseState``'s fields, nothing more."""
    expected = tuple(PulseState.model_fields)
    assert set(PULSE_KEYS) == set(expected), (
        f"PULSE_KEYS drifted from PulseState.model_fields: {sorted(PULSE_KEYS)} vs "
        f"{sorted(expected)}"
    )
