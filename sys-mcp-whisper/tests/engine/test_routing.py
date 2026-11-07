"""Track A / Track B routing locks (task-FFW-P0-C6, plan-p2-routing sections 1-6).

Two things are being pinned here and they are deliberately separated:

* ``RoutingResult`` structure -- field set, frozen/forbid, delta-key whitelist,
  value range and scenario vocabulary.  The delta value range lives inside a
  ``dict`` value annotation, so ``model_fields[...].metadata`` is empty and the
  GUARD reflection matrix cannot see it (same class of blind spot as
  ``PulseState.delayed_impact``).  It is therefore exercised behaviourally
  here, in both directions, or it is not exercised at all.
* Causally isolated routing -- a persona's apology must never register as the
  user being appeased.  Every isolation claim below is asserted in both
  directions: the persona text yields an empty delta *and* the identical text
  as a user message yields a non-empty one.  A one-directional assertion would
  stay green under an implementation that always returns ``{}``.
"""

# APOLOGY is quoted verbatim from plan-p2-routing section 2's own example, so it
# keeps its full-width punctuation; the repo norm for that is a file-level noqa
# (see tests/engine/test_assembler.py), not rewriting the contract text.
# ruff: noqa: RUF001

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.core.domain.contracts import RoutingResult
from packages.core.domain.state import S1Snapshot
from packages.core.engine import routing
from packages.core.engine.routing import (
    SCENARIO_TAGS,
    MessageEnvelope,
    Segment,
    parse_speaker,
    route_track_a,
    route_track_b,
)

ROUTING_SOURCE = Path(routing.__file__)
ENGINE_DIR = ROUTING_SOURCE.parent

# Apology text the persona says.  The user saying the same words is what must
# move the delta; the persona saying them must not.
APOLOGY = "对不起，是我不好"
APOLOGY_KEYWORD = "对不起"

# The six tags are pinned verbatim by the H ruling; order included, because the
# ruling writes them in this order and P4 appends rather than reorders.
EXPECTED_TAGS = (
    "neutral",
    "shy_hesitant",
    "defensive_cold",
    "warm_open",
    "teasing_playful",
    "distressed_fragile",
)


def make_snapshot(scope_id: str = "chat_8842") -> S1Snapshot:
    """A minimal valid snapshot; routing only reads ``scope_id``."""
    snapshot = S1Snapshot.model_validate(
        {
            "version": 1,
            "scope_id": scope_id,
            "timestamp": "2026-11-05T22:00:00Z",
            "last_decay_at": "2026-11-05T21:00:00Z",
            "is_conflict": False,
            "ipc": {
                "affiliation": 40.0,
                "dominance": 60.0,
                "relationship_stage": "friend",
                "relationship_ceiling": "lover",
            },
            "traits": {"tsundere": {"score": 72, "visibility": "unlocked"}},
            "emotions": {},
        }
    )
    return snapshot


# ---------------------------------------------------------------------------
# 1. RoutingResult structure (AC-R-2)
# ---------------------------------------------------------------------------


def test_routing_result_fields() -> None:
    """Exactly seven fields, in the order the plan table lists them."""
    assert list(RoutingResult.model_fields) == [
        "stimulus_delta",
        "risk_flag",
        "scenario_tag",
        "is_degraded",
        "source",
        "route_id",
        "matched_scope",
    ]
    assert set(RoutingResult.model_fields) == {
        "stimulus_delta",
        "risk_flag",
        "scenario_tag",
        "is_degraded",
        "source",
        "route_id",
        "matched_scope",
    }


def test_routing_result_is_frozen_and_forbids_extra() -> None:
    """A cross-layer DTO: frozen stops downstream mutation, forbid stops smuggling."""
    config = RoutingResult.model_config
    assert config.get("frozen") is True
    assert config.get("extra") == "forbid"

    result = RoutingResult(
        scenario_tag="neutral", source="track_a", route_id="route_1", matched_scope="private"
    )
    with pytest.raises(ValidationError):
        result.is_degraded = True  # type: ignore[misc]

    with pytest.raises(ValidationError) as excinfo:
        RoutingResult(
            scenario_tag="neutral",
            source="track_a",
            route_id="route_1",
            matched_scope="private",
            smuggled="x",
        )
    assert [error["type"] for error in excinfo.value.errors()] == ["extra_forbidden"]


def test_routing_result_lives_in_the_contract_layer() -> None:
    """G ruling: the cross-layer DTO belongs to contracts, not to engine."""
    assert RoutingResult.__module__ == "packages.core.domain.contracts"

    engine_source = ROUTING_SOURCE.read_text(encoding="utf-8")
    assert "class RoutingResult" not in engine_source


