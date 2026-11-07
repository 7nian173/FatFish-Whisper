"""Cold-tier must not even LOOK at the lexicon (task-FFW-P25-T3R).

A separate file, not an append to ``test_tier_gate.py``: that file locks the
cold tier's *output* (verbatim narration, no segment labels); this file locks
its *behaviour* -- how many times the lexicon is actually queried.  Different
observable, different failure modes, so the locks live apart.

``test_cold_assembly_ignores_lexicon_entirely`` passes ``lexicon={}``: that
only proves the cold path does not NEED the lexicon (an empty dict does not
crash).  It cannot distinguish "never queried" from "queried then discarded" --
an implementation that assembles all three segments first and only then
returns ``COLD_NARRATION`` passes every output assertion.  A recording dict
proves the stronger claim: the cold path performs ZERO ``lexicon.get`` calls,
so the compute saving the human asked for (cold must not consult the big
dictionary) is actually realised.
"""

from __future__ import annotations

import pytest

from packages.core.engine import assemble_prompt
from tests.engine.test_tier_gate import make_snapshot


class _RecordingLexicon(dict):
    """A dict that counts .get() calls, so we can assert cold never looks up."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.get_calls: list[str] = []

    def get(self, key, default=None):  # type: ignore[override]
        self.get_calls.append(key)
        return super().get(key, default)


@pytest.fixture()
def recording_lexicon() -> _RecordingLexicon:
    return _RecordingLexicon(
        tsundere={"high": "h", "low": "l"},
        shyness={"high": "h", "low": "l"},
    )


def test_cold_tier_never_queries_lexicon(recording_lexicon: _RecordingLexicon) -> None:
    prompt = assemble_prompt(make_snapshot("cold"), lexicon=recording_lexicon)
    assert prompt != ""
    assert recording_lexicon.get_calls == []


def test_cold_tier_unsealed_never_queries_lexicon(
    recording_lexicon: _RecordingLexicon,
) -> None:
    assemble_prompt(make_snapshot("cold"), lexicon=recording_lexicon, pure=False)
    assert recording_lexicon.get_calls == []


def test_warm_tier_does_query_lexicon(recording_lexicon: _RecordingLexicon) -> None:
    """Anti-tautology: a fake engine that never calls .get must fail here."""
    assemble_prompt(make_snapshot("warm"), lexicon=recording_lexicon, pure=False)
    assert len(recording_lexicon.get_calls) >= 1
