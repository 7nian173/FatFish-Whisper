"""Phenomenological prompt assembly (contract 4.3, plan-assembler.md).

``assemble_prompt`` is a pure function: it reads the snapshot, the lexicon and
the config, and returns a string - no mutation, no IO, no clock.  Every
template value (segment names, seal text, fallback sentence) comes from
``config.assembly``; nothing is hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..domain.config import DEFAULT_ENGINE_CONFIG, AssemblyConfig, EngineConfig
from ..domain.global_context import PulseState
from ..domain.state import S1Snapshot
from .router import COLD_NARRATION

#: Trait lexicon: trait name -> {"high": sentence, "low": sentence}.
Lexicon = dict[str, dict[str, str]]


@dataclass(frozen=True)
class PhenomenonEntry:
    """One phenomenological entry: three segment slots x long/short variants.

    ``conditions`` is a small constraint dict, e.g.
    ``{"cortisol": ">75", "dopamine": "<40", "trait_cold_anger": "unlocked"}``.
    Hormone keys compare the caller-provided ``pulse`` strictly (``>`` ``<``
    ``>=`` ``<=``, no ``==``); ``trait_<name>`` keys compare
    ``snapshot.traits[<name>].visibility`` exactly.
    """

    conditions: dict[str, str]
    long_somatic: str
    long_drive: str
    long_block: str
    short_somatic: str
    short_drive: str
    short_block: str


#: phenomenological map: entry name -> entry (task-FFW-P29-SIM).
PhenomenologicalMap = dict[str, PhenomenonEntry]

#: Mock lexicon for the sandbox pilot (plan-assembler section 3).
MOCK_LEXICON: Lexicon = {
    "tsundere": {
        "high": "被叫笨蛋时，你感到一阵烦躁，指尖发烫。",
        "low": "被叫笨蛋时，你只是轻轻哼了一声。",
    },
    "shyness": {
        "high": "视线相撞的瞬间，你的耳根先热了起来。",
        "low": "视线相撞时，你略微停顿了半秒。",
    },
    "pride": {
        "high": "你明明很骄傲，此刻却生不起气来。",
        "low": "你耸了耸肩，没有把这件事放在心上。",
    },
}

#: Fixed copy for segments without any lexicon support (contract 4.3).
_TENSION_FALLBACK = "两种念头同时拽着你，谁也没赢。"
_BYSTANDER_TEXT = "空气安静了两秒。"
_LABEL_LINE = "【潜意识感知】："
_SEAL_LABEL = "【输出格式封印】："

#: Cap for the injection body when it is handed to the host (``pure=False``).
#: Counted in code points, marker lines excluded; a truncated body keeps at
#: most ``MAX_INJECTION_BODY_CHARS - 1`` sentence characters plus one ellipsis.
MAX_INJECTION_BODY_CHARS: Final[int] = 50


def _variant(trait_name: str, score: int, lexicon: Lexicon) -> str | None:
    """Return the lexicon sentence for ``trait_name`` (high if score>=50) or None."""
    entry = lexicon.get(trait_name)
    if not entry:
        return None
    if score >= 50:
        return entry.get("high") or entry.get("low")
    return entry.get("low") or entry.get("high")


def _body_length(body: list[str]) -> int:
    """Body length in code points; the newline between lines is not counted."""
    return sum(len(line) for line in body)


def _truncate_body(body: list[str], limit: int) -> list[str]:
    """Cap the injection body at ``limit`` code points, dropping whole lines.

    Lines are dropped from the tail so the priority order stays main >
    tension > bystander.  A segment line is never left as a bare label: if a
    single line still exceeds the cap, its sentence is trimmed and terminated
    with ``\u2026`` (one code point) so the label keeps a non-empty body.
    """
    if _body_length(body) <= limit:
        return body

    kept = list(body)
    while len(kept) > 1 and _body_length(kept) > limit:
        kept.pop()

    if _body_length(kept) > limit:
        label, _, sentence = kept[0].partition("：")
        room = limit - len(label) - 1 - 1
        kept[0] = f"{label}：{sentence[:room]}…"
    return kept


#: Comparison operators accepted in ``conditions`` hormone entries.  Strict
#: semantics only -- no ``==``: float equality on a 0-100 pulse lane is a
#: measure-zero event that would make a condition either dead or fragile.
_HORMONE_OPS: Final[tuple[str, ...]] = (">=", "<=", ">", "<")

#: Hormone condition keys a ``conditions`` dict may use (the pulse lanes).
_HORMONE_KEYS: Final[frozenset[str]] = frozenset({"cortisol", "dopamine", "oxytocin"})


def _hormone_condition_holds(value: str, hormone: float) -> bool:
    """Evaluate one ``"<op><threshold>"`` condition against a pulse value."""
    op = value[:2] if value[:2] in (">=", "<=") else value[:1]
    if op not in _HORMONE_OPS:
        raise ValueError(f"unsupported hormone operator in condition: {value!r}")
    threshold = float(value[len(op) :])
    if op == ">":
        return hormone > threshold
    if op == "<":
        return hormone < threshold
    if op == ">=":
        return hormone >= threshold
    return hormone <= threshold


def _entry_matches(
    entry: PhenomenonEntry, snapshot: S1Snapshot, pulse: PulseState | None
) -> bool:
    """Return True when every condition of ``entry`` holds.

    A hormone condition with ``pulse is None`` makes the entry unmatched (no
    exception, no guessed default) -- the caller simply did not bring a pulse,
    so nothing can be said about hormones.  An unknown trait key is likewise
    unmatched.  A condition key that is neither a hormone nor a ``trait_`` key
    is a dictionary typo, so it raises instead of silently matching nothing.
    """
    for key, value in entry.conditions.items():
        if key in _HORMONE_KEYS:
            if pulse is None:
                return False
            if not _hormone_condition_holds(value, getattr(pulse, key)):
                return False
        elif key.startswith("trait_"):
            trait = snapshot.traits.get(key[len("trait_") :])
            if trait is None or trait.visibility != value:
                return False
        else:
            raise ValueError(f"unknown condition key: {key!r}")
    return True


def _match_phenomenology(
    phenomenology: PhenomenologicalMap,
    snapshot: S1Snapshot,
    pulse: PulseState | None,
) -> PhenomenonEntry | None:
    """First entry (dict insertion order) whose conditions all hold, or None."""
    for entry in phenomenology.values():
        if _entry_matches(entry, snapshot, pulse):
            return entry
    return None


def _entry_body(
    entry: PhenomenonEntry,
    variant: str,
    names: dict[str, str],
    is_conflict: bool,
) -> list[str]:
    """Build the segment body from one entry's long or short slots.

    Mapping (human ruling 3, one-to-one): somatic -> ``[主场]``,
    drive -> ``[拉扯]`` (only when ``is_conflict``), block -> ``[旁边杵着]``.
    An empty slot falls back to the legacy fixed copy for that segment; an
    entry-sourced body therefore always emits all three segment sentences.
    """
    somatic = getattr(entry, f"{variant}_somatic") or _BYSTANDER_TEXT
    drive = getattr(entry, f"{variant}_drive") or _TENSION_FALLBACK
    block = getattr(entry, f"{variant}_block") or _BYSTANDER_TEXT
    body = [f"{names['main']}：{somatic}"]
    if is_conflict:
        body.append(f"{names['tension']}：{drive}")
    body.append(f"{names['bystander']}：{block}")
    return body


def _degrade_then_truncate(
    long_body: list[str], short_body: list[str], limit: int
) -> list[str]:
    """Five-step degrade (task-FFW-P29-SIM ruling 1); the order is pinned.

    1. long body already at or under ``limit`` -> return it untouched;
    2. otherwise swap every entry-sourced segment to its short variant and
       retry;
    3. still over -> drop ``[旁边杵着]`` (the bystander) first;
    4. still over -> drop ``[拉扯]`` (the tension) next;
    5. only when nothing but ``[主场]`` remains, hand the single line to
       ``_truncate_body`` for the character-level trim (the existing branch is
       reused verbatim -- trimming logic must keep exactly one physical home).
    """
    if _body_length(long_body) <= limit:
        return long_body
    if _body_length(short_body) <= limit:
        return short_body

    # Both bodies carry the same line order: main, tension (if present),
    # bystander.  Dropping from the tail therefore removes bystander first,
    # then tension -- the pinned order, made explicit one step at a time.
    kept = list(short_body)
    # Step 3: drop the bystander line when a tail line exists to drop.
    if len(kept) > 1:
        kept.pop()
    if _body_length(kept) <= limit:
        return kept
    # Step 4: drop the tension line when one remains behind the main line.
    if len(kept) > 1:
        kept.pop()
    if _body_length(kept) <= limit:
        return kept
    # Step 5: only the main line is left -- character trim (existing logic).
    return _truncate_body(kept, limit)


def _build_body(
    snapshot: S1Snapshot,
    assembly: AssemblyConfig,
    lexicon: Lexicon,
    pure: bool,
    phenomenology: PhenomenologicalMap | None,
    pulse: PulseState | None,
) -> list[str]:
    """Build the three-segment body lines shared by both assembly entries.

    Single source of the body construction (task-FFW-P3-F): trait traversal,
    phenomenology matching, the five-step degrade chain and the character
    budget all live here and nowhere else.  Returns the body lines WITHOUT
    the label line and WITHOUT the seal line.
    """
    names = assembly.segment_names
    fallback = assembly.lexicon_fallback

    # Phenomenology first: one matched entry owns all three segments.
    entry = (
        _match_phenomenology(phenomenology, snapshot, pulse)
        if phenomenology is not None
        else None
    )

    if entry is not None:
        long_body = _entry_body(entry, "long", names, snapshot.is_conflict)
        short_body = _entry_body(entry, "short", names, snapshot.is_conflict)
        body = long_body
        if not pure:
            body = _degrade_then_truncate(long_body, short_body, MAX_INJECTION_BODY_CHARS)
    else:
        # Main segment: first unlocked trait (dict order) with a lexicon entry.
        main_sentence: str | None = None
        main_key: str | None = None
        for key, trait in snapshot.traits.items():
            if trait.visibility != "unlocked":
                continue
            sentence = _variant(key, trait.score, lexicon)
            if sentence is not None:
                main_sentence = sentence
                main_key = key
                break

        # Tension segment: the next trait in dict order with a lexicon entry
        # (after the main one); dormant traits qualify - they are the
        # suppressed voice that the conflict surfaces.
        tension_sentence: str | None = None
        if snapshot.is_conflict:
            for key, trait in snapshot.traits.items():
                if main_key is not None and key == main_key:
                    continue
                sentence = _variant(key, trait.score, lexicon)
                if sentence is not None:
                    tension_sentence = sentence
                    break

        body = [
            f"{names['main']}：{main_sentence if main_sentence is not None else fallback}"
        ]
        if snapshot.is_conflict:
            tension = (
                tension_sentence if tension_sentence is not None else _TENSION_FALLBACK
            )
            body.append(f"{names['tension']}：{tension}")
        body.append(f"{names['bystander']}：{_BYSTANDER_TEXT}")

        # Host-bound renders are capped; the pure render deliberately keeps
        # the full three-segment body so the downstream LLM still sees the
        # tension.
        if not pure:
            body = _truncate_body(body, MAX_INJECTION_BODY_CHARS)

    return body


def assemble_prompt(
    snapshot: S1Snapshot,
    config: EngineConfig = DEFAULT_ENGINE_CONFIG,
    lexicon: Lexicon = MOCK_LEXICON,
    *,
    pure: bool = True,
    phenomenology: PhenomenologicalMap | None = None,
    pulse: PulseState | None = None,
) -> str:
    """Assemble the 【潜意识感知】 prompt from the snapshot (contract 4.3).

    Structure (RC-A4 ruling, contract 4.3 governs): the label line, then
    [主场], then [拉扯] only when ``snapshot.is_conflict``, then [旁边杵着].
    ``pure=False`` drops the output seal; ``pure=True`` (default) appends the
    seal line exactly once at the end.  Missing lexicon entries fall back to
    ``config.assembly.lexicon_fallback`` so every segment always emits a
    sentence.

    Phenomenology path (task-FFW-P29-SIM): when ``phenomenology`` is given
    and one entry's ``conditions`` all hold (dict insertion order decides),
    the three segment sentences come entirely from that entry --
    ``long_somatic``/``short_somatic`` -> ``[主场]``,
    ``long_drive``/``short_drive`` -> ``[拉扯]``,
    ``long_block``/``short_block`` -> ``[旁边杵着]``.  The trait lexicon is
    not consulted in that case (traits only feed ``trait_*`` conditions);
    ``_BYSTANDER_TEXT`` is demoted to a fallback used only when no entry
    matched or an entry's slot is an empty string.  ``pulse`` is the global
    hormone carrier the hormone conditions read; ``pulse=None`` plus a
    hormone condition means the entry does not match.  The five-step
    long->short degrade runs only when ``pure=False``; a ``pure=True`` render
    is never degraded or truncated.

    Legacy path (no phenomenology or no entry matched) is byte-for-byte the
    pre-P2.9 behaviour: trait traversal, ``_TENSION_FALLBACK`` and the fixed
    bystander line.

    Cold-tier exception (task-FFW-P25-T3): when ``snapshot.compute_tier ==
    "cold"`` the function returns the fixed ``COLD_NARRATION`` constant as its
    very first statement -- before any trait traversal or lexicon lookup --
    regardless of ``pure`` and with no seal line or segment labels appended.
    """
    if snapshot.compute_tier == "cold":
        return COLD_NARRATION

    body = _build_body(
        snapshot, config.assembly, lexicon, pure, phenomenology, pulse
    )

    lines: list[str] = [_LABEL_LINE, *body]

    if pure:
        lines.append(f"{_SEAL_LABEL}{config.assembly.seal_text}")

    return "\n".join(lines)


def assemble_messages(
    snapshot: S1Snapshot,
    config: EngineConfig = DEFAULT_ENGINE_CONFIG,
    lexicon: Lexicon = MOCK_LEXICON,
    *,
    pure: bool = True,
    phenomenology: PhenomenologicalMap | None = None,
    pulse: PulseState | None = None,
    persona_card: str | None = None,
) -> tuple[str, str]:
    """Two-field assembly for the P3 chat pipeline (plan-p3 §4 P3-F).

    Returns ``(system_head, tail_message)``.  The head is byte-stable given
    the same ``persona_card`` and seal text and is fully decoupled from
    ``pure`` (human P3-5 ruling).  The label line belongs to the tail, never
    to the head (human P3-4 ruling).  The tail reuses ``_build_body`` -- the
    exact same body construction as ``assemble_prompt``, no second copy --
    and appends a second seal line at the end only when ``pure=False``.
    """
    seal_line = f"{_SEAL_LABEL}{config.assembly.seal_text}"

    if persona_card is None or persona_card == "":
        system_head = seal_line
    else:
        system_head = f"{persona_card}\n{seal_line}"

    if snapshot.compute_tier == "cold":
        tail_message = f"{_LABEL_LINE}{COLD_NARRATION}"
        if not pure:
            tail_message = f"{tail_message}\n{seal_line}"
    else:
        body = _build_body(
            snapshot, config.assembly, lexicon, pure, phenomenology, pulse
        )
        tail_lines: list[str] = [_LABEL_LINE, *body]
        if not pure:
            tail_lines.append(seal_line)
        tail_message = "\n".join(tail_lines)

    return system_head, tail_message


# Lexicon sentences, segment labels and the seal/fallback copy are copied
# verbatim from contract 4.3 / 3.3, so they keep the original full-width CJK
# punctuation that RUF001 flags as ambiguous.
# ruff: noqa: RUF001
