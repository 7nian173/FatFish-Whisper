"""Tier authority and settlement-integrity locks (task-FFW-P3-TIER).

Ruling (card section one): the tier authority is ``snapshot.compute_tier``
(option A).  GlobalContext carries only version/baseline/tension/pulse --
no heat or tier slot; MCPRequestContext carries only two clock fields --
no tier field.  Options B and C are field-refuted.

T1-T3 lock the settlement WIRING: they drive the exact engine sequence
Steps 4/8/9 must run when a settlement happens (externalized-tier
scenario), then push the result through the same storage round-trip the
orchestrator uses.  T4/T5 call the engine function directly -- as the
card allows -- because "is identity" is an engine contract that the
composed output cannot expose.  T6 is the behavioral drift lock for the
ruling: with tier taken verbatim from snapshot.compute_tier, a cold
snapshot must pass through a full request with its last_decay_at never
stamped (no settlement).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from apps.composition.use_case import EmotionUseCase
from packages.core.domain.contracts import MCPRequest, MCPRequestContext
from packages.core.domain.global_context import GlobalContext, PulseState
from packages.core.domain.state import IPCState, S1Snapshot, TraitState
from packages.core.engine.dynamics import (
    HormoneDeltas,
    apply_time_decay,
    update_hormones,
)
from packages.core.engine.router import settle_before_tier_upgrade
from packages.core.ports import ObservabilityPort

_REPO_ROOT = Path(__file__).resolve().parents[2]
_USE_CASE_PATH = _REPO_ROOT / "apps" / "composition" / "use_case.py"


class _QuietObservability(ObservabilityPort):
    """No-op telemetry sink, mirroring test_use_case's Recording sink."""

    def emit_agent_run(self, payload: dict[str, object]) -> None:
        return None

    def emit_token_usage(self, payload: dict[str, object]) -> None:
        return None

    def check_token_budget(self, estimated_tokens: int) -> bool:
        return True

_TS = datetime(2026, 11, 1, 12, 0, 0)
_NOW = _TS + timedelta(days=3)  # 2026-11-04


