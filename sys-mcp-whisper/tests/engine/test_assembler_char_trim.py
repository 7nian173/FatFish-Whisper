"""Lock the single-line character-trim branch of ``_truncate_body`` (T7R).

``assembler._truncate_body`` has two paths: drop whole lines from the tail, and
- when one line alone still exceeds the cap - trim that line's sentence and end
it with an ellipsis.  T7 covered the first path only: every lexicon entry in the
repo produced a main line short enough that the cap was reached by dropping
lines, so the second path was never executed.  Three mutations of that branch
(dropping it, ``room - 1``, ``room - 2``) left the whole suite green.

The guard is an overlong entry: with a single unlocked trait the main line is
the only body line that cannot be dropped, so the character trim has to run.
The assertion is ``== MAX_INJECTION_BODY_CHARS`` on purpose.  ``<= 50`` would
catch a room that is one too large (51) but would stay green for a room that is
one too small (49), so only equality pins both directions.

A short entry is kept as a reverse control: an implementation that appended an
ellipsis to every line and trimmed unconditionally would pass the overlong case
and fail that one.

Full-width punctuation is written as escapes here so the source stays readable
to ruff without a file-level suppression.
"""

from __future__ import annotations

from datetime import UTC, datetime

from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.assembler import (
    MAX_INJECTION_BODY_CHARS,
    _body_length,
    assemble_prompt,
)

TS = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
EMOTIONS = {"joy": EmotionValue(intensity=10.0, ttl_expires_at=TS)}

#: 93 code points, so the main line is 98 with the label prefix - far above the
#: 50 cap.  A short entry here would make every case below vacuous.
OVERLONG_SENTENCE = (
    "被叫笨蛋的时候你没有真的生气\uff0c只是把脸转到一边去假装在看窗外那棵树"
    "其实一直在偷偷用余光瞄着对方有没有回头看过来又怕被发现所以很快就收回了视线"
    "过了一会儿你又忍不住想问刚才那句话到底算不算数"
)
SHORT_SENTENCE = "被叫笨蛋时\uff0c你只是轻轻哼了一声。"

OVERLONG_LEXICON = {
    "tsundere": {
        "high": OVERLONG_SENTENCE,
        "low": SHORT_SENTENCE,
    },
}

SHORT_LEXICON = {
    "tsundere": {
        "high": SHORT_SENTENCE,
        "low": SHORT_SENTENCE,
    },
}


def make_snapshot() -> S1Snapshot:
    """One unlocked trait only, conflicted, so the main line anchors the body."""
    return S1Snapshot(
        version=1,
        scope_id="scope-1",
        timestamp=TS,
        last_decay_at=TS,
        is_conflict=True,
        ipc=IPCState(
            affiliation=0.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        emotions=EMOTIONS,
    )


def body_lines(prompt: str) -> list[str]:
    """Segment lines only: the label line and the seal line both start with a bracket."""
    return [line for line in prompt.split("\n") if line.startswith("[")]


def render(lexicon: dict[str, dict[str, str]]) -> str:
    return assemble_prompt(make_snapshot(), lexicon=lexicon, pure=False)


def test_overlong_entry_really_exceeds_the_cap() -> None:
    """Guard the fixture itself, otherwise the cases below prove nothing."""
    untrimmed = len("[主场]\uff1a") + len(OVERLONG_SENTENCE)
    assert untrimmed > MAX_INJECTION_BODY_CHARS
    assert len(OVERLONG_SENTENCE) >= 78
    prompt = render(OVERLONG_LEXICON)
    assert body_lines(prompt)[0].startswith("[主场]\uff1a")


def test_body_is_trimmed_to_exactly_the_cap() -> None:
    prompt = render(OVERLONG_LEXICON)
    assert _body_length(body_lines(prompt)) == MAX_INJECTION_BODY_CHARS


def test_trimmed_line_ends_with_an_ellipsis() -> None:
    prompt = render(OVERLONG_LEXICON)
    line = body_lines(prompt)[0]
    assert line.endswith("\u2026")
    assert line.count("\u2026") == 1


def test_every_line_keeps_a_non_empty_body() -> None:
    prompt = render(OVERLONG_LEXICON)
    for line in body_lines(prompt):
        label, separator, sentence = line.partition("\uff1a")
        assert label.startswith("[") and label.endswith("]"), line
        assert separator == "\uff1a", line
        assert sentence.strip("\u2026").strip(), f"bare label: {line!r}"


def test_the_dropped_tail_is_absent_from_the_prompt() -> None:
    prompt = render(OVERLONG_LEXICON)
    assert OVERLONG_SENTENCE[-5:] not in prompt
    assert OVERLONG_SENTENCE[:20] in prompt


def test_a_short_entry_is_left_untouched() -> None:
    """Reverse control: unconditional trimming would fail here."""
    prompt = render(SHORT_LEXICON)
    lines = body_lines(prompt)
    assert lines[0] == "[主场]\uff1a" + SHORT_SENTENCE
    assert "\u2026" not in prompt
    assert _body_length(lines) < MAX_INJECTION_BODY_CHARS
