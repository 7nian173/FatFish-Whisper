"""Pure time decay and physical clamp helpers for contract 4.1.

Both functions are pure: they never read the system clock, never touch IO and
never mutate their arguments.  The clock value (``delta_t``), the stimulus
history and the daily accumulator are all supplied by the caller.

Contract 4.1 lists three mechanisms: time decay, physical clamp and emotion TTL.
Only the first two live here; emotion TTL has no machine-checkable spec yet
(plan-dynamics-engine.md section 1, R2), so no TTL symbol is defined in this
module - a TTL comment is fine, a TTL function or class is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..domain.config import DEFAULT_ENGINE_CONFIG, DynamicsConfig
from ..domain.exceptions import TimeDecayError
from ..domain.global_context import PulseState
from ..domain.state import S1Snapshot
from .judge import HormoneDeltas

HORMONE_BASELINE = 50.0
HORMONE_KEYS = ("dopamine", "cortisol", "oxytocin")
AFFILIATION_BASELINE = 0.0

SINGLE_STIMULUS_CAP = 1.0
TWO_HOUR_WINDOW = timedelta(hours=2)
WINDOW_CAP = 3.0
DAILY_CAP = 4.0

SECONDS_PER_HOUR = 3600.0


def _default_hormone_rates() -> dict[str, float]:
    """Placeholder rate of 1.0 unit/hour per hormone.

    The concrete values are decided by ``DEFAULT_ENGINE_CONFIG.dynamics`` in
    ``packages/core/domain/config.py`` (plan-config section 2.4); this helper
    only mirrors them for the ``DecayRates`` field default.
    """
    return dict.fromkeys(HORMONE_KEYS, 1.0)


@dataclass(frozen=True)
class DecayRates:
    """Decay speeds per hour, injected by the caller.

    Use :meth:`from_config` to derive an instance from a ``DynamicsConfig``
    (the single source of truth in ``packages/core/domain/config.py``); the
    field defaults below are kept for backwards compatibility with existing
    call sites and tests.
    """

    hormone_per_hour: dict[str, float] = field(default_factory=_default_hormone_rates)
    affiliation_per_hour: float = 0.5

    @classmethod
    def from_config(cls, cfg: DynamicsConfig) -> DecayRates:
        """Derive decay speeds from a :class:`DynamicsConfig` (plan-judge section 4)."""
        return cls(
            hormone_per_hour=dict(cfg.hormone_per_hour),
            affiliation_per_hour=cfg.affiliation_per_hour,
        )


@dataclass(frozen=True)
class StimulusRecord:
    """One already-applied stimulus delta, used by the two-hour window."""

    at: datetime
    delta: float


@dataclass(frozen=True)
class DayAccumulator:
    """Today's applied totals; ``neg`` is negative (``neg <= 0``) by convention."""

    pos: float
    neg: float


#: Single default instance derived from the engine config (the single source
#: of truth), so the signature default stays a ``DecayRates`` object without
#: a function call in the argument list (ruff B008).
DEFAULT_RATES = DecayRates.from_config(DEFAULT_ENGINE_CONFIG.dynamics)


def _converge(value: float, target: float, step: float) -> float:
    """Move ``value`` towards ``target`` by ``step``, never past it.

    The sign of ``target - value`` picks the direction, so a value below the
    target moves up and a value above it moves down: converging from both sides,
    never away from the target.
    """
    distance = target - value
    if distance > 0.0:
        return value + min(step, distance)
    if distance < 0.0:
        return value - min(step, -distance)
    return value


