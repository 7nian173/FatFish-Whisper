"""SCENARIO_TAGS vocabulary locks (task-FFW-P0-C8).

C6 already guards the vocabulary in ``tests/engine/test_routing.py`` (the
verbatim six-tuple comparison and the two-copy drift lock).  A pre-work grep
enumerated every existing assertion first; the directions C6 already covers
are deliberately NOT duplicated here.  This module fills the gaps:

* hygiene -- no duplicates, non-empty, lowercase with underscores (card 2.3);
* exhaustive proof that every Mock lexicon outcome lands in the vocabulary
  (card 2.4 -- C6 sampled one keyword, and sampling silently misses new
  rules), by reflection over ``routing._MOCK_RULES``, never a hand-written
  keyword list;
* the reverse default assertion (card 2.4) -- an out-of-lexicon text must
  yield ``"neutral"``, not ``None`` / ``""`` / ``"unknown"``;
* mutation evidence (card section 5, non-tautology requirement): each lock is
  executed once against a deliberately mutated source and must turn red.
  Per task-FFW-P0-C8R, the mutation happens inside a
  ``tempfile.TemporaryDirectory()`` copy of ``packages/`` + ``tests/`` +
  ``pyproject.toml`` -- the production tree stays read-only (guarded by a
  sha256 before/after behaviour lock), so concurrent pytest runs can never
  observe a mutated source and a killed process can never leave one behind.

The appendix-H verbatim check is a documentation finding recorded in the task
report; it is deliberately not machine-checked here because reading the docx
from the suite would couple ``sys-mcp-whisper`` to files outside the repo.
"""

from __future__ import annotations

import hashlib
import itertools
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from packages.core.domain.state import S1Snapshot
from packages.core.engine import routing
from packages.core.engine.routing import SCENARIO_TAGS, Segment, route_track_a

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTING_SOURCE = REPO_ROOT / "packages" / "core" / "engine" / "routing.py"
CONTRACTS_SOURCE = REPO_ROOT / "packages" / "core" / "domain" / "contracts.py"

# C6's node that compares both copies element by element; mutations 1-3 must
# all turn this one red (it is the only place that reads contracts._SCENARIO_TAGS).
DRIFT_LOCK_NODE = (
    "tests/engine/test_routing.py::"
    "test_scenario_tag_vocabulary_matches_the_engine_constant"
)
# The node this card adds for the reverse default; mutation 4 must turn it red.
DEFAULT_NEUTRAL_NODE = (
    "tests/engine/test_scenario_tags_locks.py::"
    "test_out_of_lexicon_text_defaults_to_neutral"
)

# Mutation anchors (card section 5).  Each is asserted unique before use.
MUTATION_ROUTING_DROP = ('    "teasing_playful",\n', "")
MUTATION_CONTRACTS_DROP = ('    "warm_open",\n', "")
MUTATION_ROUTING_ADD = (
    '    "distressed_fragile",\n)',
    '    "distressed_fragile",\n    "extra_tag",\n)',
)
MUTATION_DEFAULT_UNKNOWN = ('return {}, "neutral"', 'return {}, "unknown"')


def _snapshot() -> S1Snapshot:
    """A minimal valid snapshot; routing only reads ``scope_id``."""
    return S1Snapshot.model_validate(
        {
            "version": 1,
            "scope_id": "chat_8842",
            "timestamp": "2026-11-05T22:00:00Z",
            "last_decay_at": "2026-11-05T21:00:00Z",
            "is_conflict": False,
            "ipc": {
                "affiliation": 40.0,
                "dominance": 60.0,
                "relationship_stage": "friend",
                "relationship_ceiling": "lover",
            },
            "traits": {},
            "emotions": {},
        }
    )


# ---------------------------------------------------------------------------
# Card 2.3: hygiene -- the assertions C6 did not write
# ---------------------------------------------------------------------------


def test_scenario_tags_no_duplicates_all_lowercase_underscored() -> None:
    assert len(SCENARIO_TAGS) == len(set(SCENARIO_TAGS))
    assert all(tag and tag == tag.lower() and " " not in tag for tag in SCENARIO_TAGS)


# ---------------------------------------------------------------------------
# Card 2.4: exhaustive lexicon -> vocabulary coverage, by reflection only
# ---------------------------------------------------------------------------


def test_every_mock_rule_tag_literal_is_vocabulary_member() -> None:
    """Static lock: every tag the Mock rules declare must be a vocabulary member."""
    assert len(routing._MOCK_RULES) >= 1, "the Mock lexicon must not be empty"
    for keyword, _, tag in routing._MOCK_RULES:
        assert tag in SCENARIO_TAGS, f"rule {keyword!r} declares tag {tag!r} outside the vocabulary"


