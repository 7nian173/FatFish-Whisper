"""Tests for packages/core/engine/dynamics.py (AC-E1..AC-E13).

This file was written before the engine sources on purpose: the first run must
fail with an ImportError and only turns green once dynamics.py exists.

All assertions are pure arithmetic expanded from plan-dynamics-engine.md, with
explicit DecayRates injection so no test locks a rate default.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.core.domain.exceptions import TimeDecayError
from packages.core.domain.global_context import PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.dynamics import (
    DayAccumulator,
    DecayRates,
    StimulusRecord,
    apply_limit_cap,
    apply_time_decay,
)

ENGINE_DIR = Path(__file__).resolve().parents[2] / "packages" / "core" / "engine"

# Single source of truth (task-FFW-P3-00): this file used to keep its own copy
# of the engine allowlist, which drifted from the authoritative table in
# ``30_沙盒区/scripts/check_imports.py`` -- most recently over ``pydantic``
# (fixed in task-FFW-P2-N3R2 by hand-patching the copy, and before that over
# pkg_core_* names that only made sense in the retired Nov-3 tree).  The copy
# is gone: we load the gate script by path (importlib, because the script is
# neither a package member nor on sys.path; a shared module would add a new
# file both sides must depend on) and read its ``ALLOWED_TOP`` directly, so
# script and suite can never disagree again.
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_imports.py"
_SPEC = importlib.util.spec_from_file_location("check_imports", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GATE)

ALLOWED_TOP_LEVEL_MODULES = _GATE.ALLOWED_TOP["packages.core.engine"]

BANNED_ATTRIBUTE_CALLS = {"now", "utcnow", "today", "fromtimestamp"}

TTL_SYMBOL_FRAGMENTS = ("ttl", "freeze", "decay_emotion")

def _guard_tokens(name: str) -> list[str]:
    """Split a symbol name into lowercase tokens across ``_`` and camelCase."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", spaced)
    return [t.lower() for t in spaced.split("_") if t]


def _matches_ttl_fragment(name: str) -> bool:
    tokens = _guard_tokens(name)
    lowered = name.lower()
    for fragment in TTL_SYMBOL_FRAGMENTS:
        if "_" in fragment:  # multi-word fragment: substring is fine
            if fragment in lowered:
                return True
        elif any(token.startswith(fragment) for token in tokens):
            return True
    return False

# One hour of decay at 1.0 hormone unit/h and 0.5 affiliation unit/h, so every
# expected value below is exact decimal arithmetic, not a default-value lock.
RATES = DecayRates(
    hormone_per_hour={"dopamine": 1.0, "cortisol": 1.0, "oxytocin": 1.0},
    affiliation_per_hour=0.5,
)