def apply_time_decay(
    local_snapshot: S1Snapshot,
    global_pulse: PulseState,
    delta_t: timedelta,
    rates: DecayRates = DEFAULT_RATES,
) -> tuple[S1Snapshot, PulseState]:
    """Return fresh copies of the snapshot and the pulse, both decayed.

    Two return values because the state it settles lives in two carriers: the
    three hormone lanes moved to ``GlobalContext.pulse`` in P2.8 (ADR-008
    section 1.7), while ``ipc.affiliation`` is still per-scope and stays on
    ``S1Snapshot``.  One call therefore has to advance both clocks at once --
    splitting it into two functions would let a caller settle one carrier and
    forget the other, which is the drift this signature exists to prevent.

    ``delta_t < 0`` raises :class:`TimeDecayError`; ``delta_t == 0`` returns
    deep copies of both inputs with every field unchanged; ``delta_t > 0``
    decays the hormones towards 50 and affiliation towards 0.

    Only the three hormone lanes and ``ipc.affiliation`` are decayed.
    ``delayed_impact`` belongs to the HPA deferral mechanism (it is transported
    verbatim, never decayed -- and never dropped: the pulse is rebuilt field by
    field below); ``emotions`` belongs to the TTL mechanism and ``dominance``
    is not in the contract 4.1 decay list, so both are passed through.
    ``compute_tier`` takes no part in the decay either, but it must be carried
    through verbatim: the return value is rebuilt field by field, and a field
    left out of that list silently falls back to its Pydantic default (the
    dropped-tier bug fixed in P2.5-T6).
    """
    if delta_t < timedelta(0):
        raise TimeDecayError(f"delta_t < 0: {delta_t}")
    if delta_t == timedelta(0):
        return local_snapshot.model_copy(deep=True), global_pulse.model_copy(deep=True)

    hours = delta_t.total_seconds() / SECONDS_PER_HOUR

    hormone_rates = rates.hormone_per_hour
    pulse = PulseState(
        dopamine=_converge(
            global_pulse.dopamine, HORMONE_BASELINE, hormone_rates["dopamine"] * hours
        ),
        cortisol=_converge(
            global_pulse.cortisol, HORMONE_BASELINE, hormone_rates["cortisol"] * hours
        ),
        oxytocin=_converge(
            global_pulse.oxytocin, HORMONE_BASELINE, hormone_rates["oxytocin"] * hours
        ),
        # delayed_impact never decays; copy it so the caller's pulse is not
        # aliased (a later in-place edit of one would otherwise move the other).
        delayed_impact=dict(global_pulse.delayed_impact),
    )

    ipc = local_snapshot.ipc.model_copy(deep=True)
    ipc.affiliation = _converge(
        ipc.affiliation, AFFILIATION_BASELINE, rates.affiliation_per_hour * hours
    )

    # Decay never touches version / scope_id / timestamp / is_conflict /
    # compute_tier / traits / emotions / dominance. "Decay before evaluation"
    # is a caller-side ordering guarantee: this function only ever decays, it
    # never evaluates.
    snapshot = S1Snapshot(
        version=local_snapshot.version,
        scope_id=local_snapshot.scope_id,
        timestamp=local_snapshot.timestamp,
        # Carried through, never invented here: a field omitted from this list
        # silently falls back to its Pydantic default (``None`` for this one),
        # which is the same class of bug as the P2.5 dropped tier.  Nothing
        # writes ``last_active_at`` yet (reserved by C3), so dropping it on the
        # floor is invisible today and wrong the day somebody starts reading it.
        last_active_at=local_snapshot.last_active_at,
        last_decay_at=local_snapshot.timestamp,
        is_conflict=local_snapshot.is_conflict,
        compute_tier=local_snapshot.compute_tier,
        ipc=ipc,
        traits=local_snapshot.traits,
        emotions=local_snapshot.emotions,
    )
    return snapshot, pulse