def test_routing_result_defaults() -> None:
    """Only the two optional-ish fields default; everything else is required."""
    result = RoutingResult(
        scenario_tag="neutral", source="track_a", route_id="route_1", matched_scope="private"
    )
    assert result.stimulus_delta == {}
    assert result.risk_flag == "none"
    assert result.is_degraded is False


def test_stimulus_delta_default_is_not_shared() -> None:
    """A mutable default must be copied per instance, not shared."""
    first = RoutingResult(
        scenario_tag="neutral", source="track_a", route_id="route_1", matched_scope="private"
    )
    second = RoutingResult(
        scenario_tag="neutral", source="track_a", route_id="route_2", matched_scope="private"
    )
    assert first.stimulus_delta == {}
    assert first.stimulus_delta is not second.stimulus_delta


# ---------------------------------------------------------------------------
# 2. delta key whitelist (AC-R-8)
# ---------------------------------------------------------------------------


def test_stimulus_delta_keys() -> None:
    """Keys come from DELTA_ELIGIBLE_PATHS; a typo is rejected, not silently zeroed.

    P2.8 (ADR-008 section 1.8) replaced the snapshot's structural path list
    with the five-path delta whitelist, so a legal key is one of
    pulse.dopamine / pulse.cortisol / pulse.oxytocin /
    ipc.affiliation / ipc.dominance.  traits used to be legal here
    because it was a snapshot field path; it is not a measurable quantity and
    is no longer accepted.
    """
    ok = RoutingResult(
        stimulus_delta={
            "pulse.dopamine": 1.0,
            "ipc.affiliation": -2.0,
            "ipc.dominance": 0.5,
        },
        scenario_tag="neutral",
        source="track_a",
        route_id="route_1",
        matched_scope="private",
    )
    assert set(ok.stimulus_delta) == {"pulse.dopamine", "ipc.affiliation", "ipc.dominance"}

    for bad_key in (
        "hormon.dopamine",
        "hormones.dopamin",
        "Hormones.dopamine",
        "traits",
        "",
    ):
        with pytest.raises(ValidationError) as excinfo:
            RoutingResult(
                stimulus_delta={bad_key: 1.0},
                scenario_tag="neutral",
                source="track_a",
                route_id="route_1",
                matched_scope="private",
            )
        messages = " ".join(error["msg"] for error in excinfo.value.errors())
        assert "unknown delta key" in messages, f"{bad_key!r} was not rejected by the key rule"


# ---------------------------------------------------------------------------
# 3. delta value range (invisible to the metadata reflection matrix)
# ---------------------------------------------------------------------------


def test_stimulus_delta_value_bounds() -> None:
    """ge=-100 / le=100 sit on dict values, so this is the only guard they get."""
    for accepted in (100.0, -100.0, 0.0):
        result = RoutingResult(
            stimulus_delta={"pulse.dopamine": accepted},
            scenario_tag="neutral",
            source="track_a",
            route_id="route_1",
            matched_scope="private",
        )
        assert result.stimulus_delta["pulse.dopamine"] == accepted

    for rejected in (101.0, -101.0):
        with pytest.raises(ValidationError):
            RoutingResult(
                stimulus_delta={"pulse.dopamine": rejected},
                scenario_tag="neutral",
                source="track_a",
                route_id="route_1",
                matched_scope="private",
            )


# ---------------------------------------------------------------------------
# 4. scenario vocabulary (AC-R-4) and the two-source drift lock
# ---------------------------------------------------------------------------


def test_scenario_tag_vocabulary() -> None:
    """Six tags plus the F-ruling literal ``degraded``; anything else is rejected."""
    for tag in SCENARIO_TAGS:
        result = RoutingResult(
            scenario_tag=tag, source="track_a", route_id="route_1", matched_scope="private"
        )
        assert result.scenario_tag == tag

    degraded = RoutingResult(
        scenario_tag="degraded", source="track_b", route_id="route_1", matched_scope="private"
    )
    assert degraded.scenario_tag == "degraded"

    for bad_tag in ("mating", "shy", "neutral ", "NEUTRAL", "degraded_x"):
        with pytest.raises(ValidationError):
            RoutingResult(
                scenario_tag=bad_tag, source="track_a", route_id="route_1", matched_scope="private"
            )


