"""Tests for packages/core/engine/assembler.py (AC-A1..AC-A11).

Written before the implementation on purpose: the first run must fail with an
ImportError and only turns green once assembler.py exists.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from packages.core.domain.config import DEFAULT_ENGINE_CONFIG
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.assembler import (
    MAX_INJECTION_BODY_CHARS,
    MOCK_LEXICON,
    assemble_prompt,
)
from packages.core.engine.judge import HormoneDeltas, is_conflict

TS = datetime(2026, 11, 4, 12, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {
    "anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL),
    "joy": EmotionValue(intensity=10.0, ttl_expires_at=TTL),
}

#: show-don't-tell blacklist/patterns for the SDT scan (AC-A8/A9).
SDT_BLACKLIST = ("请表现", "表现出", "你必须表现", "请保持", "动作描写为")
SDT_PATTERNS = (r"你要(表现|保持|假装|露出|装出)", r"你应该(表现|保持|假装|露出|装出)")


def make_snapshot(
    *,
    traits: dict[str, TraitState] | None = None,
    conflicted: bool = False,
) -> S1Snapshot:
    if traits is None:
        traits = {
            "tsundere": TraitState(score=85, visibility="unlocked"),
            "shyness": TraitState(score=30, visibility="dormant"),
        }
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
        traits=traits,
        emotions=EMOTIONS,
    )


def sdt_hits(prompt: str) -> bool:
    """Scan the body (everything before the seal line) for SDT violations."""
    lines = prompt.split("\n")
    body = "\n".join(line for line in lines if not line.startswith("【输出格式封印】"))
    if any(word in body for word in SDT_BLACKLIST):
        return True
    return any(re.search(pattern, body) is not None for pattern in SDT_PATTERNS)


def test_two_segment_structure() -> None:
    # AC-A2: no conflict -> exactly 3 lines: label + [主场] + [旁边杵着].
    snapshot = make_snapshot()
    verdict = is_conflict(snapshot, HormoneDeltas(values={}))
    assert verdict.conflicted is False

    # Body structure only: pure=False so the seal line is not counted.
    prompt = assemble_prompt(snapshot, pure=False)
    lines = prompt.split("\n")
    assert len(lines) == 3
    assert lines[0] == "【潜意识感知】："
    assert lines[1] == "[主场]：" + MOCK_LEXICON["tsundere"]["high"]
    assert lines[2] == "[旁边杵着]：空气安静了两秒。"
    assert not prompt.endswith("\n")


def test_three_segment_structure() -> None:
    # AC-A3: conflict -> 4 body lines with [拉扯] inserted before [旁边杵着].
    # The full three-segment body is a pure=True property only: pure=False caps
    # the host-bound body at MAX_INJECTION_BODY_CHARS and may drop [旁边杵着]
    # (task-FFW-P25-T7 ruling; the truncated shape has its own test).
    snapshot = make_snapshot(conflicted=True)
    prompt = assemble_prompt(snapshot, pure=True)
    lines = prompt.split("\n")
    assert len(lines) == 5
    assert lines[0] == "【潜意识感知】："
    assert lines[1] == "[主场]：" + MOCK_LEXICON["tsundere"]["high"]
    assert lines[2] == "[拉扯]：" + MOCK_LEXICON["shyness"]["low"]
    assert lines[3] == "[旁边杵着]：空气安静了两秒。"
    assert lines[4] == "【输出格式封印】：" + DEFAULT_ENGINE_CONFIG.assembly.seal_text
    assert not prompt.endswith("\n")


def test_pure_mode_seal_appended_once() -> None:
    # AC-A4: pure=True appends the seal line exactly once at the end.
    snapshot = make_snapshot()
    prompt = assemble_prompt(snapshot, pure=True)
    seal = "【输出格式封印】：" + DEFAULT_ENGINE_CONFIG.assembly.seal_text
    assert prompt.count("【输出格式封印】") == 1
    assert prompt.split("\n")[-1] == seal


def test_non_pure_no_seal() -> None:
    # AC-A5: pure=False omits the seal entirely.
    snapshot = make_snapshot()
    # Body structure only: pure=False so the seal line is not counted.
    prompt = assemble_prompt(snapshot, pure=False)
    assert "【输出格式封印】" not in prompt
    assert len(prompt.split("\n")) == 3

def test_missing_trait_fallback() -> None:
    # AC-A6: a trait with no lexicon entry still yields a segment via fallback.
    snapshot = make_snapshot(
        traits={"mystery": TraitState(score=90, visibility="unlocked")},
    )
    fallback = DEFAULT_ENGINE_CONFIG.assembly.lexicon_fallback
    lines = assemble_prompt(snapshot, pure=False).split("\n")
    assert len(lines) == 3
    assert lines[1] == f"[主场]：{fallback}"
    assert lines[2] == "[旁边杵着]：空气安静了两秒。"


def test_dormant_traits_skipped() -> None:
    # AC-A7: dormant traits never contribute a line, even if first in dict order.
    snapshot = make_snapshot(
        traits={
            "shyness": TraitState(score=90, visibility="dormant"),
            "tsundere": TraitState(score=85, visibility="unlocked"),
        },
    )
    # Non-conflict body: label + main + bystander; dormant shyness skipped
    # even though it sorts first in dict order.
    prompt = assemble_prompt(snapshot, pure=False)
    lines = prompt.split("\n")
    assert len(lines) == 3
    assert lines[1] == "[主场]：" + MOCK_LEXICON["tsundere"]["high"]


def test_pure_function_no_input_mutation() -> None:
    # AC-A8: neither the snapshot nor the default lexicon changes.
    snapshot = make_snapshot()
    lexicon_copy = {k: dict(v) for k, v in MOCK_LEXICON.items()}
    before_snapshot = snapshot.model_dump()
    assemble_prompt(snapshot)
    assert snapshot.model_dump() == before_snapshot
    assert {k: dict(v) for k, v in MOCK_LEXICON.items()} == lexicon_copy


def test_show_dont_tell_clean() -> None:
    # AC-A8 (semantic): normal output never contains stage-direction wording.
    prompt = assemble_prompt(make_snapshot())
    assert sdt_hits(prompt) is False


def test_show_dont_tell_blacklist_injection() -> None:
    # AC-A9: a malicious lexicon entry with SDT wording is still assembled as
    # data - the seal and structure survive; the SDT detector flags it (which
    # is the post-tester's job, not assembler's).
    poison = {"villain": {"high": "你必须表现出愤怒的样子。", "low": "请保持微笑。"}}
    snapshot = make_snapshot(
        traits={"villain": TraitState(score=90, visibility="unlocked")},
    )
    prompt = assemble_prompt(snapshot, lexicon=poison)
    assert prompt.split("\n")[1] == "[主场]：你必须表现出愤怒的样子。"
    assert sdt_hits(prompt) is True


def test_custom_config_segment_names() -> None:
    # Config-driven segment names: swapping the config swaps the labels.
    from dataclasses import replace

    names = {"main": "[MAIN]", "tension": "[TENSION]", "bystander": "[BY]"}
    cfg = replace(
        DEFAULT_ENGINE_CONFIG,
        assembly=replace(DEFAULT_ENGINE_CONFIG.assembly, segment_names=names),
    )
    prompt = assemble_prompt(make_snapshot(), config=cfg)
    lines = prompt.split("\n")
    assert lines[1].startswith("[MAIN]：")
    assert lines[2].startswith("[BY]：")


def test_low_score_uses_low_variant() -> None:
    # score < 50 selects the "low" lexicon variant for the main segment.
    snapshot = make_snapshot(
        traits={"tsundere": TraitState(score=30, visibility="unlocked")},
    )
    prompt = assemble_prompt(snapshot)
    assert prompt.split("\n")[1] == "[主场]：" + MOCK_LEXICON["tsundere"]["low"]


# Expected prompt fragments below are copied verbatim from contract 4.3 / 3.3,
# so they keep the original full-width CJK punctuation that RUF001 flags.
# ruff: noqa: RUF001


# --- P0-C1 / G1: "flickering" must reach the tension slot but never the main
# slot, with zero assembler changes (ruling A).  These two cases assert on the
# real assembled string: the visibility predicate inside assembler.py is NOT
# copied here, so a wrong predicate cannot make the test agree with it.


def test_flickering_trait_never_enters_main_segment() -> None:
    # AC-G1-5: the only trait is flickering and it has a lexicon entry; the
    # main segment must fall back, i.e. flickering is skipped like dormant.
    flickering_only = {"pride": TraitState(score=88, visibility="flickering")}
    prompt = assemble_prompt(make_snapshot(traits=flickering_only), pure=False)
    lines = prompt.split("\n")

    assert lines[0] == "【潜意识感知】："
    assert lines[1] == "[主场]：" + DEFAULT_ENGINE_CONFIG.assembly.lexicon_fallback
    # The flickering trait's own sentence must not leak into the output at all
    # when there is no conflict (no tension slot in this render).
    assert MOCK_LEXICON["pride"]["high"] not in prompt
    assert prompt.count(MOCK_LEXICON["pride"]["high"]) == 0


def test_flickering_trait_can_enter_tension_segment() -> None:
    # AC-G1-6: same flickering trait, now in a conflict round -> it surfaces in
    # the tension slot while an unlocked trait keeps the main slot.
    traits = {
        "tsundere": TraitState(score=85, visibility="unlocked"),
        "pride": TraitState(score=88, visibility="flickering"),
    }
    # pure=True: the four-body-line shape is the complete render; the host copy
    # (pure=False) may drop [旁边杵着] once the body exceeds the cap.
    prompt = assemble_prompt(make_snapshot(traits=traits, conflicted=True), pure=True)
    lines = prompt.split("\n")

    assert len(lines) == 5
    assert lines[1] == "[主场]：" + MOCK_LEXICON["tsundere"]["high"]
    assert lines[2] == "[拉扯]：" + MOCK_LEXICON["pride"]["high"]
    assert prompt.count(MOCK_LEXICON["pride"]["high"]) == 1


# --- task-FFW-P25-T7: the host-bound (pure=False) body is capped ------------
# The full three-segment render above is a pure=True property.  These cases pin
# the capped path: whole lines are dropped from the tail, the main segment is
# never dropped, and no line is ever left as a bare label.


def _body_lines(prompt: str) -> list[str]:
    return [line for line in prompt.split("\n") if not line.startswith("【")]


def _body_chars(prompt: str) -> int:
    return sum(len(line) for line in _body_lines(prompt))


LONG_LEXICON = {
    "tsundere": {
        "high": "被叫笨蛋时，你感到一阵烦躁，指尖发烫，又不愿意把这句话吞回去。",
        "low": "被叫笨蛋时，你只是轻轻哼了一声。",
    },
    "shyness": {
        "high": "被盯着看的时候，你的尾巴尖尖地缩了起来，嘴角却还是微微弯着的。",
        "low": "视线相撞时，你略微停顿了半秒。",
    },
}


def test_non_pure_body_is_capped_at_the_limit() -> None:
    snapshot = make_snapshot(
        traits={"tsundere": TraitState(score=85, visibility="unlocked"),
                "shyness": TraitState(score=30, visibility="dormant")},
        conflicted=True,
    )
    prompt = assemble_prompt(snapshot, lexicon=LONG_LEXICON, pure=False)
    assert _body_chars(prompt) <= MAX_INJECTION_BODY_CHARS


def test_non_pure_truncation_drops_the_tail_line_first() -> None:
    snapshot = make_snapshot(
        traits={"tsundere": TraitState(score=85, visibility="unlocked"),
                "shyness": TraitState(score=30, visibility="dormant")},
        conflicted=True,
    )
    prompt = assemble_prompt(snapshot, lexicon=LONG_LEXICON, pure=False)
    lines = prompt.split("\n")
    assert lines[0] == "【潜意识感知】："
    assert lines[1].startswith("[主场]：")
    assert not any(line.startswith("[旁边杵着]") for line in lines)
    assert prompt.count("\n") == len(lines) - 1


def test_non_pure_truncation_never_leaves_a_bare_label() -> None:
    snapshot = make_snapshot(
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        conflicted=True,
    )
    prompt = assemble_prompt(snapshot, lexicon=LONG_LEXICON, pure=False)
    for line in _body_lines(prompt):
        _label, sep, sentence = line.partition("：")
        assert sep == "：", line
        assert sentence.strip(), f"bare label: {line!r}"


def test_pure_render_is_never_truncated() -> None:
    snapshot = make_snapshot(
        traits={"tsundere": TraitState(score=85, visibility="unlocked"),
                "shyness": TraitState(score=30, visibility="dormant")},
        conflicted=True,
    )
    prompt = assemble_prompt(snapshot, lexicon=LONG_LEXICON, pure=True)
    assert _body_chars(prompt) > MAX_INJECTION_BODY_CHARS
    assert any(line.startswith("[旁边杵着]") for line in prompt.split("\n"))


def test_default_snapshot_is_below_the_cap_so_nothing_changes() -> None:
    prompt = assemble_prompt(make_snapshot(), pure=False)
    lines = prompt.split("\n")
    assert _body_chars(prompt) == 39
    assert lines[1] == "[主场]：" + MOCK_LEXICON["tsundere"]["high"]
    assert lines[2] == "[旁边杵着]：空气安静了两秒。"