TS = datetime(2026, 11, 4, 12, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {"joy": EmotionValue(intensity=20.0, ttl_expires_at=TTL)}


def make_snapshot(
    *,
    affiliation: float = 0.0,
    dominance: float = 0.0,
    is_conflict: bool = False,
    timestamp: datetime = TS,
) -> S1Snapshot:
    """Build a valid S1Snapshot with the given scalar overrides.

    P2.8 (ADR-008 section 1.7) moved the three hormone lanes off the snapshot,
    so this factory no longer takes hormone values; ``make_pulse`` builds the
    carrier they live in now.
    """
    return S1Snapshot(
        version=1,
        scope_id="group_8888",
        timestamp=timestamp,
        last_decay_at=datetime(2026, 11, 4, 10, 0, 0),
        is_conflict=is_conflict,
        ipc=IPCState(
            affiliation=affiliation,
            dominance=dominance,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"warmth": TraitState(score=50, visibility="unlocked")},
        emotions=EMOTIONS,
    )


def make_pulse(
    *,
    dopamine: float = 50.0,
    cortisol: float = 50.0,
    oxytocin: float = 50.0,
    delayed_impact: dict[str, float] | None = None,
) -> PulseState:
    """Build the pulse carrier that holds the three hormone lanes."""
    return PulseState(
        dopamine=dopamine,
        cortisol=cortisol,
        oxytocin=oxytocin,
        delayed_impact={} if delayed_impact is None else delayed_impact,
    )


# ---------------------------------------------------------------------------
# AC-E2: delta_t == 0 returns an equal snapshot and does not raise
# ---------------------------------------------------------------------------


def test_zero_delta_returns_equal_snapshot_no_raise() -> None:
    snap = make_snapshot(affiliation=-30.0)
    pulse = make_pulse(dopamine=45.5, cortisol=80.0, oxytocin=10.0)

    result, result_pulse = apply_time_decay(snap, pulse, timedelta(0), RATES)

    assert result.model_dump() == snap.model_dump()
    assert result_pulse.model_dump() == pulse.model_dump()
    # Decision R1 option A: deep copies are returned, so the caller's objects
    # can never be reached through the results.
    assert result is not snap
    assert result_pulse is not pulse
    assert result.ipc is not snap.ipc


# ---------------------------------------------------------------------------
# AC-E3: delta_t < 0 raises TimeDecayError
# ---------------------------------------------------------------------------


def test_negative_delta_raises_time_decay_error() -> None:
    snap = make_snapshot(affiliation=10.0)

    with pytest.raises(TimeDecayError):
        apply_time_decay(snap, make_pulse(), timedelta(seconds=-1), RATES)


# ---------------------------------------------------------------------------
# AC-E4: hormones converge towards the 50 baseline from both sides
# ---------------------------------------------------------------------------


def test_hormone_regression_to_baseline() -> None:
    below_snap, below = apply_time_decay(
        make_snapshot(is_conflict=True),
        make_pulse(dopamine=45.5, cortisol=40.0, oxytocin=49.0),
        timedelta(hours=1),
        RATES,
    )
    assert below.dopamine == pytest.approx(46.5)
    assert below.cortisol == pytest.approx(41.0)
    assert below.oxytocin == pytest.approx(50.0)
    # Decay does not touch the snapshot half besides its anchor.
    assert below_snap.is_conflict is True

    above_snap, above = apply_time_decay(
        make_snapshot(),
        make_pulse(dopamine=55.0, cortisol=60.0, oxytocin=51.0),
        timedelta(hours=1),
        RATES,
    )
    assert above.dopamine == pytest.approx(54.0)
    assert above.cortisol == pytest.approx(59.0)
    assert above.oxytocin == pytest.approx(50.0)
    assert above_snap.last_decay_at == above_snap.timestamp

    # Already on the baseline: no movement in either direction.
    _, on_baseline = apply_time_decay(make_snapshot(), make_pulse(), timedelta(hours=1), RATES)
    assert on_baseline.dopamine == pytest.approx(50.0)
    assert on_baseline.cortisol == pytest.approx(50.0)
    assert on_baseline.oxytocin == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# AC-E5: affiliation decays towards 0 from both signs
# ---------------------------------------------------------------------------


def test_affiliation_decay_to_zero() -> None:
    positive, _ = apply_time_decay(
        make_snapshot(affiliation=60.0), make_pulse(), timedelta(hours=1), RATES
    )
    assert positive.ipc.affiliation == pytest.approx(59.5)

    negative, _ = apply_time_decay(
        make_snapshot(affiliation=-50.0), make_pulse(), timedelta(hours=1), RATES
    )
    assert negative.ipc.affiliation == pytest.approx(-49.5)

    near_zero, _ = apply_time_decay(
        make_snapshot(affiliation=0.2), make_pulse(), timedelta(hours=1), RATES
    )
    assert near_zero.ipc.affiliation == pytest.approx(0.0)

    # dominance is outside the contract 4.1 decay list: passed through untouched.
    assert positive.ipc.dominance == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# AC-E6: a huge delta_t clamps onto baseline / zero
# ---------------------------------------------------------------------------


def test_huge_delta_clamps_to_baseline() -> None:
    snap = make_snapshot(affiliation=-100.0)
    pulse = make_pulse(dopamine=0.0, cortisol=100.0, oxytocin=0.0)

    result, result_pulse = apply_time_decay(snap, pulse, timedelta(hours=1_000_000), RATES)

    assert result_pulse.dopamine == pytest.approx(50.0)
    assert result_pulse.cortisol == pytest.approx(50.0)
    assert result_pulse.oxytocin == pytest.approx(50.0)
    assert result.ipc.affiliation == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# AC-E7: single stimulus is capped at +/- 1
# ---------------------------------------------------------------------------


def test_single_stimulus_cap() -> None:
    empty: list[StimulusRecord] = []
    zero = DayAccumulator(pos=0.0, neg=0.0)

    assert apply_limit_cap(1.0, empty, TS, zero) == pytest.approx(1.0)
    assert apply_limit_cap(5.0, empty, TS, zero) == pytest.approx(1.0)
    assert apply_limit_cap(-1.0, empty, TS, zero) == pytest.approx(-1.0)
    assert apply_limit_cap(-5.0, empty, TS, zero) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# AC-E8: sliding two-hour window is capped at +/- 3
# ---------------------------------------------------------------------------


def test_two_hour_window_cap() -> None:
    history = [
        StimulusRecord(at=TS - timedelta(minutes=90), delta=1.0),
        StimulusRecord(at=TS - timedelta(minutes=30), delta=1.0),
    ]
    zero = DayAccumulator(pos=2.0, neg=0.0)
    assert apply_limit_cap(1.5, history, TS, zero) == pytest.approx(1.0)

    # Window already exhausted in the positive direction: allow "no movement".
    exhausted = [
        StimulusRecord(at=TS - timedelta(minutes=90), delta=1.0),
        StimulusRecord(at=TS - timedelta(minutes=30), delta=1.0),
        StimulusRecord(at=TS - timedelta(minutes=10), delta=1.0),
    ]
    assert apply_limit_cap(1.0, exhausted, TS, DayAccumulator(pos=3.0, neg=0.0)) == pytest.approx(
        0.0
    )

    # Records older than two hours do not participate in the window.
    old = [
        StimulusRecord(at=TS - timedelta(hours=3), delta=1.0),
        StimulusRecord(at=TS - timedelta(hours=2, seconds=1), delta=1.0),
    ]
    assert apply_limit_cap(1.0, old, TS, DayAccumulator(pos=2.0, neg=0.0)) == pytest.approx(1.0)

    neg_history = [StimulusRecord(at=TS - timedelta(minutes=20), delta=-2.0)]
    neg_acc = DayAccumulator(pos=0.0, neg=-2.0)
    assert apply_limit_cap(-1.5, neg_history, TS, neg_acc) == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# AC-E9: daily accumulation is capped at +/- 4
# ---------------------------------------------------------------------------


def test_daily_cap() -> None:
    # +3.5 happened outside the two-hour window, so only the daily layer binds.
    history: list[StimulusRecord] = []
    acc = DayAccumulator(pos=3.5, neg=0.0)

    assert apply_limit_cap(1.0, history, TS, acc) == pytest.approx(0.5)

    # Negative direction, daily quota exhausted: clamp to zero, not below.
    assert apply_limit_cap(-1.0, history, TS, DayAccumulator(pos=0.0, neg=-4.0)) == pytest.approx(
        0.0
    )

    # Negative direction with remaining quota.
    assert apply_limit_cap(-1.0, history, TS, DayAccumulator(pos=0.0, neg=-3.5)) == pytest.approx(
        -0.5
    )


# ---------------------------------------------------------------------------
# AC-E10: repeated calls respect all three layers
# ---------------------------------------------------------------------------


def test_repeated_calls_respect_caps() -> None:
    def run_sequence(sign: float) -> list[float]:
        history: list[StimulusRecord] = []
        pos = 0.0
        neg = 0.0
        results: list[float] = []
        for step in range(8):
            now = TS + timedelta(minutes=step)
            out = apply_limit_cap(sign * 1.0, history, now, DayAccumulator(pos=pos, neg=neg))
            results.append(out)
            history.append(StimulusRecord(at=now, delta=out))
            if out > 0:
                pos += out
            elif out < 0:
                neg += out
        return results

    assert run_sequence(1.0) == pytest.approx([1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert run_sequence(-1.0) == pytest.approx([-1.0, -1.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# AC-E12: decay has no side effect and passes is_conflict through
# ---------------------------------------------------------------------------


def test_decay_no_side_effect_and_is_conflict_passthrough() -> None:
    snap = make_snapshot(affiliation=-50.0, is_conflict=True)
    pulse = make_pulse(dopamine=45.5)
    before = snap.model_dump()
    pulse_before = pulse.model_dump()

    result, result_pulse = apply_time_decay(snap, pulse, timedelta(hours=1), RATES)

    # The caller's objects are untouched (no in-place mutation anywhere).
    assert snap.model_dump() == before
    assert pulse.model_dump() == pulse_before
    assert result is not snap
    assert result_pulse is not pulse
    assert result.is_conflict is True
    assert result.is_conflict == snap.is_conflict
    # Fields that decay does not own are passed through verbatim.
    assert result.traits == snap.traits
    assert result.emotions == snap.emotions
    assert result.ipc.dominance == snap.ipc.dominance
    assert result.version == snap.version
    assert result.scope_id == snap.scope_id
    assert result.timestamp == snap.timestamp
    # The decay anchor moves forward to the snapshot timestamp.
    assert result.last_decay_at == snap.timestamp


def test_limit_cap_does_not_mutate_inputs() -> None:
    history = [
        StimulusRecord(at=TS - timedelta(minutes=30), delta=1.0),
        StimulusRecord(at=TS - timedelta(minutes=5), delta=-1.0),
    ]
    before = list(history)
    acc = DayAccumulator(pos=1.0, neg=-1.0)

    out = apply_limit_cap(0.5, history, TS, acc)

    assert out == pytest.approx(0.5)
    assert history == before
    assert acc == DayAccumulator(pos=1.0, neg=-1.0)


# ---------------------------------------------------------------------------
# Purity guards: frozen dataclasses, import whitelist, clock, TTL absence
# ---------------------------------------------------------------------------


def test_engine_dataclasses_are_frozen() -> None:
    rates = DecayRates()
    assert isinstance(rates.hormone_per_hour, dict)
    assert set(rates.hormone_per_hour) == {"dopamine", "cortisol", "oxytocin"}
    assert isinstance(rates.affiliation_per_hour, float)

    with pytest.raises(AttributeError):
        rates.affiliation_per_hour = 9.9  # type: ignore[misc]

    record = StimulusRecord(at=TS, delta=1.0)
    with pytest.raises(AttributeError):
        record.delta = 2.0  # type: ignore[misc]

    with pytest.raises(AttributeError):
        DayAccumulator(pos=0.0, neg=0.0).pos = 1.0  # type: ignore[misc]


def engine_sources() -> list[Path]:
    sources = sorted(ENGINE_DIR.glob("*.py"))
    assert sources, "engine/ has no Python sources to inspect"
    return sources


def test_engine_import_whitelist() -> None:
    offenders: list[str] = []

    for path in engine_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in ALLOWED_TOP_LEVEL_MODULES:
                        offenders.append(f"import:{alias.name}")
            elif isinstance(node, ast.ImportFrom):
                # Relative imports (level > 0) are the only allowed cross-package
                # direction, and they must point downwards into ..domain.
                if node.level:
                    continue
                root = (node.module or "").split(".")[0]
                if root not in ALLOWED_TOP_LEVEL_MODULES:
                    offenders.append(f"from:{node.module}")

    assert offenders == []


def test_engine_sources_do_not_read_the_clock() -> None:
    def is_banned_time_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if not isinstance(node.func, ast.Attribute):
            return False
        return node.func.attr in BANNED_ATTRIBUTE_CALLS

    hits: list[str] = []
    for path in engine_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits.extend(
            node.func.attr
            for node in ast.walk(tree)
            if is_banned_time_call(node)
        )

    assert hits == []


def test_ttl_symbols_absent() -> None:
    # AC-E13 as an in-suite guard: comments may mention TTL, symbols may not.
    hits: set[str] = set()
    for path in engine_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and _matches_ttl_fragment(node.name):
                hits.add(node.name)

    assert sorted(hits) == []


def test_ttl_fragment_matcher_is_bidirectional() -> None:
    # P2.5-T6: the AC-E13 guard switched from raw substring matching to
    # tokenized word-prefix matching because ``settle_before_tier_upgrade``
    # contains the substring "settle" (false positive).  The new predicate
    # must still hit every genuine TTL/freeze symbol shape (including
    # CamelCase like EmotionTTL, which a naive ``ttl_`` prefix check would
    # miss) and never hit names that merely contain similar substrings.
    must_hit = (
        "ttl_expires_at",
        "TTLStore",
        "EmotionTTL",
        "freeze_emotion",
        "decay_emotions",
        "MyFreezeHook",
        "ttl",
        "emotion_ttl_store",
    )
    must_not_hit = (
        "settle_before_tier_upgrade",
        "settle_pending",
        "settleUpgrade",
        "battle_cry",
        "bottle",
        "brittle",
        "frozen_until",
    )
    for name in must_hit:
        assert _matches_ttl_fragment(name), name
    for name in must_not_hit:
        assert not _matches_ttl_fragment(name), name


# ---------------------------------------------------------------------------
# AC-G2-5 / AC-G2-6 (task-FFW-P0-C2): PulseState.delayed_impact is a
# structural companion dict in this card -- decay must not consume it, and
# must not mutate the caller's dict either.  The merge-into-hormone logic is
# P1-03's business, not this card's.
# ---------------------------------------------------------------------------


def test_delayed_impact_does_not_affect_decay() -> None:
    # AC-G2-5: pending delayed impacts are in-flight, NOT yet part of the
    # hormone values -- decaying the same pulse with and without the dict must
    # yield identical results everywhere.
    with_delayed = make_pulse(delayed_impact={"dopamine": 1.0})
    without_delayed = make_pulse()

    dec_with, pulse_with = apply_time_decay(
        make_snapshot(), with_delayed, timedelta(hours=1), RATES
    )
    dec_without, pulse_without = apply_time_decay(
        make_snapshot(), without_delayed, timedelta(hours=1), RATES
    )

    # Hormone levels: identical (the pending 1.0 must NOT leak into the
    # dopamine value that decay converges towards the baseline).
    assert pulse_with.dopamine == pulse_without.dopamine
    assert pulse_with.cortisol == pulse_without.cortisol
    assert pulse_with.oxytocin == pulse_without.oxytocin

    # And no other dimension moves either: ipc / traits / emotions equal.
    assert dec_with.ipc.model_dump() == dec_without.ipc.model_dump()
    assert {k: v.model_dump() for k, v in dec_with.traits.items()} == {
        k: v.model_dump() for k, v in dec_without.traits.items()
    }
    assert dec_with.emotions == dec_without.emotions


def test_delayed_impact_dict_not_mutated_by_decay() -> None:
    # AC-G2-6: apply_time_decay must leave the caller's delayed_impact dict
    # untouched.  Pydantic's frozen model config stops *field* assignment,
    # but a dict value can still be mutated in place -- this assertion makes
    # that boundary visible instead of silent.
    pulse = make_pulse(delayed_impact={"dopamine": 1.0})
    before = {"dopamine": 1.0}

    _, result_pulse = apply_time_decay(make_snapshot(), pulse, timedelta(hours=1), RATES)

    assert pulse.delayed_impact == before
    assert result_pulse.delayed_impact == before
    # The returned dict is a copy, not the caller's object.
    assert result_pulse.delayed_impact is not pulse.delayed_impact