def test_scenario_tag_vocabulary_matches_the_engine_constant() -> None:
    """contracts.py keeps an inline copy; the two must not drift.

    The domain may not import engine (layering), so the copy is unavoidable --
    this test is what keeps it honest.  Order matters: P4 appends, never reorders.
    """
    contracts = importlib.import_module("packages.core.domain.contracts")
    inline = contracts._SCENARIO_TAGS

    assert tuple(inline) == tuple(SCENARIO_TAGS)
    assert tuple(SCENARIO_TAGS) == EXPECTED_TAGS
    assert len(SCENARIO_TAGS) == 6
    assert "degraded" not in SCENARIO_TAGS, "degraded is the F ruling literal, not a lexicon tag"


# ---------------------------------------------------------------------------
# 5. causal isolation (AC-R-3) -- both directions every time
# ---------------------------------------------------------------------------


def test_causal_isolation() -> None:
    """A persona apology must not count as the user being appeased."""
    state = make_snapshot()

    persona_only = route_track_a([Segment(role="persona", text=APOLOGY)], state)
    assert persona_only.stimulus_delta == {}

    user_same_text = route_track_a([Segment(role="user", text=APOLOGY)], state)
    assert user_same_text.stimulus_delta != {}

    mixed = route_track_a(
        [Segment(role="user", text=APOLOGY), Segment(role="persona", text=APOLOGY)], state
    )
    assert mixed.stimulus_delta == user_same_text.stimulus_delta


def test_unknown_role_is_neither_user_nor_persona() -> None:
    """Rule 3: an unlabelled message is dropped, and that must be visible."""
    state = make_snapshot()

    unknown = route_track_a([Segment(role="unknown", text=APOLOGY)], state)
    assert unknown.stimulus_delta == {}
    assert unknown.risk_flag == "none"
    assert unknown.scenario_tag == "neutral"


def test_parse_speaker_does_not_guess() -> None:
    """Rule 1: only the host label decides; an unknown role keeps no text."""
    segments = parse_speaker(
        [
            MessageEnvelope(role="user", text=APOLOGY),
            MessageEnvelope(role="persona", text="哼"),
            MessageEnvelope(role="system", text=APOLOGY),
        ]
    )

    assert [segment.role for segment in segments] == ["user", "persona", "unknown"]
    assert segments[0].text == APOLOGY
    assert segments[1].text == "哼"
    assert segments[2].text == "", "an unlabelled message must not smuggle its text through"
    assert segments[2].text != APOLOGY


def test_user_text_is_joined_before_a_single_lookup() -> None:
    """Rule 4: the same sentence split in two messages must not count twice."""
    state = make_snapshot()

    one_message = route_track_a([Segment(role="user", text=APOLOGY_KEYWORD)], state)
    two_messages = route_track_a(
        [
            Segment(role="user", text=APOLOGY_KEYWORD),
            Segment(role="user", text=APOLOGY_KEYWORD),
        ],
        state,
    )

    assert one_message.stimulus_delta != {}
    assert two_messages.stimulus_delta == one_message.stimulus_delta
    assert two_messages.scenario_tag == one_message.scenario_tag


# ---------------------------------------------------------------------------
# 6. Track A output shape (AC-R-2 / AC-R-4 wiring)
# ---------------------------------------------------------------------------


def test_route_track_a_shape() -> None:
    state = make_snapshot()
    result = route_track_a([Segment(role="user", text=APOLOGY)], state)

    assert result.source == "track_a"
    assert result.is_degraded is False
    assert result.matched_scope == "private"
    assert result.route_id
    assert result.scenario_tag in SCENARIO_TAGS
    for key in result.stimulus_delta:
        assert key in S1Snapshot.model_fields or key.count(".") == 1


def test_route_id_is_deterministic_and_idempotent() -> None:
    """The id is the idempotency key, so identical input must give identical ids."""
    state = make_snapshot()
    segments = [Segment(role="user", text=APOLOGY)]

    first = route_track_a(segments, state)
    second = route_track_a(segments, state)
    different = route_track_a([Segment(role="user", text="今天天气不错")], state)

    assert first.route_id == second.route_id
    assert first.route_id != different.route_id
    assert len(first.route_id) >= 1


def test_matched_scope_follows_the_scope_id_format() -> None:
    """Appendix H.5.3: group scopes carry ``group_id:user_id``, private ones do not."""
    private_result = route_track_a([Segment(role="user", text=APOLOGY)], make_snapshot("chat_8842"))
    group_result = route_track_a(
        [Segment(role="user", text=APOLOGY)], make_snapshot("group_7:user_9527")
    )

    assert private_result.matched_scope == "private"
    assert group_result.matched_scope == "group"