def apply_limit_cap(
    new_delta: float,
    history: list[StimulusRecord],
    now: datetime,
    today_acc: DayAccumulator,
) -> float:
    """Clamp one stimulus delta through the three layers of contract 4.1.

    Layers run from strictest to widest: single stimulus +/-1, sliding two-hour
    window +/-3 (``now - 2h < at <= now``) and natural-day total +/-4. ``history``
    and ``today_acc`` are read-only; the caller records the returned value.
    """
    x = max(-SINGLE_STIMULUS_CAP, min(SINGLE_STIMULUS_CAP, new_delta))

    window_start = now - TWO_HOUR_WINDOW
    window_pos = 0.0
    window_neg = 0.0
    for record in history:
        if window_start < record.at <= now:
            if record.delta > 0.0:
                window_pos += record.delta
            elif record.delta < 0.0:
                window_neg += record.delta

    # room_down stays negative: -WINDOW_CAP minus the already applied negative
    # deltas. Writing +WINDOW_CAP here would raise every negative delta to +3.
    x = min(x, WINDOW_CAP - window_pos)
    x = max(x, -WINDOW_CAP - window_neg)

    # Same sign convention for the daily layer: -DAILY_CAP - today_acc.neg.
    x = min(x, DAILY_CAP - today_acc.pos)
    x = max(x, -DAILY_CAP - today_acc.neg)

    return x



HORMONE_FLOOR = 0.0
HORMONE_CEILING = 100.0
IPC_FLOOR = -100.0
IPC_CEILING = 100.0


def _apply_delta(value: float, delta: float, floor: float, ceiling: float) -> float:
    """Add ``delta`` to ``value`` and pin the result into ``[floor, ceiling]``.

    Clamping here, before the model round trip, is the point: ``PulseState``
    carries ``ge=0/le=100`` and ``IPCState`` carries ``ge=-100/le=100``, so an
    unclamped 103.0 would surface as a ``ValidationError`` from validation
    instead of the pinned boundary the contract asks for.  A stimulus arriving
    while a hormone already sits on its rail would then abort a request instead
    of pinning the value.
    """
    return min(ceiling, max(floor, value + delta))


def update_hormones(
    snapshot: S1Snapshot,
    pulse: PulseState,
    deltas: HormoneDeltas,
) -> tuple[S1Snapshot, PulseState]:
    """Apply one dotted-path delta across the two hormone carriers.

    ``pulse.*`` keys land on ``PulseState`` (the global layer shared by every
    ``scope_id``); ``ipc.*`` keys land on ``S1Snapshot.ipc`` (per user).  The
    split is why this returns a pair: a caller cannot apply the global half and
    accidentally leave the local half behind, because there is no way to receive
    one without the other.

    ``apply_limit_cap`` is deliberately *not* called here (human ruling
    2026-11-07 10:05).  That function limits a raw stimulus through the
    single / two-hour-window / daily layers and admits at most +/-1 per event;
    the deltas reaching this function are already limited, so capping again
    would silently shrink them.

    Only the two IPC numbers may move on the snapshot.  Everything else --
    ``version`` / ``scope_id`` / ``timestamp`` / ``last_active_at`` /
    ``last_decay_at`` / ``is_conflict`` / ``compute_tier`` / ``traits`` /
    ``emotions`` / ``relationship_stage`` / ``relationship_ceiling`` -- is
    carried through verbatim, and ``delayed_impact`` is copied rather than
    aliased so a later in-place edit of the result cannot reach back into the
    caller's pulse.
    """
    pulse_patch: dict[str, object] = {}
    ipc_patch: dict[str, float] = {}

    for path, delta in deltas.values.items():
        namespace, _, field = path.partition(".")
        if namespace == "pulse":
            current = getattr(pulse, field)
            pulse_patch[field] = _apply_delta(
                current, delta, HORMONE_FLOOR, HORMONE_CEILING
            )
        else:
            current = getattr(snapshot.ipc, field)
            ipc_patch[field] = _apply_delta(current, delta, IPC_FLOOR, IPC_CEILING)

    # delayed_impact never carries a delta (it is not a whitelist member); it is
    # copied so the returned pulse owns its own dict.
    new_pulse = PulseState.model_validate(
        pulse.model_dump()
        | pulse_patch
        | {"delayed_impact": dict(pulse.delayed_impact)}
    )

    new_snapshot = S1Snapshot.model_validate(
        snapshot.model_dump() | {"ipc": snapshot.ipc.model_dump() | ipc_patch}
    )
    return new_snapshot, new_pulse