def test_every_mock_keyword_yields_in_vocabulary_tag() -> None:
    """Run every lexicon keyword through route_track_a; no hand-written list."""
    snapshot = _snapshot()
    for keyword, _, _ in routing._MOCK_RULES:
        result = route_track_a([Segment(role="user", text=keyword)], snapshot)
        assert result.scenario_tag in SCENARIO_TAGS, f"keyword {keyword!r}"


def test_every_keyword_combination_yields_in_vocabulary_tag() -> None:
    """All non-empty keyword combinations: the tags[0] pick can never escape."""
    snapshot = _snapshot()
    keywords = [rule[0] for rule in routing._MOCK_RULES]
    for size in range(1, len(keywords) + 1):
        for combo in itertools.combinations(keywords, size):
            text = "".join(combo)
            result = route_track_a([Segment(role="user", text=text)], snapshot)
            assert result.scenario_tag in SCENARIO_TAGS, f"text {text!r}"


def test_out_of_lexicon_text_defaults_to_neutral() -> None:
    """Reverse default (card 2.4): a no-hit text must yield ``neutral``, never None/""/unknown."""
    snapshot = _snapshot()
    text = "今天天气不错"
    for keyword, _, _ in routing._MOCK_RULES:
        assert keyword not in text, "fixture text must miss every lexicon rule"

    result = route_track_a([Segment(role="user", text=text)], snapshot)
    assert result.scenario_tag == "neutral"
    assert result.scenario_tag not in ("", "unknown", None)
    assert result.stimulus_delta == {}


# ---------------------------------------------------------------------------
# Card section 5: mutation evidence -- non-tautology, sha256-restored
# ---------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_mutation_turns_lock_red(
    source: Path, old: str, new: str, node_id: str
) -> None:
    """Mutate a *temporary copy*, run the lock node there, leave prod untouched.

    task-FFW-P0-C8R hard requirements: the production tree is never written
    (a sha256 before/after lock proves it), the copy carries ``packages/`` +
    ``tests/`` + ``pyproject.toml`` so pytest can run inside it, the anchor
    uniqueness check survives, the red criterion is ``returncode != 0`` plus
    the *expected failing node name* appearing in the output (no collection
    false-reds), and ``timeout=180`` is kept.
    """
    original = source.read_bytes()
    old_bytes, new_bytes = old.encode("utf-8"), new.encode("utf-8")
    assert old_bytes != new_bytes, "mutation must change the bytes"
    assert original.count(old_bytes) == 1, f"anchor not unique in {source.name}: {old!r}"

    prod_routing_sha = _sha(ROUTING_SOURCE)
    prod_contracts_sha = _sha(CONTRACTS_SOURCE)
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td)
        for sub in ("packages", "tests"):
            shutil.copytree(
                REPO_ROOT / sub,
                dst / sub,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        shutil.copy2(REPO_ROOT / "pyproject.toml", dst / "pyproject.toml")

        target = dst / source.relative_to(REPO_ROOT)
        target.write_bytes(original.replace(old_bytes, new_bytes, 1))

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                node_id,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=str(dst),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        output = proc.stdout + proc.stderr
        expected_node = node_id.split("::")[-1]
        assert proc.returncode != 0 and "failed" in output, (
            f"lock stayed green under mutation {old!r} -> {new!r}:\n{output[-2000:]}"
        )
        assert expected_node in output, (
            f"expected failing node {expected_node!r} missing from output "
            f"(collection error false-red?):\n{output[-2000:]}"
        )

    assert _sha(ROUTING_SOURCE) == prod_routing_sha, "production routing.py was written"
    assert _sha(CONTRACTS_SOURCE) == prod_contracts_sha, (
        "production contracts.py was written"
    )


def test_mutation_routing_drop_teasing_playful() -> None:
    """Mutation 1 (card 5.1): dropping a routing.py tag must turn 2.1 red."""
    _assert_mutation_turns_lock_red(ROUTING_SOURCE, *MUTATION_ROUTING_DROP, DRIFT_LOCK_NODE)


def test_mutation_contracts_drop_warm_open() -> None:
    """Mutation 2 (card 5.2): dropping the inline copy's tag must turn 2.2 red."""
    _assert_mutation_turns_lock_red(
        CONTRACTS_SOURCE, *MUTATION_CONTRACTS_DROP, DRIFT_LOCK_NODE
    )


def test_mutation_routing_add_extra_tag() -> None:
    """Mutation 3 (card 5.3): a one-sided addition must turn 2.2 red too."""
    _assert_mutation_turns_lock_red(ROUTING_SOURCE, *MUTATION_ROUTING_ADD, DRIFT_LOCK_NODE)


def test_mutation_default_tag_to_unknown() -> None:
    """Mutation 4 (card 5.4): changing the default must turn the reverse lock red."""
    _assert_mutation_turns_lock_red(
        ROUTING_SOURCE, *MUTATION_DEFAULT_UNKNOWN, DEFAULT_NEUTRAL_NODE
    )
