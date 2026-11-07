"""Lexicon arbitration (task-FFW-P25-T5).

:class:`DictionaryArbiter` -- a local keyword lexicon scores a message on
two fixed axes (``agreeableness`` / ``arousal``) in ``[-1.0, 1.0]``.  No
LLM, no network, no file IO, no environment: the table below is the whole
world it sees.  Matching is substring accumulation; the final value is
clamped with ``max(-1.0, min(1.0, total))``.

Confidence (task-FFW-P25-T5 card section 13): ``evaluate`` also returns a
third key ``confidence`` in ``[0.0, 1.0]``, and both axis scores are shrunk
towards 0 by it (``score * confidence``) before the clamp.  A lone short
keyword in a long message is weak evidence -- a single "太好了" can be
sarcasm -- so the raw sum is never emitted at full strength unless the
message itself corroborates the table.

(The cross-domain merge that shipped alongside this card lives in
``router.py`` as ``merge_cross_domain_state`` -- one definition, exported
through ``packages.core.engine``.)
"""

from __future__ import annotations

from typing import Final

#: (keyword, agreeableness delta, arousal delta).  Local, final, small:
#: positive and negative entries on both axes, no external source of any kind.
_LEXICON: Final[tuple[tuple[str, float, float], ...]] = (
    ("太好了", 0.8, 0.4),
    ("谢谢", 0.5, 0.0),
    ("喜欢你", 0.7, 0.3),
    ("开心", 0.6, 0.2),
    ("好的", 0.4, -0.2),
    ("麻烦你", 0.2, 0.3),
    ("讨厌", -0.7, 0.5),
    ("烦死了", -0.8, 0.6),
    ("滚", -0.9, 0.8),
    ("闭嘴", -0.8, 0.7),
    ("无所谓", -0.3, -0.4),
    ("随便", -0.2, -0.3),
    ("急死了", -0.1, 0.9),
    ("慢点", 0.3, -0.5),
)


def _clamp(value: float) -> float:
    """Bidirectional clamp onto ``[-1.0, 1.0]``."""
    return max(-1.0, min(1.0, value))


#: Keyword coverage (matched keyword chars / message length) at or above which
#: the lexicon table is considered to "explain" the message.
_CONFIDENCE_DENSITY_TARGET = 0.25
#: Number of independent hits at which the evidence is considered corroborated.
_CONFIDENCE_COUNT_TARGET = 2


def _confidence(hits: tuple[str, ...], stripped: str) -> float:
    """Explainable confidence in ``[0.0, 1.0]`` -- how the number is computed:

    two equally weighted factors, each in ``[0, 1]``:

    * ``count_factor = min(1, len(hits) / 2)`` -- two or more independent
      keyword hits corroborate each other; a single hit alone is exactly the
      sarcasm/noise case the human ruling calls out, so it caps at 0.5.
    * ``length_factor = min(1, density / 0.25)`` where ``density`` is matched
      keyword characters over the stripped message length -- when the matched
      text covers at least a quarter of the message the table actually
      explains what was said, instead of one word adrift in filler.

    ``confidence = 0.5 * count_factor + 0.5 * length_factor``; no hits at all
    means no evidence, so the confidence is ``0.0`` (the scores are 0 anyway).
    The shrinkage is linear (``score * confidence``) so a half-confident
    ``-0.9`` arrives at ``-0.45`` -- extreme PAD values are unreachable on
    weak evidence, which is the point of the patch.
    """
    if not hits:
        return 0.0
    count_factor = min(1.0, len(hits) / _CONFIDENCE_COUNT_TARGET)
    matched_chars = sum(len(keyword) for keyword in hits)
    density = matched_chars / max(1, len(stripped))
    length_factor = min(1.0, density / _CONFIDENCE_DENSITY_TARGET)
    return 0.5 * count_factor + 0.5 * length_factor


class DictionaryArbiter:
    """Score a message on two fixed axes with a purely local keyword table."""

    def evaluate(self, message: str) -> dict[str, float]:
        """Accumulate every keyword hit, then clamp both sums onto ``[-1, 1]``.

        Multiple hits accumulate (sum of the deltas); both sums are then
        shrunk towards 0 by ``confidence`` (see module docstring / ``_confidence``)
        and clamped.  An empty or miss message returns all-zero -- never
        raises, never returns ``None``.
        """
        agreeableness = 0.0
        arousal = 0.0
        for keyword, agree_delta, arousal_delta in _LEXICON:
            if keyword in message:
                agreeableness += agree_delta
                arousal += arousal_delta
        confidence = _confidence(self.matched_keywords(message), message.strip())
        return {
            "agreeableness": _clamp(agreeableness * confidence),
            "arousal": _clamp(arousal * confidence),
            "confidence": confidence,
        }

    def matched_keywords(self, message: str) -> tuple[str, ...]:
        """Return the keywords that hit, in lexicon order (for test evidence)."""
        return tuple(keyword for keyword, _, _ in _LEXICON if keyword in message)
