"""E1-E10 locks for ``commit_delta`` (task-FFW-P3-E, ADR-009 section S2).

``commit_delta`` commits exactly two things: ``tension`` (clamped into 0..100)
and ``version`` (+1).  Hormone (``pulse.*``) and IPC (``ipc.*``) increments
belong to ``update_hormones``; here they must be rejected with a message that
names the offending key.  Silently ignoring them would let a caller believe a
hormone moved when it did not, which is the exact failure ADR-009 section S1
forbids (``GlobalContext.pulse`` is written by the UseCase dual write, not by
this function).

Written before ``commit.py`` existed on purpose: the first run fails on the
import above and only turns green once the module is in place.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.core.domain.global_context import (
    GlobalContext,
    GlobalDelta,
    TensionState,
)
from packages.core.engine.commit import commit_delta

COMMIT_SOURCE = (
    Path(__file__).resolve().parents[2] / "packages" / "core" / "engine" / "commit.py"
)

#: Roots a pure contract function may import from.  Relative imports (level > 0)
#: are the engine's own domain contracts and are allowed by construction.
PURE_IMPORT_ROOTS = frozenset({"__future__", "typing", "pydantic"})

#: Builtins that reach the outside world; a commit function may use none.
PURE_CALL_NAMES = frozenset({"open", "input", "eval", "exec", "compile", "__import__"})

#: Attribute calls that read a clock.  Same set the engine purity gate scans for.
CLOCK_ATTRIBUTES = frozenset({"now", "utcnow", "today", "fromtimestamp"})

#: Modules a pure commit function must not import at all.
BANNED_MODULES = frozenset(
    {"time", "datetime", "os", "sys", "pathlib", "sqlite3", "random", "uuid"}
)

FEEDBACK_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_context(
    *,
    version: int = 0,
    tension: float = 10.0,
    last_feedback_at: datetime | None = None,
) -> GlobalContext:
    """A global context with the two commit-relevant fields steered."""
    return GlobalContext(
        version=version,
        tension=TensionState(value=tension, last_feedback_at=last_feedback_at),
    )


def test_commit_delta_returns_a_new_context_and_bumps_version() -> None:
    """E1: the result is a new object and the input's version is untouched."""
    context = make_context(version=3, tension=10.0)

    result = commit_delta(context, GlobalDelta(tension=5.0))

    assert result is not context
    assert context.version == 3
    assert result.version == 3 + 1


def test_commit_delta_does_not_mutate_its_arguments() -> None:
    """E2: neither the context nor the delta changes across the call."""
    context = make_context(version=1, tension=10.0)
    delta = GlobalDelta(tension=5.0)
    context_before = context.model_dump_json()
    delta_before = delta.model_dump_json()

    commit_delta(context, delta)

    assert context.model_dump_json() == context_before
    assert delta.model_dump_json() == delta_before


def test_commit_delta_adds_the_tension_increment() -> None:
    """E3: 10.0 + 5.0 lands on 15.0."""
    result = commit_delta(make_context(tension=10.0), GlobalDelta(tension=5.0))

    assert result.tension.value == 15.0


def test_tension_above_the_ceiling_clamps_to_exactly_one_hundred() -> None:
    """E4: 98.0 + 5.0 clamps to 100.0 and does not raise.

    The clamp has to happen here rather than in Pydantic: ``TensionState.value``
    carries ``le=100.0``, so an unclamped 103.0 would surface as a
    ``ValidationError`` from the round trip instead of a committed 100.0.
    """
    result = commit_delta(make_context(tension=98.0), GlobalDelta(tension=5.0))

    assert result.tension.value == 100.0


def test_tension_below_the_floor_clamps_to_exactly_zero() -> None:
    """E5: 2.0 - 5.0 clamps to 0.0 and does not raise."""
    result = commit_delta(make_context(tension=2.0), GlobalDelta(tension=-5.0))

    assert result.tension.value == 0.0


def test_a_pulse_delta_is_rejected_naming_the_key() -> None:
    """E6: ``pulse.*`` is not this function's business and must say so."""
    with pytest.raises(ValueError) as excinfo:
        commit_delta(make_context(), GlobalDelta(pulse={"pulse.dopamine": 1.0}))

    message = str(excinfo.value)
    assert "pulse.dopamine" in message
    assert "update_hormones" in message


def test_an_ipc_delta_is_rejected_naming_the_key() -> None:
    """E7: ``ipc.*`` is rejected the same way, naming the key."""
    with pytest.raises(ValueError) as excinfo:
        commit_delta(make_context(), GlobalDelta(ipc={"ipc.affiliation": 1.0}))

    message = str(excinfo.value)
    assert "ipc.affiliation" in message
    assert "update_hormones" in message


def test_an_empty_delta_only_bumps_the_version() -> None:
    """E8: no increments means tension stays put while version advances."""
    result = commit_delta(make_context(version=7, tension=42.0), GlobalDelta())

    assert result.tension.value == 42.0
    assert result.version == 8


def test_explicitly_empty_carriers_count_as_absent() -> None:
    """E8b: ``pulse={}`` / ``ipc={}`` are empty, not a rejected submission."""
    result = commit_delta(make_context(version=0), GlobalDelta(pulse={}, ipc={}))

    assert result.version == 1


def test_last_feedback_at_is_carried_through_untouched() -> None:
    """E9: the feedback anchor is neither read nor rewritten."""
    context = make_context(last_feedback_at=FEEDBACK_AT)

    result = commit_delta(context, GlobalDelta(tension=1.0))

    assert result.tension.last_feedback_at is not None
    assert result.tension.last_feedback_at == FEEDBACK_AT
    assert result.tension.last_feedback_at == context.tension.last_feedback_at


def test_commit_does_not_invent_a_feedback_timestamp() -> None:
    """E9b: a ``None`` anchor stays ``None``; committing is not feedback."""
    result = commit_delta(make_context(last_feedback_at=None), GlobalDelta(tension=1.0))

    assert result.tension.last_feedback_at is None


def test_commit_module_is_pure_at_the_ast_level() -> None:
    """E10: no clock, no IO, no import outside the pure contract set.

    Checked on the syntax tree rather than on ``sys.modules`` because an import
    is a property of the source, not of what happened to be loaded by the time
    this test runs.
    """
    tree = ast.parse(COMMIT_SOURCE.read_text(encoding="utf-8"))

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots <= PURE_IMPORT_ROOTS, (
        f"commit.py imports outside the pure set: {sorted(imported_roots - PURE_IMPORT_ROOTS)}"
    )
    assert not (imported_roots & BANNED_MODULES), (
        f"commit.py imports a banned module: {sorted(imported_roots & BANNED_MODULES)}"
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in CLOCK_ATTRIBUTES, (
                    f"commit.py calls a clock attribute: {node.func.attr}"
                )
            elif isinstance(node.func, ast.Name):
                assert node.func.id not in PURE_CALL_NAMES, (
                    f"commit.py calls an IO builtin: {node.func.id}"
                )


def test_commit_source_has_no_module_level_mutable_state() -> None:
    """E10b: a pure contract module declares constants and functions only.

    A module-level ``list``/``dict``/``set`` would make two calls share state,
    which is precisely what makes a "pure" function impure in practice.
    """
    tree = ast.parse(COMMIT_SOURCE.read_text(encoding="utf-8"))

    mutable_assignments = [
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in {"list", "dict", "set"}
    ]

    assert mutable_assignments == []
