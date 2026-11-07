"""Behavioural locks for assemble_messages (task-FFW-P3-F, F1-F12).

The two-field split follows the human rulings P3-4 (label line belongs to
the tail, never the head) and P3-5 (``pure`` only controls the tail's second
seal line; the head always carries the seal).  All assertions are exact
``==`` / ``len ==`` comparisons -- no ``in`` / ``startswith`` fuzz where the
card demands precision, and F2's ``startswith`` is the card's own wording.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

from packages.core.domain.config import DEFAULT_ENGINE_CONFIG
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.assembler import (
    _LABEL_LINE,
    _SEAL_LABEL,
    MAX_INJECTION_BODY_CHARS,
    PhenomenonEntry,
    _body_length,
    assemble_messages,
    assemble_prompt,
)

_TS = datetime(2026, 11, 6, 22, 0, 0)
_TTL = datetime(2030, 1, 1, tzinfo=UTC)

_SEAL_TEXT = DEFAULT_ENGINE_CONFIG.assembly.seal_text
_SEAL_LINE = f"{_SEAL_LABEL}{_SEAL_TEXT}"

_EMOTIONS = {
    "joy": EmotionValue(intensity=10.0, ttl_expires_at=_TTL),
}


def make_snapshot(
    *,
    traits: dict[str, TraitState] | None = None,
    conflicted: bool = False,
    compute_tier: str = "warm",
) -> S1Snapshot:
    if traits is None:
        traits = {
            "tsundere": TraitState(score=85, visibility="unlocked"),
            "shyness": TraitState(score=30, visibility="dormant"),
        }
    return S1Snapshot(
        version=1,
        scope_id="scope-1",
        timestamp=_TS,
        last_decay_at=_TS,
        is_conflict=conflicted,
        compute_tier=compute_tier,
        ipc=IPCState(
            affiliation=0.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits=traits,
        emotions=_EMOTIONS,
    )


# F1 -- bare head, no persona card, warm snapshot
def test_f1_head_is_exactly_the_27_char_seal() -> None:
    head, _tail = assemble_messages(make_snapshot())
    assert head == "【输出格式封印】：绝对不要使用任何括号或星号描写动作。"
    assert len(head) == 27


# F2 -- head never carries the label line; tail starts with it
def test_f2_head_has_no_label_line_and_tail_starts_with_it() -> None:
    head, tail = assemble_messages(make_snapshot())
    assert "【潜意识感知】" not in head
    assert tail.startswith("【潜意识感知】：\n")
    assert tail.count("【潜意识感知】") == 1


# F3 -- persona card prepended with a single newline
# The card's arithmetic (5 + 1 + 27 == 33) requires a 5-char card; the card's
# own example string is 4 chars, so the 5-char form is used to keep the exact
# arithmetic the card demands (deviation noted in report, quoting card F3).
def test_f3_persona_card_prepended_with_single_newline() -> None:
    head, _tail = assemble_messages(make_snapshot(), persona_card="傲娇少女酱")
    assert head == "傲娇少女酱\n【输出格式封印】：绝对不要使用任何括号或星号描写动作。"
    assert len(head) == 5 + 1 + 27 == 33


# F4 -- empty persona card behaves exactly like None
def test_f4_empty_persona_card_equals_none() -> None:
    head_none, _ = assemble_messages(make_snapshot(), persona_card=None)
    head_empty, _ = assemble_messages(make_snapshot(), persona_card="")
    assert head_none == head_empty
    assert len(head_none) == 27


# F5 -- head is fully decoupled from pure
def test_f5_head_independent_of_pure() -> None:
    snap = make_snapshot()
    head_true, _ = assemble_messages(snap, pure=True)
    head_false, _ = assemble_messages(snap, pure=False)
    assert head_true == head_false


# F6 -- pure only controls the tail's second seal line
def test_f6_pure_only_controls_tail_seal_line() -> None:
    snap = make_snapshot()
    _h, tail_true = assemble_messages(snap, pure=True)
    _h, tail_false = assemble_messages(snap, pure=False)
    assert not tail_true.endswith(_SEAL_LINE)
    assert tail_false.endswith(_SEAL_LINE)
    assert tail_false[: -len("\n" + _SEAL_LINE)] == tail_true


# F7 -- cold tier: 36-char tail, head unchanged
def test_f7_cold_tail_is_label_plus_narration_36_chars() -> None:
    snap = make_snapshot(compute_tier="cold")
    head, tail = assemble_messages(snap)
    assert tail == "【潜意识感知】：[系统提示：未唤醒] 角色当前处于未深度参与的旁观状态。"
    assert len(tail) == 36
    warm_head, _ = assemble_messages(make_snapshot())
    assert head == warm_head


# F8 -- cold tier also honours the pure contract on the tail
def test_f8_cold_pure_false_appends_second_seal() -> None:
    snap = make_snapshot(compute_tier="cold")
    _h, tail_true = assemble_messages(snap, pure=True)
    _h, tail_false = assemble_messages(snap, pure=False)
    assert tail_false.endswith(_SEAL_LINE)
    assert tail_false[: -len("\n" + _SEAL_LINE)] == tail_true


# F9 -- the 50-char budget governs the body only, not the whole tail
def test_f9_budget_covers_body_not_whole_tail() -> None:
    snap = make_snapshot()
    _h, tail = assemble_messages(snap, pure=False)
    lines = tail.split("\n")
    body_lines = lines[1:-1]  # drop label line and second seal line
    assert _body_length(body_lines) <= MAX_INJECTION_BODY_CHARS
    # the whole tail is of course longer than the budget: the budget is not
    # a whole-tail cap
    assert len(tail) > MAX_INJECTION_BODY_CHARS


# F10 -- both entries share one body construction (single implementation)
# Same ``pure`` on both entries: the prompt then ends with the seal line and
# the messages tail does not, so the seal line is stripped from the prompt
# before the byte-for-byte comparison.
def test_f10_messages_and_prompt_share_body_bytes() -> None:
    snap = make_snapshot(conflicted=True)
    entry = PhenomenonEntry(
        conditions={"cortisol": ">0"},
        long_somatic="肩胛骨之间有一块地方绷得很紧。",
        long_drive="你想开口，又把话咽了回去。",
        long_block="空气安静了两秒。",
        short_somatic="呼吸浅了一拍。",
        short_drive="话到嘴边停住。",
        short_block="安静了两秒。",
    )
    phenomenology = {"cold_anger": entry}
    prompt = assemble_prompt(snap, pure=True, phenomenology=phenomenology)
    _h, tail = assemble_messages(snap, pure=True, phenomenology=phenomenology)
    # strip label line from both; strip the seal line from the prompt
    prompt_body = prompt[len(_LABEL_LINE): -len("\n" + _SEAL_LINE)]
    tail_body = tail[len(_LABEL_LINE):]
    assert tail_body == prompt_body


# F11 -- assemble_prompt zero regression on representative shapes
def test_f11_prompt_structure_unchanged() -> None:
    snap = make_snapshot()
    prompt_true = assemble_prompt(snap, pure=True)
    assert prompt_true.startswith(_LABEL_LINE)
    assert prompt_true.endswith(_SEAL_LINE)
    prompt_false = assemble_prompt(snap, pure=False)
    assert not prompt_false.endswith(_SEAL_LINE)
    assert _SEAL_LINE not in prompt_false


# F12 -- AST: no loop, no literal label/seal strings in assemble_messages
def test_f12_messages_body_is_not_duplicated() -> None:
    source = inspect.getsource(assemble_messages)
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "assemble_messages"
    )
    for node in ast.walk(fn):
        assert not isinstance(node, (ast.For, ast.While, ast.AsyncFor)), (
            f"body construction loop inside assemble_messages: {ast.dump(node)[:80]}"
        )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in ("【潜意识感知】", "【输出格式封印】"), (
                "literal label/seal text must come from the shared constants"
            )
