"""Tests for packages/core/engine/delay.py (task-FFW-P25-T7).

Written before the implementation on purpose: the first run must fail with an
ImportError and only turns green once delay.py exists.

Card section 9 adds one machine-checkable ordering requirement: the caller must
feed ``calculate_typing_delay`` the body length *after* ``assemble_prompt(...,
pure=False)`` has truncated it.  ``delay.py`` cannot enforce that (it never sees
the snapshot), so the requirement is pinned down here by three checks:
an AST check that the function neither calls ``assemble_prompt`` nor touches a
``snapshot`` attribute, a docstring check that names the ordering, and an
end-to-end test proving the two lengths really produce different delays.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.core.domain.global_context import PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.assembler import assemble_prompt
from packages.core.engine.delay import calculate_typing_delay

TS = datetime(2026, 11, 6, 9, 0, 0, tzinfo=UTC)
TTL = datetime(2030, 1, 1, tzinfo=UTC)

DELAY_MODULE = Path(__file__).resolve().parents[2] / "packages/core/engine/delay.py"

#: Segment lines are the body; the two label lines never count (card ruling 2).
_LABEL_PREFIX = "【潜意识感知】"
_SEAL_PREFIX = "【输出格式封印】"


def body_lines(prompt: str) -> list[str]:
    """Return the injection body: every line except the two marker lines."""
    return [
        line
        for line in prompt.split("\n")
        if not line.startswith(_LABEL_PREFIX) and not line.startswith(_SEAL_PREFIX)
    ]


def body_chars(prompt: str) -> int:
    """Body length in code points (card ruling 1: code points, not bytes)."""
    return sum(len(line) for line in body_lines(prompt))


def make_snapshot(*, conflicted: bool = False) -> S1Snapshot:
    return S1Snapshot(
        version=1,
        scope_id="scope-1",
        timestamp=TS,
        last_decay_at=TS,
        is_conflict=conflicted,
        ipc=IPCState(
            affiliation=0.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        emotions={"joy": EmotionValue(intensity=10.0, ttl_expires_at=TTL)},
    )


def make_hormones(
    *, dopamine: float = 50.0, cortisol: float = 50.0, oxytocin: float = 50.0
) -> PulseState:
    return PulseState(dopamine=dopamine, cortisol=cortisol, oxytocin=oxytocin)


# ---------------------------------------------------------------------------
# 1-8. The delay formula itself (card section 5)
# ---------------------------------------------------------------------------


def test_baseline_delay_is_500() -> None:
    # base = 300 + 20 * min(10, 100) = 500, factor = 1.0.
    assert calculate_typing_delay(make_hormones(), False, 10) == 500


def test_shy_factor_doubles_the_delay() -> None:
    # oxytocin 80 > 70 and 45 <= cortisol 60 <= 70 -> factor 2.0 -> 1000.
    shy = make_hormones(oxytocin=80.0, cortisol=60.0)
    assert calculate_typing_delay(shy, False, 10) == 1000


def test_angry_delay_is_clamped_up_to_the_lower_bound() -> None:
    # base 500, factor 0.2 -> raw 100 -> clamped to 500 (lower bound proof).
    angry = make_hormones(dopamine=20.0, cortisol=90.0)
    assert calculate_typing_delay(angry, False, 10) == 500


def test_upper_bound_clamps_at_5000() -> None:
    # base = 300 + 20 * 100 = 2300; shy 2.0 times conflict 1.3 = 2.6;
    # 2300 * 2.6 = 5980 -> clamped to 5000.
    shy = make_hormones(oxytocin=80.0, cortisol=60.0)
    assert calculate_typing_delay(shy, True, 100) == 5000


def test_the_two_priority_conditions_are_mutually_exclusive() -> None:
    # Card section 5.5: if the two conditions cannot hold at once there is no
    # priority to test, and we must say so instead of faking a priority case.
    # Anger needs cortisol > 80; shy needs 45 <= cortisol <= 70.  The cortisol
    # ranges are disjoint, so no hormone triple satisfies both.
    def is_angry(dopamine: float, cortisol: float, oxytocin: float) -> bool:
        return dopamine < 40 and cortisol > 80

    def is_shy(dopamine: float, cortisol: float, oxytocin: float) -> bool:
        return oxytocin > 70 and 45 <= cortisol <= 70

    for cortisol in (0.0, 45.0, 70.0, 70.5, 80.0, 80.5, 100.0):
        for dopamine in (0.0, 39.9, 40.0, 100.0):
            for oxytocin in (0.0, 70.0, 70.1, 100.0):
                assert not (
                    is_angry(dopamine, cortisol, oxytocin)
                    and is_shy(dopamine, cortisol, oxytocin)
                ), f"conditions overlapped at {dopamine}/{cortisol}/{oxytocin}"

    # Cortisol 80 exactly is the gap: not angry (needs > 80), not shy (needs <= 70).
    assert calculate_typing_delay(make_hormones(cortisol=80.0), False, 10) == 500


def test_conflict_multiplier_stacks_on_top_of_the_priority_factor() -> None:
    # Same hormones, conflict off vs on: 1.0 -> 1.3.  base = 300 + 20*10 = 500,
    # 500 * 1.3 = 650, no clamp needed.
    base_hormones = make_hormones()
    assert calculate_typing_delay(base_hormones, False, 10) == 500
    assert calculate_typing_delay(base_hormones, True, 10) == 650


def test_conflict_multiplier_stacks_after_the_shy_factor() -> None:
    # Anti-tautology for "after, not inside": shy 2.0 then *1.3 = 2.6.
    # base 500 -> 1300.  If 1.3 were folded into the priority branch instead,
    # the product would be the same here, so this only pins the value; the
    # ordering itself is pinned by the angry+conflict case below.
    shy = make_hormones(oxytocin=80.0, cortisol=60.0)
    assert calculate_typing_delay(shy, True, 10) == 1300


def test_conflict_multiplier_applies_to_the_angry_branch_too() -> None:
    # Anger 0.2 * 1.3 = 0.26.  Use a long message so base = 300 + 20*100 = 2300
    # and the lower bound cannot hide the 1.3: 2300 * 0.2 = 460 -> clamped up to
    # 500, while 2300 * 0.26 = 598 stays inside the window.  If 1.3 were applied
    # only to the baseline branch, both calls would return 500.
    angry = make_hormones(dopamine=20.0, cortisol=90.0)
    assert calculate_typing_delay(angry, False, 100) == 500  # 460 clamped up
    assert calculate_typing_delay(angry, True, 100) == 598  # 2300 * 0.26


def test_negative_message_length_raises() -> None:
    with pytest.raises(ValueError):
        calculate_typing_delay(make_hormones(), False, -1)


def test_returns_a_plain_int() -> None:
    result = calculate_typing_delay(make_hormones(oxytocin=80.0, cortisol=60.0), True, 37)
    assert isinstance(result, int)
    assert not isinstance(result, bool)


def test_is_pure_no_input_mutation_and_repeatable() -> None:
    hormones = make_hormones(dopamine=20.0, cortisol=90.0)
    before = hormones.model_dump()
    first = calculate_typing_delay(hormones, True, 42)
    second = calculate_typing_delay(hormones, True, 42)
    assert first == second
    assert hormones.model_dump() == before


# ---------------------------------------------------------------------------
# 9. Ordering contract: the length must be the POST-truncation body length
# ---------------------------------------------------------------------------


def _function_node(module: str) -> ast.FunctionDef:
    tree = ast.parse(module)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "calculate_typing_delay":
            return node
    raise AssertionError("calculate_typing_delay not found in delay.py")


def test_delay_never_assembles_and_never_reads_the_snapshot() -> None:
    """AST proof that the ordering must happen in the caller (card 9.1)."""
    node = _function_node(DELAY_MODULE.read_text(encoding="utf-8"))

    calls = {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
    assert "assemble_prompt" not in calls

    attribute_roots = {
        attr.value.id
        for attr in ast.walk(node)
        if isinstance(attr, ast.Attribute) and isinstance(attr.value, ast.Name)
    }
    assert "snapshot" not in attribute_roots


def test_signature_keeps_user_msg_len_required() -> None:
    """Card 9.1: user_msg_len is an explicit required parameter, no default."""
    node = _function_node(DELAY_MODULE.read_text(encoding="utf-8"))
    params = [arg.arg for arg in node.args.args]
    assert params == ["hormones", "is_conflict", "user_msg_len"]
    assert node.args.defaults == []


def test_docstring_documents_the_post_truncation_ordering() -> None:
    """Card 9.2: the docstring must name the ordering and the 180 ms cost."""
    tree = ast.parse(DELAY_MODULE.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree) or ""
    assert "pure=False" in docstring
    assert "截断" in docstring
    assert "180" in docstring
    assert "59" in docstring


def test_post_truncation_length_yields_a_different_delay() -> None:
    """Card 9.3: the two body lengths must really produce different delays."""
    snapshot = make_snapshot(conflicted=True)
    body_untruncated = body_chars(assemble_prompt(snapshot, pure=True))
    body_truncated = body_chars(assemble_prompt(snapshot, pure=False))

    # The premise of the check: truncation really did shrink the body.
    assert body_untruncated > body_truncated

    hormones = make_hormones()
    early = calculate_typing_delay(hormones, True, body_untruncated)
    late = calculate_typing_delay(hormones, True, body_truncated)
    assert early != late


def test_truncation_cost_matches_the_documented_180ms() -> None:
    """59 -> 50 costs 20 * (59 - 50) = 180 ms at factor 1.0."""
    hormones = make_hormones()
    assert calculate_typing_delay(hormones, False, 59) - calculate_typing_delay(
        hormones, False, 50
    ) == 180
