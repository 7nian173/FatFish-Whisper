"""Track A / Track B routing skeleton (plan-p2-routing sections 2-4, G9/G10).

Two tracks, one output type:

* Track A reads the raw message text through a Mock lexicon and proposes numeric
  deltas.  Its whole point is causal isolation: a persona's apology must never
  register as the user being appeased, so only ``role == "user"`` segments ever
  reach the lexicon.
* Track B is the degraded fallback.  It decides nothing about the text; it
  returns a fixed shape whose three signals (``scenario_tag="degraded"``,
  ``is_degraded=True``, ``source="track_b"``) corroborate each other.

Both are pure: no clock read, no IO, no mutation of the snapshot.  ``async def``
on Track B is structural, not cosmetic -- P3 wraps the call in
``asyncio.wait_for(..., 0.8)``, which accepts coroutines only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from ..domain.contracts import RoutingResult
from ..domain.state import S1Snapshot

#: Six posture tags, pinned verbatim by the H ruling.  Order is part of the
#: contract: P4 appends new tags, it never reorders them.  The same tuple exists
#: inline in ``domain/contracts.py`` (the domain may not import engine); the
#: drift between the two copies is locked by
#: ``tests/engine/test_routing.py::test_scenario_tag_vocabulary_matches_the_engine_constant``.
SCENARIO_TAGS: Final[tuple[str, ...]] = (
    "neutral",
    "shy_hesitant",
    "defensive_cold",
    "warm_open",
    "teasing_playful",
    "distressed_fragile",
)

#: Keyword -> (delta suggestion, scenario tag) for the P2 Mock lexicon.
#:
#: Deliberately NOT reused from ``assembler.py``'s ``MOCK_LEXICON``: that one is
#: keyed by *trait name* (``tsundere`` / ``shyness`` / ``pride``) and its values
#: are persona lines, not text-to-delta rules.  Reusing it would silently map
#: the wrong vocabulary onto the wrong axis.
#:
#: One lookup per entry, not one per occurrence: a sentence split across two
#: messages must not double-count (plan section 2, rule 4).
_MOCK_RULES: Final[tuple[tuple[str, dict[str, float], str], ...]] = (
    ("对不起", {"ipc.affiliation": 1.0}, "warm_open"),
    ("谢谢", {"ipc.affiliation": 0.8}, "warm_open"),
    ("滚", {"ipc.affiliation": -1.5, "pulse.cortisol": 2.0}, "defensive_cold"),
    ("随便", {"ipc.affiliation": -0.5}, "shy_hesitant"),
    ("哈哈", {"pulse.dopamine": 1.0}, "teasing_playful"),
    ("我难过", {"pulse.cortisol": 1.5}, "distressed_fragile"),
)


@dataclass(frozen=True)
class Segment:
    """One speaker-labelled slice of an incoming message batch."""

    role: Literal["user", "persona", "unknown"]
    text: str


@dataclass(frozen=True)
class MessageEnvelope:
    """Minimal stand-in for the host's message record.

    P3 replaces this with the real envelope when the host adapter lands; for P2
    it exists only so the Mock and its tests have something to pass in.  It
    deliberately does NOT live in the domain package: it is not a cross-layer
    contract, and putting it there would put a transport shape into the contract
    layer without a ruling.
    """

    role: str
    text: str


def _stable_digest(text: str) -> str:
    """FNV-1a over the code points, in pure Python.

    ``hashlib`` is not on the engine import whitelist and ``hash()`` is salted
    per process, so neither can produce a reproducible idempotency key.  Eight
    hex digits are enough to tell Mock inputs apart and stay stable across runs.
    """
    digest = 0x811C9DC5
    for character in text:
        digest ^= ord(character)
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return f"{digest:08x}"


def _resolve_scope(scope_id: str) -> Literal["private", "group"]:
    """Appendix H.5.3: group scopes are ``group_id:user_id``, private ones are not."""
    return "group" if ":" in scope_id else "private"


def parse_speaker(source_messages: list[MessageEnvelope]) -> list[Segment]:
    """Label each message by the host's own ``role`` field, never by guessing.

    Rules 1-3 of plan section 2: only the host label decides; an unrecognised
    role keeps no text at all, so it cannot leak into a lexicon lookup later.
    """
    segments: list[Segment] = []
    for message in source_messages:
        if message.role == "user":
            segments.append(Segment(role="user", text=message.text))
        elif message.role == "persona":
            segments.append(Segment(role="persona", text=message.text))
        else:
            segments.append(Segment(role="unknown", text=""))
    return segments


def _lookup(user_text: str) -> tuple[dict[str, float], str]:
    """One pass over the joined user text; first matching rule wins per entry."""
    delta: dict[str, float] = {}
    tags: list[str] = []
    for keyword, rule_delta, tag in _MOCK_RULES:
        if keyword in user_text:
            for key, value in rule_delta.items():
                delta[key] = delta.get(key, 0.0) + value
            tags.append(tag)
    if not tags:
        return {}, "neutral"
    return delta, tags[0]


def route_track_a(segments: list[Segment], state: S1Snapshot) -> RoutingResult:
    """Propose deltas from the user's own words only (plan section 2, rules 1-4).

    ``state`` is accepted because P3 will need it for stage-aware routing; the
    P2 Mock reads exactly one field, ``scope_id``, and takes no other decision
    from the snapshot.
    """
    user_text = " ".join(segment.text for segment in segments if segment.role == "user")
    delta, tag = _lookup(user_text)
    return RoutingResult(
        stimulus_delta=delta,
        risk_flag="none",
        scenario_tag=tag,
        is_degraded=False,
        source="track_a",
        route_id=f"route_a_{_stable_digest(user_text)}",
        matched_scope=_resolve_scope(state.scope_id),
    )


async def route_track_b(envelope: MessageEnvelope, state: S1Snapshot) -> RoutingResult:
    """Degraded fallback: a fixed shape, no semantic decision, no sleeping.

    The 800 ms budget belongs to P3's wiring (``asyncio.wait_for`` around a real
    LLM call), not to this Mock, so nothing here is time-dependent.
    """
    return RoutingResult(
        stimulus_delta={},
        risk_flag="none",
        scenario_tag="degraded",
        is_degraded=True,
        source="track_b",
        route_id=f"route_b_{_stable_digest(envelope.text)}",
        matched_scope=_resolve_scope(state.scope_id),
    )
