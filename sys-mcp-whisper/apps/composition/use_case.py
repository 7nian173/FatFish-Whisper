"""EmotionUseCase: the P3-G orchestration main loop (plan-p3 section 4).

Sixteen ordered steps over the real engine components.  Retry policy lives
HERE -- the adapters never retry (ADR-009 S1); ``TX_MAX_RETRIES`` caps how
many times a failed optimistic-lock save is replayed from step 1.  After the
budget is exhausted the failed request is written to the dead-letter table.

In-memory retry bookkeeping (``pending_retry_count``) is per-instance state
only (ADR-009 U3): never persisted, never shared across instances.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Final

from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from packages.core.domain.config import PLACEHOLDER_STIMULUS_UNIT_POINTS as STIMULUS_UNIT_POINTS
from packages.core.domain.contracts import (
    ActionIntent,
    MCPRequest,
    MCPResponse,
)
from packages.core.domain.global_context import GlobalContext, GlobalDelta
from packages.core.domain.state import IPCState, S1Snapshot
from packages.core.engine import assembler, commit, delay, dynamics, judge, routing, stimulus
from packages.core.engine.arbiter import DictionaryArbiter
from packages.core.engine.judge import HormoneDeltas
from packages.core.engine.router import (
    sanitize_tier_state,
    settle_before_tier_upgrade,
)
from packages.core.engine.routing import MessageEnvelope
from packages.core.ports import ObservabilityPort

#: Optimistic-lock replay budget (plan-p3 section 4, P3-G; blueprint ruling).
TX_MAX_RETRIES: Final[int] = 3

#: Track B coroutine timeout in seconds (plan-p3 section 4, step 5).
_TRACK_B_TIMEOUT: Final[float] = 0.8


def _merge_deltas(track_a_delta: dict[str, float], mapping: dict[str, float]) -> dict[str, float]:
    """Same-key addition over the union of both delta sources (step 6)."""
    merged = {
        k: track_a_delta.get(k, 0.0) + mapping.get(k, 0.0)
        for k in set(track_a_delta) | set(mapping)
    }
    # Zero-value pruning is an orchestration duty (to_delta explicitly leaves
    # it to this layer); HormoneDeltas' whitelist rejects zero-affect keys only
    # implicitly -- pruning keeps the stored deltas meaningful.
    return {k: v for k, v in merged.items() if v != 0.0}


class EmotionUseCase:
    """Compose the engine pipeline into one request -> response round trip."""

    def __init__(
        self,
        store: SqliteStateStore,
        observability: ObservabilityPort,
        max_retries: int = TX_MAX_RETRIES,
    ) -> None:
        self._store = store
        self._observability = observability
        self._max_retries = max_retries
        #: In-memory optimistic-lock failure counter (ADR-009 U3): per-instance
        #: only, never persisted, reset to 0 on a successful ``tx.commit()``.
        self.pending_retry_count: int = 0

    async def execute(self, request: MCPRequest) -> MCPResponse:
        # Step 13 retry loop: each attempt reopens a fresh transaction and
        # replays steps 1-13; a rolled-back tx is never reused.
        for attempt in range(self._max_retries):
            with self._store.transaction() as tx:
                # -- Step 1: open tx and load both carriers. ---------------
                snapshot, context = self._store.get(tx, request.scope_id)
                if snapshot is None or context is None:
                    # Empty-store cold start: brand-new carriers, version 1 / 0.
                    # [PLACEHOLDER-P4] initial trait/emotion seeding values are
                    # to be calibrated in the P4 load-testing stage.
                    snapshot = S1Snapshot(
                        version=1,
                        scope_id=request.scope_id,
                        timestamp=request.context.current_time,
                        last_decay_at=request.context.current_time,
                        is_conflict=False,
                        ipc=IPCState(
                            affiliation=0.0,
                            dominance=0.0,
                            relationship_stage="stranger",
                            relationship_ceiling="lover",
                        ),
                        traits={},
                        emotions={},
                    )
                    context = GlobalContext()

                # -- Step 2: read -> clamp (ADR-009 R2).  The clamp MUST run
                # before any engine pure function sees a clock value.
                now: datetime = max(
                    request.context.current_time, snapshot.last_decay_at
                )

                # -- Step 3: tier comes straight from the snapshot; heat
                # recalculation is NOT called here (human U-G5 ruling).
                # [PLACEHOLDER-P4] tier recalculation coupled with arb input
                # belongs to the P4 tier-aware arbiter card.
                tier = snapshot.compute_tier

                # -- Step 4: settle or sanitize depending on the tier. ------
                # Both carriers (snapshot AND pulse) must be settled
                # together (P2.8 ADR-008 S1.7): settle_before_tier_upgrade
                # returns the settled pulse and we must carry it into
                # Steps 8/9 -- discarding it would tear the two carriers
                # apart (see task-FFW-P3-TIER).  With tier taken verbatim
                # from snapshot.compute_tier the settle branch is currently
                # unreachable; the wiring below is defensive so the code
                # stays correct once tier is ever externalized.
                if tier == "cold":
                    snapshot = sanitize_tier_state(snapshot)
                    # Cold tier: no settlement; the global pulse passes
                    # through untouched.
                    context_pulse = context.pulse
                else:
                    snapshot, context_pulse = settle_before_tier_upgrade(
                        snapshot, context.pulse, now, tier
                    )

                # -- Step 5: routing (track B only for hot, safely falling
                # back to track A on timeout or any error). -----------------
                segments = routing.parse_speaker(
                    [MessageEnvelope(role="user", text=request.message)]
                )
                track_a = routing.route_track_a(segments, snapshot)
                if tier == "hot":
                    try:
                        track_b = await asyncio.wait_for(
                            routing.route_track_b(
                                MessageEnvelope(role="user", text=request.message),
                                snapshot,
                            ),
                            timeout=_TRACK_B_TIMEOUT,
                        )
                        track_a = track_b
                    except (TimeoutError, Exception):
                        track_a = track_a

                # -- Step 6: synthesize the hormone deltas. -----------------
                # The plain dictionary arbiter (P2's DictionaryArbiter) feeds
                # to_delta; the P4 tier-aware arbiter card owns any upgrade.
                # The confidence value is informational and not part of the
                # to_delta contract, so only the two attitude axes are passed.
                arb = DictionaryArbiter().evaluate(request.message)
                mapping = stimulus.to_delta(
                    {
                        "agreeableness": arb["agreeableness"],
                        "arousal": arb["arousal"],
                    }
                )
                merged = _merge_deltas(track_a.stimulus_delta, mapping)

                # -- Step 6b: business rate limiting (ADR-009 S5 closed).
                # Three-step unit conversion (human ruling m16532 Q2=A):
                # points -> units (/8) -> apply_limit_cap -> points (*8).
                # Only pulse.* dimensions go through the cap; ipc.* deltas
                # are relationship-verdict carriers and pass through raw.
                # The ledger row records what actually took effect (the
                # capped value), so the window and the daily accumulator
                # can never disagree with reality.
                limited: dict[str, float] = {}
                for key, points in merged.items():
                    if not key.startswith("pulse."):
                        limited[key] = points
                        continue
                    dimension = key.split(".", 1)[1]
                    history, today_acc = self._store.load_stimulus(
                        tx, snapshot.scope_id, dimension, now
                    )
                    records = [
                        dynamics.StimulusRecord(
                            at=at, delta=delta / STIMULUS_UNIT_POINTS
                        )
                        for at, delta in history
                    ]
                    accumulator = dynamics.DayAccumulator(
                        pos=today_acc[0] / STIMULUS_UNIT_POINTS,
                        neg=today_acc[1] / STIMULUS_UNIT_POINTS,
                    )
                    applied_units = dynamics.apply_limit_cap(
                        points / STIMULUS_UNIT_POINTS,
                        records,
                        now,
                        accumulator,
                    )
                    applied_points = applied_units * STIMULUS_UNIT_POINTS
                    self._store.append_stimulus(
                        tx,
                        snapshot.scope_id,
                        dimension,
                        now,
                        applied_points,
                    )
                    if applied_points != 0.0:
                        limited[key] = applied_points
                self._store.prune_stimulus(tx, snapshot.scope_id, now)
                merged = limited

                deltas = HormoneDeltas(values=merged)
                # Business rate limiting is wired in as of task-FFW-P3-LIMIT
                # (ADR-009 S5 closed).

                # -- Step 7: conflict verdict on the OLD snapshot (rule B):
                # must run before update_hormones mutates the hormone levels.
                verdict = judge.is_conflict(snapshot, deltas)

                # -- Step 8: hormone update -- cold tier included (rule A):
                # stacking happens at every tier, never gated by tier.
                # Uses the SETTLED pulse carried from Step 4, never
                # context.pulse (which would bypass settlement).
                snapshot2, pulse2 = dynamics.update_hormones(
                    snapshot, context_pulse, deltas
                )

                # -- Step 9: time decay over the clamped interval. ----------
                # delta_t is measured against the SETTLED snapshot's
                # last_decay_at.  When settlement happened, settle stamped
                # last_decay_at = now, so delta_t == timedelta(0) and the
                # decay is a no-op -- that zero is CORRECT and intentional:
                # the interval was already consumed inside settle.  Using
                # the pre-settlement value here would double-decay.
                snapshot3, pulse3 = dynamics.apply_time_decay(
                    snapshot2, pulse2, delta_t=now - snapshot.last_decay_at
                )

                # -- Step 10: write back the verdict without in-place
                # assignment (check_model_copy gate). ------------------------
                snapshot3 = S1Snapshot.model_validate(
                    snapshot3.model_dump() | {"is_conflict": verdict.conflicted}
                )

                # -- Step 11: assemble both message fields (P3-F). ----------
                system_head, tail_message = assembler.assemble_messages(
                    snapshot3,
                    pure=(request.chat_mode == "pure"),
                    pulse=pulse3,
                    persona_card=request.persona_card,
                )

                # -- Step 12: typing delay fed the TAIL length (rule C). ----
                delay_ms = delay.calculate_typing_delay(
                    pulse3, snapshot3.is_conflict, user_msg_len=len(tail_message)
                )

                # -- Step 13: persist with optimistic-lock replay. ----------
                # Step 9 decayed the interval [snapshot.last_decay_at, now],
                # but ``apply_time_decay`` is clock-free: it stamps the anchor
                # with the *input* timestamp, so the orchestrator is the only
                # place that can move it to ``now``.  Not moving it means the
                # next request re-decays the whole span from the original
                # anchor -- three requests one hour apart decayed 1h+2h+3h
                # instead of 1h+1h+1h (2026-11-07 audit, silent data drift).
                snapshot_final = S1Snapshot.model_validate(
                    snapshot3.model_dump()
                    | {
                        "version": snapshot3.version + 1,
                        "timestamp": now,
                        "last_decay_at": now,
                    }
                )
                # [PLACEHOLDER-P4] tension accumulation business rules are to
                # be calibrated in the P4 load-testing stage.
                # The new hormone levels (pulse3) live on GlobalContext.pulse
                # (ADR-008 §1) -- they reach the store only through the
                # context, so they are written back here via a model_validate
                # round trip (check_model_copy gate; no in-place assignment).
                context_with_pulse = GlobalContext.model_validate(
                    context.model_dump() | {"pulse": pulse3.model_dump()}
                )
                context2 = commit.commit_delta(
                    context_with_pulse, GlobalDelta(tension=0.0)
                )
                ok = self._store.save(
                    tx,
                    snapshot_final,
                    snapshot.version,
                    context2,
                    context.version,
                )
                if ok:
                    tx.commit()
                    self.pending_retry_count = 0
                    break
                tx.rollback()
                self.pending_retry_count += 1
                if attempt == self._max_retries - 1:
                    self._store.record_dead_letter(
                        tx,
                        request.request_id,
                        context.version,
                        snapshot.version,
                        "optimistic-lock-exhausted",
                        failed_at=now,
                    )
                    tx.commit()

        # -- Step 14: build the response (U-G4 placeholder intent). ---------
        # [PLACEHOLDER-P4] ActionIntent production logic belongs to P4
        # product design; this constant pair is a structural placeholder.
        response = MCPResponse(
            request_id=request.request_id,
            status="success",
            system_head=system_head,
            tail_message=tail_message,
            state_snapshot=snapshot_final,
            action_intent=ActionIntent(emotion="neutral", sticker_tag="none"),
            typing_delay_ms=delay_ms,
        )

        # -- Step 15: emit telemetry (U-G3 minimal payload skeleton). -------
        # [PLACEHOLDER-P4] telemetry payload schema is a minimal skeleton; the
        # full schema belongs to P4 observability design.
        self._observability.emit_agent_run(
            {
                "request_id": request.request_id,
                "route_id": track_a.route_id,
                "is_degraded": track_a.is_degraded,
                "tier": tier,
            }
        )
        self._observability.emit_token_usage({"request_id": request.request_id})

        # -- Step 16: return. ----------------------------------------------
        return response