# ---------------------------------------------------------------------------
# 7. Track B Mock (AC-R-5)
# ---------------------------------------------------------------------------


def test_track_b_mock_shape() -> None:
    """Three signals must corroborate each other: degraded / True / track_b."""
    state = make_snapshot("group_7:user_9527")
    result = asyncio.run(route_track_b(MessageEnvelope(role="user", text=APOLOGY), state))

    assert result.stimulus_delta == {}
    assert result.risk_flag == "none"
    assert result.scenario_tag == "degraded"
    assert result.is_degraded is True
    assert result.source == "track_b"
    assert result.route_id
    assert result.matched_scope == "group"


def test_track_b_is_awaitable_under_wait_for() -> None:
    """P3 wraps this call in ``asyncio.wait_for(..., 0.8)``; a sync def cannot be."""
    assert inspect.iscoroutinefunction(route_track_b)

    async def run() -> RoutingResult:
        return await asyncio.wait_for(
            route_track_b(MessageEnvelope(role="user", text=APOLOGY), make_snapshot()),
            timeout=0.8,
        )

    result = asyncio.run(run())
    assert result.is_degraded is True


def test_track_b_does_not_decide_by_text() -> None:
    """The degraded path does no semantic work at all: text in, same shape out."""
    state = make_snapshot()
    first = asyncio.run(route_track_b(MessageEnvelope(role="user", text=APOLOGY), state))
    second = asyncio.run(route_track_b(MessageEnvelope(role="user", text="今天天气不错"), state))

    assert first.stimulus_delta == second.stimulus_delta == {}
    assert first.scenario_tag == second.scenario_tag == "degraded"
    assert first.risk_flag == second.risk_flag == "none"


# ---------------------------------------------------------------------------
# 8. Purity: the new engine file keeps the same import and clock rules
# ---------------------------------------------------------------------------


def _routing_imports() -> set[str]:
    tree = ast.parse(ROUTING_SOURCE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            roots.add((node.module or "").split(".")[0])
    return roots


def test_routing_import_whitelist() -> None:
    """Engine may only reach for its whitelisted stdlib and its own package."""
    gate = importlib.import_module("tests.engine.test_dynamics")
    allowed = gate.ALLOWED_TOP_LEVEL_MODULES

    offenders = sorted(_routing_imports() - set(allowed))
    assert offenders == [], f"routing.py imports outside the engine whitelist: {offenders}"


def test_routing_sources_do_not_read_the_clock() -> None:
    """Same AST rule as the rest of the engine: no now/utcnow/today."""
    banned = {"now", "utcnow", "today", "fromtimestamp"}
    tree = ast.parse(ROUTING_SOURCE.read_text(encoding="utf-8"))

    hits = [
        f"call:{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in banned
    ]
    assert hits == []


def test_scenario_tags_is_a_frozen_constant() -> None:
    """AC-R-4 keeps the vocabulary importable and comparable, not configurable."""
    assert isinstance(SCENARIO_TAGS, tuple)
    assert all(isinstance(tag, str) for tag in SCENARIO_TAGS)
    tree = ast.parse(ROUTING_SOURCE.read_text(encoding="utf-8"))
    assigned = [
        target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        for target in [node.target]
    ]
    assert "SCENARIO_TAGS" in assigned


def _all_sort_key(name: str) -> tuple[int, str]:
    """Ruff RUF022 orders ``__all__`` isort-style: constants, classes, functions.

    Plain ``sorted()`` puts ``ConflictVerdict`` before ``MOCK_LEXICON`` and would
    disagree with what ruff -- and therefore every other engine module -- actually
    enforces.  Spelling the rule out here keeps the assertion meaningful instead
    of tautological.
    """
    if name.isupper():
        return (0, name)
    if name[:1].isupper():
        return (1, name)
    return (2, name)


def test_engine_package_reexports_the_routing_symbols() -> None:
    """`engine/__init__.py` must expose the six new symbols with a sorted __all__."""
    engine_package = importlib.import_module("packages.core.engine")

    for name in (
        "SCENARIO_TAGS",
        "Segment",
        "MessageEnvelope",
        "parse_speaker",
        "route_track_a",
        "route_track_b",
    ):
        assert name in engine_package.__all__, f"{name} missing from engine.__all__"
        assert hasattr(engine_package, name), f"{name} not importable from packages.core.engine"

    assert list(engine_package.__all__) == sorted(
        engine_package.__all__, key=_all_sort_key
    ), "engine.__all__ must follow the repo's ruff RUF022 (isort-style) ordering"
