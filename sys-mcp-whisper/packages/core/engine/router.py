"""Heat computation and compute-tier evaluation (task-FFW-P25-T2).

Two pure functions from ``20_文档区/02_架构决策记录(ADR)/02-热度指标与转换矩阵.md``
sections 2-3.  They take no clock, do no IO and touch no environment: the
host supplies the metrics, this module only does arithmetic.

``calculate_heat`` runs entirely in integer arithmetic and converts to float
only at the return boundary.  ``Heat`` is an integer on ``[0, 100]``; the
``float`` return exists only to satisfy the signature and allow fractional
weights later.  If the internals multiplied the coefficients in floating
point and summed, ``0.1 + 0.2 != 0.3``-style error would push the 10.0 and
50.0 boundaries to the wrong side of ``evaluate_compute_tier`` -- computing
in integers first keeps both boundary values exactly representable.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Final

from ..domain.global_context import PulseState
from ..domain.state import S1Snapshot
from .dynamics import DEFAULT_RATES, DecayRates, apply_time_decay


def _norm_msg(count: int) -> int:
    """10 messages in 5 minutes = full score."""
    return min(count * 10, 100)


def _norm_mention(count: int) -> int:
    """4 mentions = full score."""
    return min(count * 25, 100)


def _norm_turn(count: int) -> int:
    """20 context turns = full score."""
    return min(count * 5, 100)


def calculate_heat(msg_freq: int, mention_count: int, context_turns: int) -> float:
    """Weighted heat score on ``[0.0, 100.0]``; integer math until the return.

    ``Heat = (4*norm_msg + 5*norm_mention + 1*norm_turn) // 10`` -- the
    weights 4:5:1 and the three saturation caps are normative, see the ADR.

    Input hygiene (P2.5 Bug 3, human ruling 2026-11-06):

    * Negative counts are impossible as inputs (you cannot have sent -3
      messages), so they are clamped to 0 rather than rejected -- a negative
      count most likely means an off-by-one upstream, and producing a negative
      heat would be worse than treating it as "nothing happened".
    * ``nan`` / ``inf`` are NOT clamped.  They mean the caller's arithmetic is
      broken, and ADR-006 is explicit that degraded heat must never be
      SILENTLY substituted ("禁止静默降级").  Returning 0.0 would make a busy
      group look idle; returning a huge number would make it look hot.  Both
      are lies, so this raises ``ValueError`` and lets the caller decide.

    Note ``nan`` would otherwise fall through every comparison in
    ``evaluate_compute_tier`` and come out as ``hot`` -- the worst direction.
    """
    for name, value in (
        ("msg_freq", msg_freq),
        ("mention_count", mention_count),
        ("context_turns", context_turns),
    ):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"heat input {name} must be finite, got {value!r}")
    msg_freq = max(0, msg_freq)
    mention_count = max(0, mention_count)
    context_turns = max(0, context_turns)
    heat = (
        4 * _norm_msg(msg_freq) + 5 * _norm_mention(mention_count) + 1 * _norm_turn(context_turns)
    ) // 10
    return float(heat)


def evaluate_compute_tier(heat: float) -> str:
    """Map a heat score to the compute tier literal ``cold`` / ``warm`` / ``hot``."""
    if heat < 10:
        return "cold"
    if heat < 50:
        return "warm"
    return "hot"


# ---------------------------------------------------------------------------
# Compute-tier gating (task-FFW-P25-T3, human ruling 2026-11-06)
# ---------------------------------------------------------------------------

#: Fixed cold-tier narration.  The copy is fixed by human ruling (2026-11-06);
#: any change to this text requires human confirmation.  No scene variants,
#: no affinity variants, no group/private branches.  This is the single source
#: of truth -- assembler.py imports it instead of redefining a copy.
COLD_NARRATION: Final[str] = "[系统提示：未唤醒] 角色当前处于未深度参与的旁观状态。"

#: The three compute-tier literals (ADR-006), in gate order.
VALID_TIERS: Final[tuple[str, ...]] = ("cold", "warm", "hot")


def should_call_track_b(tier: str) -> bool:
    """只有 hot 档允许调用 Track B (高成本路径)。

    返回 True 表示调用 Track B, False 表示跳过。

    Unknown tiers raise ``ValueError`` instead of silently returning False:
    a silent False would permanently skip Track B on a mistyped tier, which
    the ADR forbids as silent degradation.
    """
    if tier == "hot":
        return True
    if tier in ("cold", "warm"):
        return False
    raise ValueError(f"unknown compute tier: {tier!r}")


def sanitize_tier_state(snapshot: S1Snapshot) -> S1Snapshot:
    """Force ``is_conflict=False`` on the cold tier; other tiers pass through.

    assembler.py:88/:99 branch on ``snapshot.is_conflict``; on a cold tier the
    field would otherwise keep a stale ``True`` from the previous request and
    the assembler would emit a three-segment prompt the gate should never
    produce.  Non-cold snapshots are returned unchanged (same object).
    """
    if snapshot.compute_tier == "cold":
        # Round-tripped instead of model_copy(update=...): model_copy never runs
        # field validation, so a bad ``is_conflict`` value would be stamped onto
        # the snapshot with no complaint.  The constant False here is legal by
        # construction, but keeping ONE update idiom across this module means a
        # later edit cannot quietly reintroduce the unvalidated path.
        return S1Snapshot.model_validate(
            snapshot.model_dump() | {"is_conflict": False}
        )
    return snapshot


def settle_before_tier_upgrade(
    snapshot: S1Snapshot,
    global_pulse: PulseState,
    now: datetime,
    new_tier: str,
) -> tuple[S1Snapshot, PulseState]:
    """Settle pending decay before upgrading a cold snapshot's tier.

    Lazily evaluated settlement (P2.5-T6): a cold snapshot may sit idle for
    days; the pending decay is applied once, at the moment of the tier
    upgrade, not on every request.  Only an upgrade OUT of the cold tier
    (``new_tier`` in ``("warm", "hot")``) settles.  Same-tier and downgrade
    transitions, and snapshots that are not cold in the first place, are
    returned unchanged -- the very same objects (``is`` identity) for both the
    snapshot and the pulse, so callers can rely on reference equality to
    detect "nothing happened".

    Returns a ``(snapshot, pulse)`` pair because P2.8 moved the hormone lanes
    to ``GlobalContext.pulse`` (ADR-008 section 1.7): a settlement has to
    advance both carriers or the pulse would keep the pre-idle values.

    Order is a hard constraint: settle FIRST, write the tier SECOND.  The
    decayed snapshot is rebuilt by ``apply_time_decay``; writing the tier
    first would let the rebuild's tier win (historically the dropped-tier
    bug even erased the written tier entirely), so the upgrade is stamped on
    the already-decayed copy.

    The returned snapshot stamps ``last_decay_at = now``.  A pure decay
    function cannot know ``now`` and stamps ``snapshot.timestamp`` instead; if
    the settlement passed that through, the next upgrade would recompute the
    span just settled, over-counting by ``now - timestamp`` every round
    (P2.5 Bug 1, ruling 2026-11-06: the settlement owns the clock it was given).

    ``delta_t = now - snapshot.last_decay_at``; a negative delta raises
    ``ValueError`` (a clock running backwards is the caller's bug; silently
    treating it as zero would let it lurk).  An unknown ``new_tier`` raises
    ``ValueError`` (``VALID_TIERS`` is reused -- an unknown tier is never
    silently treated as a legal one).  Neither input is ever modified.
    """
    if new_tier not in VALID_TIERS:
        raise ValueError(f"unknown compute tier: {new_tier!r}")
    if snapshot.compute_tier == "cold" and new_tier in ("warm", "hot"):
        delta_t = now - snapshot.last_decay_at
        if delta_t < timedelta(0):
            raise ValueError(f"clock ran backwards: now < last_decay_at ({delta_t})")
        decayed, settled_pulse = apply_time_decay(snapshot, global_pulse, delta_t)
        # P2.5 Bug 1: apply_time_decay is a pure function with no clock, so it
        # stamps ``last_decay_at = snapshot.timestamp`` -- an instant that may be
        # arbitrarily old.  Leaving it there makes the NEXT settlement recompute
        # the already-settled span: the over-count is exactly ``now - timestamp``
        # per round.  The caller owns ``now``, so the caller stamps it.
        # model_validate, not model_copy(update=...): the latter skips field
        # validation entirely, so writing a tier outside VALID_TIERS or a
        # non-datetime ``now`` would produce an object the model would never
        # have accepted.  The round trip keeps S1Snapshot the single validator.
        settled = S1Snapshot.model_validate(
            decayed.model_dump() | {"compute_tier": new_tier, "last_decay_at": now}
        )
        return settled, settled_pulse
    return snapshot, global_pulse


def merge_cross_domain_state(
    cold_snap: S1Snapshot,
    cold_pulse: PulseState,
    hot_snap: S1Snapshot,
    hot_pulse: PulseState,
    weight: float = 0.3,
    *,
    now: datetime,
    rates: DecayRates = DEFAULT_RATES,
) -> tuple[S1Snapshot, PulseState]:
    """Blend the hot (local) domain into the cold (global) one; weight = hot side.

    Human ruling 2026-11-06 00:27: default ``weight=0.3`` means global 0.7 +
    local 0.3.  ``merged = cold * (1 - weight) + hot * weight`` now applies to
    exactly ONE quantity, ``ipc.affiliation``.  Every other field is taken from
    ``cold_snap`` verbatim, including ``version`` and ``scope_id``.

    merge was the hormone system's single inflow point until P2.8; ADR-008
    section 1.4 moved the hormone inflow to the stimulus delta, so merge now
    transfers IPC only.  Cross-domain hormone "residual warmth" is gone by
    design, not by omission: blending pulse lanes would double-count a stimulus
    that the delta path already applied.

    Because only the IPC half is blended, the pulse is NOT blended either --
    ``cold_pulse`` is returned as a deep copy (the hot side's pulse was decayed
    for the IPC blend and is then discarded; its fresh value still has to be
    settled by its own owner, which is the caller).

    The merge still owns the time decay of the hot side (ruling 2026-11-06
    01:21): ``hot_snap``/``hot_pulse`` are first decayed over
    ``now - hot_snap.last_decay_at`` and the DECAYED affiliation is what gets
    blended.  The order matters -- blending first and decaying afterwards would
    also erase part of the cold side, which has already been decayed by its own
    owner.  ``now`` is keyword-only and mandatory: the engine layer never reads
    a clock, the host hands the instant in.  A negative delta raises
    ``TimeDecayError`` (dynamics does not tolerate time running backwards).

    ``compute_tier``: the tier travels with ``apply_time_decay`` verbatim
    since P2.5-T6 fixed the rebuild's dropped-tier bug, so no manual
    carry-back is needed here anymore; the merged result keeps the cold
    domain's tier regardless.

    ``weight`` outside ``[0, 1]`` raises ``ValueError`` -- a configuration
    error is never silently clamped.  The result's ``last_decay_at`` is
    ``now`` (the cold side has just absorbed a fully decayed hot side).
    All four inputs stay unmodified; fresh objects are returned.
    """
    if not (0.0 <= weight <= 1.0):
        raise ValueError(f"weight out of range [0, 1]: {weight!r}")

    delta_t = now - hot_snap.last_decay_at
    hot_decayed, hot_decayed_pulse = apply_time_decay(hot_snap, hot_pulse, delta_t, rates)
    # hot_decayed_pulse is deliberately unused below: ADR-008 section 1.6 moved
    # the hormone carry-over out of merge.  It is still computed because the
    # decay call settles the hot side's clock for the affiliation blend.
    del hot_decayed_pulse

    blended_affiliation = (
        cold_snap.ipc.affiliation * (1 - weight)
        + hot_decayed.ipc.affiliation * weight
    )

    # model_validate, not model_copy(update=..., deep=True): the round trip
    # re-runs IPCState's own bounds (-100..100) on the blended affiliation, so an
    # out-of-range input cannot slip a 114.0 through.  The ipc payload is given
    # as a plain dict on purpose: pydantic's default ``revalidate_instances`` is
    # "never", so handing it an already-built IPCState instance would skip the
    # very validation this round trip exists for.
    merged = S1Snapshot.model_validate(
        cold_snap.model_dump()
        | {
            "ipc": cold_snap.ipc.model_dump() | {"affiliation": blended_affiliation},
            "compute_tier": cold_snap.compute_tier,
            "last_decay_at": now,
        }
    )
    return merged, cold_pulse.model_copy(deep=True)