def _cold_snapshot(version: int = 1) -> S1Snapshot:
    """Cold snapshot idle for three days: IPC 80.0, decay stamp on _TS."""
    return S1Snapshot(
        version=version,
        scope_id="chat_settle",
        timestamp=_TS,
        last_decay_at=_TS,
        is_conflict=False,
        compute_tier="cold",
        ipc=IPCState(
            affiliation=80.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        emotions={},
    )


def _pulse100() -> PulseState:
    return PulseState(dopamine=100.0, cortisol=100.0, oxytocin=100.0)


def _context(pulse: PulseState) -> GlobalContext:
    return GlobalContext.model_validate(
        {"pulse": pulse.model_dump()}
    )


def _request() -> MCPRequest:
    return MCPRequest(
        request_id="uuid-settle-1",
        user_id="u-1",
        scope_id="chat_settle",
        message="今天天气不错",
        persona_card="",
        chat_mode="pure",
        context=MCPRequestContext(last_interaction_at=_TS, current_time=_NOW),
    )


def _store(tmp_path: Path, preseed: S1Snapshot | None = None,
           pulse: PulseState | None = None) -> SqliteStateStore:
    store = SqliteStateStore(tmp_path / "settle.db")
    store.connect()
    if preseed is not None:
        with store.transaction() as tx:
            assert store.save(tx, preseed, 0, _context(pulse or _pulse100()), 0) is True
            tx.commit()
    return store


def _run(case: EmotionUseCase, request: MCPRequest):
    return asyncio.run(case.execute(request))


def _use_case_source() -> str:
    return _USE_CASE_PATH.read_text(encoding="utf-8")


def _wired_settlement_sequence(
    snapshot: S1Snapshot, pulse: PulseState
) -> tuple[S1Snapshot, PulseState]:
    """The exact engine sequence Steps 4/8/9 must run when a settlement
    happened (externalized-tier scenario): settle carries the settled
    pulse into update_hormones, and Step 9 measures delta_t against the
    SETTLED snapshot -- which is timedelta(0) right after settlement.
    Mirrors the defensive wiring locked in use_case.py Steps 4/8/9."""
    settled, settled_pulse = settle_before_tier_upgrade(
        snapshot, pulse, _NOW, "warm"
    )
    # Step 8 with zero stimulus deltas ("今天天气不错" hits no mock rule).
    s2, p2 = update_hormones(settled, settled_pulse, HormoneDeltas(values={}))
    # Step 9: delta_t measured against the settled snapshot == timedelta(0).
    return apply_time_decay(s2, p2, delta_t=_NOW - s2.last_decay_at)


# -- T1 ---------------------------------------------------------------------


def test_settled_pulse_is_carried_not_discarded(tmp_path: Path) -> None:
    """The settled pulse (50.0 after three idle days) must reach the
    database through the orchestrator's storage round-trip -- never the
    stale pre-settlement pulse (100.0) that Step 4 returns alongside the
    settled snapshot."""
    # Wiring lock: Step 4 must BIND the settled pulse (a discarded `_`
    # would tear the two carriers apart -- the exact trap of card 0.5).
    assert "snapshot, context_pulse = settle_before_tier_upgrade(" in _use_case_source(), (
        "Step 4 must bind the settled pulse, not discard it with '_'"
    )
    store = _store(tmp_path)
    final, final_pulse = _wired_settlement_sequence(
        _cold_snapshot(), _pulse100()
    )
    assert final_pulse.dopamine == pytest.approx(50.0), (
        "settled pulse must be carried, not the pre-settlement 100.0"
    )
    # Same storage round-trip the orchestrator performs in Steps 13/14:
    # the pulse reaches the store only through GlobalContext.pulse.
    with store.transaction() as tx:
        assert store.save(tx, final, 1, _context(final_pulse), 0) is True
        tx.commit()
    tx2 = store.transaction()
    with tx2:
        got, ctx = store.get(tx2, "chat_settle")
    assert got is not None and ctx is not None
    assert ctx.pulse.dopamine == pytest.approx(50.0)


# -- T2 ---------------------------------------------------------------------


def test_settlement_advances_both_carriers(tmp_path: Path) -> None:
    """IPC affiliation (80 -> 44.0) and pulse (100 -> 50.0) must move
    together -- settlement may not advance only one carrier."""
    # Wiring lock: Step 8 must consume the SETTLED pulse carried from
    # Step 4, never context.pulse (which would bypass settlement).
    assert "snapshot, context_pulse, deltas" in _use_case_source(), (
        "Step 8 must feed update_hormones the settled context_pulse"
    )
    settled, settled_pulse = settle_before_tier_upgrade(
        _cold_snapshot(), _pulse100(), _NOW, "warm"
    )
    assert settled.ipc.affiliation == pytest.approx(44.0)
    assert settled_pulse.dopamine == pytest.approx(50.0)
    assert settled_pulse.cortisol == pytest.approx(50.0)
    assert settled_pulse.oxytocin == pytest.approx(50.0)
    # The correctly wired sequence preserves BOTH carriers.
    final, final_pulse = _wired_settlement_sequence(
        _cold_snapshot(), _pulse100()
    )
    assert final.ipc.affiliation == pytest.approx(44.0)
    assert final_pulse.dopamine == pytest.approx(50.0)


# -- T3 ---------------------------------------------------------------------


def test_step9_delta_t_is_zero_after_settlement(tmp_path: Path) -> None:
    """Right after settlement delta_t is timedelta(0) and the pulse is NOT
    decayed a second time (50.0 stays 50.0).  The zero is correct and
    intentional: the interval was already consumed inside settle.  Using
    the pre-settlement last_decay_at here would double-decay."""
    # Wiring lock: Step 9 must measure delta_t against the SETTLED
    # snapshot's stamp (snapshot.last_decay_at == now after settlement).
    assert "delta_t=now - snapshot.last_decay_at" in _use_case_source(), (
        "Step 9 must measure delta_t against the settled stamp so a "
        "settlement yields delta_t == timedelta(0), not a double decay"
    )
    settled, settled_pulse = settle_before_tier_upgrade(
        _cold_snapshot(), _pulse100(), _NOW, "warm"
    )
    delta_t = _NOW - settled.last_decay_at
    assert delta_t == timedelta(0)
    s2, p2 = update_hormones(settled, settled_pulse, HormoneDeltas(values={}))
    s3, p3 = apply_time_decay(s2, p2, delta_t=delta_t)
    assert p3.dopamine == pytest.approx(50.0)
    assert s3.ipc.affiliation == pytest.approx(44.0)
    assert s3.last_decay_at == _NOW


# -- T4 ---------------------------------------------------------------------


def test_no_settlement_when_tier_equals_snapshot() -> None:
    """This test drives the engine function directly, not through the
    orchestrator, because "is identity" is an engine contract that the
    composed output cannot expose.  Same-tier settle must return the very
    same objects (no settlement, no stamp)."""
    snap = _cold_snapshot()
    pulse = _pulse100()
    out_snap, out_pulse = settle_before_tier_upgrade(snap, pulse, _NOW, "cold")
    assert out_snap is snap
    assert out_pulse is pulse
    assert out_snap.last_decay_at == _TS  # not stamped to _NOW


# -- T5 ---------------------------------------------------------------------


def test_settle_is_identity_on_downgrade() -> None:
    """Direct engine call (same reason as T4): a downgrade (hot -> warm)
    must not settle -- identity return, hormones untouched."""
    snap = S1Snapshot.model_validate(
        _cold_snapshot().model_dump() | {"compute_tier": "hot"}
    )
    pulse = _pulse100()
    out_snap, out_pulse = settle_before_tier_upgrade(snap, pulse, _NOW, "warm")
    assert out_snap is snap
    assert out_pulse is pulse
    assert out_pulse.dopamine == pytest.approx(100.0)
    assert out_snap.last_decay_at == _TS


# -- T6 ---------------------------------------------------------------------


def test_tier_argument_cannot_drift_from_snapshot(tmp_path: Path) -> None:
    """Behavioral drift lock for the ruling (tier authority = option A).
    With tier taken verbatim from snapshot.compute_tier, a cold snapshot
    must pass through the full request still marked ``cold``.  If Step 3
    ever drifts to recomputing the tier (e.g. via evaluate_compute_tier),
    the cold snapshot could receive new_tier='warm' and settlement would
    rewrite compute_tier -- that is what the assertion below guards.

    The anchor does move, and that is the *orchestrator's* doing, not
    settlement's: Step 13 always stamps ``timestamp``/``last_decay_at``
    with ``now`` because ``apply_time_decay`` is clock-free and cannot.
    Before the 2026-11-07 audit this test read ``post.last_decay_at == _TS``
    and thereby froze a real bug in place -- a never-advancing anchor makes
    every request re-decay the whole span it already decayed."""
    store = _store(tmp_path, preseed=_cold_snapshot())
    tx0 = store.transaction()
    with tx0:
        pre, _ = store.get(tx0, "chat_settle")
    assert pre is not None
    assert pre.last_decay_at == _TS
    response = _run(EmotionUseCase(store, _QuietObservability()), _request())
    assert response.status == "success"
    tx1 = store.transaction()
    with tx1:
        post, _ = store.get(tx1, "chat_settle")
    assert post is not None
    # The tier argument may not drift.
    assert post.compute_tier == "cold"
    # The anchor advances with the clock even on a cold no-settlement path,
    # otherwise the next request would decay the same interval twice.
    assert post.last_decay_at == _NOW
    # And the preseeded stamp really did advance (guards a no-op Step 13).
    assert pre.last_decay_at != post.last_decay_at
