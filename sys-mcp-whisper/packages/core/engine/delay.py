"""Human-like typing delay for a reply (task-FFW-P25-T7).

The delay is a pure function of the three hormone lanes, the conflict flag and
the length of the message the engine is about to send.  It reads no clock, no
IO and no snapshot: the caller owns both the clock and the snapshot, which also
keeps this module inside the engine import whitelist.

Ordering contract (card section 9)
----------------------------------
``user_msg_len`` 必须是 ``assemble_prompt(..., pure=False)`` 截断**之后**的最终
正文字数, 不能是截断前的长度. 用截断前长度会让延迟偏大: 冲突态截断前 59,
截断后 ≤50, ``base`` 相差 ``20 * (59 - 50) = 180ms``.This module cannot
enforce the ordering because it never sees the snapshot, so
``tests/engine/test_delay.py`` pins it down with an AST check, a docstring check
and an end-to-end test that the two lengths really produce different delays.

Thresholds (card section 5.3)
-----------------------------
``dopamine < 40 and cortisol > 80`` -> 0.2 (furious, answer immediately);
``oxytocin > 70 and 45 <= cortisol <= 70`` -> 2.0 (shy / hesitant, stall longer);
otherwise 1.0.  Conflict multiplies on top of whichever branch fired.

Threshold provenance: these are the P2.5 human card numbers, **not tuned
values**.  They are recorded here verbatim so a later change is visible in the
diff of this module rather than buried in a config default.
"""

from __future__ import annotations

from typing import Final

from ..domain.global_context import PulseState

BASE_DELAY_MS: Final[int] = 300
PER_CHAR_DELAY_MS: Final[int] = 20
MESSAGE_LENGTH_CAP: Final[int] = 100
MIN_DELAY_MS: Final[int] = 500
MAX_DELAY_MS: Final[int] = 5000

FURIOUS_DOPAMINE_MAX: Final[float] = 40.0
FURIOUS_CORTISOL_MIN: Final[float] = 80.0
FURIOUS_FACTOR: Final[float] = 0.2

SHY_OXYTOCIN_MIN: Final[float] = 70.0
SHY_CORTISOL_MIN: Final[float] = 45.0
SHY_CORTISOL_MAX: Final[float] = 70.0
SHY_FACTOR: Final[float] = 2.0

CONFLICT_FACTOR: Final[float] = 1.3


def calculate_typing_delay(
    hormones: PulseState,
    is_conflict: bool,
    user_msg_len: int,
) -> int:
    """Return the typing delay in milliseconds for the message being sent.

    ``user_msg_len`` must be the body length of ``assemble_prompt(...,
    pure=False)`` **after** truncation.  Feeding the pre-truncation length makes
    the delay too large: in a conflict state the body is 59 code points before
    truncation and at most 50 after it, so ``base`` differs by
    ``20 * (59 - 50) = 180ms``.

    ``user_msg_len`` is a required explicit parameter on purpose: this function
    must not read the snapshot, and must not call ``assemble_prompt`` itself, so
    that the ordering stays visible at the call site instead of hiding here.

    The hormone figures come from ``GlobalContext.pulse`` (ADR-008 section 1.7
    moved them off the snapshot); this module only reads the three lanes, so
    the move required no change to the arithmetic below.
    """
    if user_msg_len < 0:
        raise ValueError(f"user_msg_len must be >= 0: {user_msg_len}")

    if (
        hormones.dopamine < FURIOUS_DOPAMINE_MAX
        and hormones.cortisol > FURIOUS_CORTISOL_MIN
    ):
        factor = FURIOUS_FACTOR
    elif (
        hormones.oxytocin > SHY_OXYTOCIN_MIN
        and SHY_CORTISOL_MIN <= hormones.cortisol <= SHY_CORTISOL_MAX
    ):
        factor = SHY_FACTOR
    else:
        factor = 1.0

    if is_conflict:
        factor *= CONFLICT_FACTOR

    base = BASE_DELAY_MS + PER_CHAR_DELAY_MS * min(user_msg_len, MESSAGE_LENGTH_CAP)
    return max(MIN_DELAY_MS, min(MAX_DELAY_MS, int(base * factor)))
