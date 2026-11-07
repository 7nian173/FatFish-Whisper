"""Phenomenology acceptance tests R1-R10 (task-FFW-P29-SIM section 2.7).

Each test corresponds to one R-line of the card.  The entry data is copied
verbatim from the ADR ``20_文档区/02_架构决策记录(ADR)/
现象学词典定义.md`` (state_anger_cold_glare); the snapshot/pulse
fixtures below
hand-construct every value -- no trait evolution, no IO, no clock.
"""

from __future__ import annotations

from datetime import UTC, datetime

from packages.core.domain.global_context import PulseState
from packages.core.domain.state import EmotionValue, IPCState, S1Snapshot, TraitState
from packages.core.engine.assembler import (
    MAX_INJECTION_BODY_CHARS,
    MOCK_LEXICON,
    PhenomenonEntry,
    assemble_prompt,
)
from packages.core.engine.assembler import (
    _body_length as body_length,
)

TS = datetime(2026, 11, 4, 12, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {
    "anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL),
    "joy": EmotionValue(intensity=10.0, ttl_expires_at=TTL),
}

#: ADR entry 1, copied verbatim (long somatic = 24 code points).
ENTRY = PhenomenonEntry(
    conditions={"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"},
    long_somatic="你眼神冷下来，下颌绷紧，呼吸变浅，指尖却微微发颤。",
    long_drive="你想把怒意压成一句冷静的质问，保持掌控。",
    long_block="火气冲到嘴边又被你咽回去，只剩短促的停顿。",
    short_somatic="眼神冷，下颌紧。",
    short_drive="想压住火。",
    short_block="话变短。",
)
MAP = {"state_anger_cold_glare": ENTRY}

MAIN = "眼神冷，下颌紧。"
DRIVE = "想压住火。"
BLOCK = "话变短。"


def make_snapshot(
    *,
    conflicted: bool = True,
    cold_anger: str = "unlocked",
) -> S1Snapshot:
    traits = {
        "cold_anger": TraitState(score=80, visibility=cold_anger),
        "tsundere": TraitState(score=85, visibility="unlocked"),
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


def make_pulse(*, cortisol: float = 90.0, dopamine: float = 30.0) -> PulseState:
    return PulseState(cortisol=cortisol, dopamine=dopamine, oxytocin=50.0)


# R1a: conditions hold -> the entry hits, main segment carries its somatic text.
# pure=True so the (over-cap) long body renders verbatim; degradation is R4's job.
def test_r1_conditions_hit() -> None:
    out = assemble_prompt(
        make_snapshot(), phenomenology=MAP, pulse=make_pulse(), pure=True
    )
    assert "[主场]：" + ENTRY.long_somatic in out.split("\n")


# R1b: dopamine outside the condition -> no hit, falls back to the trait lexicon.
def test_r1_dopamine_out_of_range_falls_back() -> None:
    out = assemble_prompt(
        make_snapshot(),
        phenomenology=MAP,
        pulse=make_pulse(dopamine=60.0),
        pure=False,
    )
    assert "[主场]：被叫笨蛋时，你感到一阵烦躁，指尖发烫。" in out.split("\n")
    assert ENTRY.long_somatic not in out


# R2: pulse=None with hormone conditions -> no hit, no exception.
def test_r2_pulse_none_means_no_match() -> None:
    out = assemble_prompt(make_snapshot(), phenomenology=MAP, pulse=None, pure=False)
    assert ENTRY.long_somatic not in out
    assert "[主场]：被叫笨蛋时，你感到一阵烦躁，指尖发烫。" in out.split("\n")


# R3: pure=True never degrades even when the long body exceeds the cap.
def test_r3_pure_true_keeps_long_over_limit() -> None:
    big = PhenomenonEntry(
        conditions={"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"},
        long_somatic="超" * 60,
        long_drive="压" * 30,
        long_block="咽" * 30,
        short_somatic=MAIN,
        short_drive=DRIVE,
        short_block=BLOCK,
    )
    out = assemble_prompt(
        make_snapshot(),
        phenomenology={"state_big": big},
        pulse=make_pulse(),
        pure=True,
    )
    assert "[主场]：" + "超" * 60 in out


# R4: pure=False over limit -> all three segments swapped to short, under cap.
def test_r4_over_limit_swaps_to_short() -> None:
    big = PhenomenonEntry(
        conditions={"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"},
        long_somatic="超" * 60,
        long_drive="压" * 30,
        long_block="咽" * 30,
        short_somatic=MAIN,
        short_drive=DRIVE,
        short_block=BLOCK,
    )
    out = assemble_prompt(
        make_snapshot(),
        phenomenology={"state_big": big},
        pulse=make_pulse(),
        pure=False,
    )
    body = [line for line in out.split("\n") if line.startswith("[")]
    assert "[主场]：" + MAIN in body
    assert "[拉扯]：" + DRIVE in body
    assert "[旁边杵着]：" + BLOCK in body
    assert body_length([line.split("：", 1)[1] for line in body]) <= MAX_INJECTION_BODY_CHARS


# R5: short still over (40+15+16=71 > 50) -> bystander dropped first, tension stays
# (40+15=55... drive line is 15: 5+1+9... actual: 40+15=55 still over -> shorter drive).
def test_r5_degrade_drops_bystander_first() -> None:
    big = PhenomenonEntry(
        conditions={"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"},
        long_somatic="超" * 60,
        long_drive="压" * 30,
        long_block="咽" * 30,
        short_somatic="冷" * 24,  # line = 5+1+24 = 30
        short_drive="压" * 9,  # line = 4+1+9 = 14
        short_block="咽" * 10,  # line = 5+1+10 = 16
    )
    out = assemble_prompt(
        make_snapshot(),
        phenomenology={"state_big": big},
        pulse=make_pulse(),
        pure=False,
    )
    assert "[旁边杵着]" not in out
    assert "[拉扯]" in out


# R6: after bystander drops still over (30+14=44 -> use bigger main) -> tension
# dropped too, main only.  main line 5+1+44=50... need main+tension > 50:
# main 44 chars -> line 50, tension line 14 -> 64 > 50 -> drop tension -> 50 <= 50 stays.
def test_r6_degrade_drops_tension_second() -> None:
    big = PhenomenonEntry(
        conditions={"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"},
        long_somatic="超" * 60,
        long_drive="压" * 30,
        long_block="咽" * 30,
        short_somatic="冷" * 44,  # line = 5+1+44 = 50
        short_drive="压" * 9,  # line = 14
        short_block="咽" * 10,  # line = 16
    )
    out = assemble_prompt(
        make_snapshot(),
        phenomenology={"state_big": big},
        pulse=make_pulse(),
        pure=False,
    )
    body = [line for line in out.split("\n") if line.startswith("[")]
    assert len(body) == 1
    assert body[0].startswith("[主场]：")


# R7: single main line still over -> character trim, whole line exactly 50
# code points (_body_length counts the full rendered line incl. label).
def test_r7_single_line_trim_is_exact_fifty() -> None:
    big = PhenomenonEntry(
        conditions={"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"},
        long_somatic="超" * 60,
        long_drive="压" * 30,
        long_block="咽" * 30,
        short_somatic="冷" * 80,  # line = 86 -> trim
        short_drive="压" * 30,
        short_block="咽" * 30,
    )
    out = assemble_prompt(
        make_snapshot(),
        phenomenology={"state_big": big},
        pulse=make_pulse(),
        pure=False,
    )
    body = [line for line in out.split("\n") if line.startswith("[")]
    assert len(body) == 1
    assert body_length([body[0]]) == MAX_INJECTION_BODY_CHARS


# R8: no conflict -> no tension line even on the entry-hit path.
# pure=True keeps the long body verbatim (over-cap degradation is R4's job).
def test_r8_no_conflict_no_tension_segment() -> None:
    out = assemble_prompt(
        make_snapshot(conflicted=False),
        phenomenology=MAP,
        pulse=make_pulse(),
        pure=True,
    )
    assert "[拉扯]" not in out
    assert "[主场]：" + ENTRY.long_somatic in out.split("\n")


# R9: same input, pure=True vs pure=False outputs must differ (anti-vacuous).
def test_r9_pure_and_host_renders_differ() -> None:
    big = PhenomenonEntry(
        conditions={"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"},
        long_somatic="超" * 60,
        long_drive="压" * 30,
        long_block="咽" * 30,
        short_somatic=MAIN,
        short_drive=DRIVE,
        short_block=BLOCK,
    )
    pure = assemble_prompt(
        make_snapshot(),
        phenomenology={"state_big": big},
        pulse=make_pulse(),
        pure=True,
    )
    host = assemble_prompt(
        make_snapshot(),
        phenomenology={"state_big": big},
        pulse=make_pulse(),
        pure=False,
    )
    assert pure != host


# R10: slot-to-segment direction -- somatic lands in [主场], drive in [拉扯],
# block in [旁边杵着]; each asserted with its own text (anti "all present").
# pure=True keeps long bodies verbatim (over-cap degradation is R4's job).
def test_r10_slot_direction_mapping() -> None:
    out = assemble_prompt(
        make_snapshot(), phenomenology=MAP, pulse=make_pulse(), pure=True
    )
    lines = out.split("\n")
    main_line = next(line for line in lines if line.startswith("[主场]"))
    tension_line = next(line for line in lines if line.startswith("[拉扯]"))
    block_line = next(line for line in lines if line.startswith("[旁边杵着]"))
    assert main_line.endswith(ENTRY.long_somatic)
    assert tension_line.endswith(ENTRY.long_drive)
    assert block_line.endswith(ENTRY.long_block)
    # Entry hit -> the legacy bystander copy must not leak in.
    assert "空气安静了两秒。" not in out


# Guard for the legacy fallback path (card section 2.6): no phenomenology ->
# byte-for-byte legacy behaviour; MOCK_LEXICON stays reachable.
def test_legacy_path_untouched_without_phenomenology() -> None:
    out = assemble_prompt(
        make_snapshot(conflicted=False), lexicon=MOCK_LEXICON, pure=False
    )
    assert "[旁边杵着]：空气安静了两秒。" in out.split("\n")
    assert "[主场]：被叫笨蛋时，你感到一阵烦躁，指尖发烫。" in out.split("\n")
