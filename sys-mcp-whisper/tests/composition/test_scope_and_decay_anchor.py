"""Regression locks for the two data-corruption bugs found in the P3 audit.

Both were silent: no exception, no retry, no dead letter -- the state simply
came out wrong.  That is exactly the class of failure the rest of the suite is
bad at noticing, so each one gets a behavioural lock here rather than a
comment.

1. **The decay anchor never advanced.**  ``apply_time_decay`` is clock-free:
   it stamps ``last_decay_at`` with the *input* timestamp, so only the
   orchestrator can move it to ``now``.  It did not, so every request re-decayed
   the whole span from the original anchor -- three requests an hour apart
   decayed 1h + 2h + 3h instead of 1h + 1h + 1h.

2. **``get()`` read a snapshot row from the wrong scope.**  The query was
   ``SELECT payload FROM s1_snapshot LIMIT 1`` with no ``WHERE scope_id``, so
   with two conversations on disk a request for one could load the other, run
   the entire pipeline on it and CAS-write it back.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from apps.composition.use_case import EmotionUseCase
from packages.core.domain.contracts import MCPRequest, MCPRequestContext
from packages.core.domain.global_context import GlobalContext
from packages.core.domain.state import IPCState, S1Snapshot, TraitState
from packages.core.engine.dynamics import DecayRates, apply_time_decay
from packages.core.ports import ObservabilityPort

_TS = datetime(2026, 11, 7, 12, 0, 0)


class _QuietObservability(ObservabilityPort):
    """No-op telemetry sink: this file is about state, not reporting."""

    def emit_agent_run(self, payload: dict[str, object]) -> None:
        return None

    def emit_token_usage(self, payload: dict[str, object]) -> None:
        return None

    def check_token_budget(self, estimated_tokens: int) -> bool:
        return True


def _snapshot(
    version: int = 1,
    scope_id: str = "scope-1",
    affiliation: float = 10.0,
    traits: dict[str, TraitState] | None = None,
) -> S1Snapshot:
    return S1Snapshot(
        version=version,
        scope_id=scope_id,
        timestamp=_TS,
        last_decay_at=_TS,
        is_conflict=False,
        compute_tier="warm",
        ipc=IPCState(
            affiliation=affiliation,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits=traits if traits is not None else {},
        emotions={},
    )


def _request(
    current_time: datetime = _TS, scope_id: str = "scope-1", message: str = "你真好"
) -> MCPRequest:
    return MCPRequest(
        request_id=f"uuid-{scope_id}",
        user_id="u-1",
        scope_id=scope_id,
        message=message,
        persona_card="",
        chat_mode="roleplay",
        context=MCPRequestContext(
            last_interaction_at=current_time, current_time=current_time
        ),
    )


def _store(tmp_path: Path) -> SqliteStateStore:
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    return store


def _seed(store: SqliteStateStore, snapshot: S1Snapshot) -> None:
    with store.transaction() as tx:
        assert store.save(tx, snapshot, 1, GlobalContext(), 0) is True
        tx.commit()


def _rows(store: SqliteStateStore) -> list[tuple[str, int]]:
    assert store._conn is not None
    return [
        (str(row[0]), int(row[1]))
        for row in store._conn.execute(
            "SELECT scope_id, version FROM s1_snapshot ORDER BY scope_id"
        ).fetchall()
    ]


# -- Bug 1: the decay anchor must advance exactly once per request -----------


def test_decay_anchor_advances_to_now_on_every_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _seed(store, _snapshot())
        case = EmotionUseCase(store, _QuietObservability())
        for hour in (1, 2, 3):
            at = _TS + timedelta(hours=hour)
            asyncio.run(case.execute(_request(current_time=at)))
            with store.transaction() as tx:
                snap, _ = store.get(tx, "scope-1")
                tx.rollback()
            assert snap is not None
            assert snap.last_decay_at == at, f"anchor stuck at {snap.last_decay_at}"
            assert snap.timestamp == at
    finally:
        store.close()


def test_decay_is_not_replayed_from_the_original_anchor(tmp_path: Path) -> None:
    """The double-decay signature: 1h+2h+3h collapses affiliation to 7.0.

    ``affiliation_per_hour`` is 0.5, so three correctly-decayed single-hour
    steps cost 0.5 each (10.0 -> 9.5 -> 9.0 -> 8.5).  Re-decaying from the
    original anchor each time would subtract 0.5, 1.0 and 1.5 instead.
    """
    store = _store(tmp_path)
    try:
        _seed(store, _snapshot())
        case = EmotionUseCase(store, _QuietObservability())
        seen: list[float] = []
        for hour in (1, 2, 3):
            asyncio.run(case.execute(_request(current_time=_TS + timedelta(hours=hour))))
            with store.transaction() as tx:
                snap, _ = store.get(tx, "scope-1")
                tx.rollback()
            assert snap is not None
            seen.append(snap.ipc.affiliation)
        assert seen == pytest.approx([9.5, 9.0, 8.5])
    finally:
        store.close()


def test_apply_time_decay_keeps_last_active_at() -> None:
    """A field missing from the rebuild list silently falls back to None.

    ``last_active_at`` has no reader yet (reserved by C3), which is precisely
    why dropping it went unnoticed -- the same way the P2.5 tier drop did.
    """
    stamp = _TS + timedelta(minutes=5)
    source = S1Snapshot.model_validate(
        _snapshot().model_dump() | {"last_active_at": stamp}
    )
    decayed, _ = apply_time_decay(
        source,
        GlobalContext().pulse,
        delta_t=timedelta(hours=1),
        rates=DecayRates(),
    )
    assert decayed.last_active_at == stamp


# -- Bug 2: a request may only ever read and write its own scope ------------


def test_get_returns_the_row_for_the_requested_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _seed(store, _snapshot(version=1, scope_id="chat_A"))
        with store.transaction() as tx:
            assert store.save(tx, _snapshot(version=2, scope_id="chat_B"), 1, GlobalContext(), 0)
            tx.commit()
        for scope in ("chat_A", "chat_B"):
            with store.transaction() as tx:
                snap, _ = store.get(tx, scope)
                tx.rollback()
            assert snap is not None
            assert snap.scope_id == scope
    finally:
        store.close()


def test_get_on_an_unknown_scope_does_not_borrow_another_row(tmp_path: Path) -> None:
    """No row for this scope means "no snapshot", not "somebody else's"."""
    store = _store(tmp_path)
    try:
        _seed(store, _snapshot(version=1, scope_id="chat_A"))
        with store.transaction() as tx:
            snap, _ = store.get(tx, "chat_B")
            tx.rollback()
        assert snap is None
    finally:
        store.close()


def test_request_for_one_scope_leaves_the_other_scope_byte_identical(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        _seed(
            store,
            _snapshot(
                version=1,
                scope_id="chat_A",
                traits={"tsundere": TraitState(score=85, visibility="unlocked")},
            ),
        )
        with store.transaction() as tx:
            assert store.save(tx, _snapshot(version=2, scope_id="chat_B"), 1, GlobalContext(), 0)
            tx.commit()
        assert store._conn is not None
        before = store._conn.execute(
            "SELECT payload FROM s1_snapshot WHERE scope_id = 'chat_A'"
        ).fetchone()

        response = asyncio.run(
            EmotionUseCase(store, _QuietObservability()).execute(
                _request(scope_id="chat_B")
            )
        )

        assert response.state_snapshot.scope_id == "chat_B"
        after = store._conn.execute(
            "SELECT payload FROM s1_snapshot WHERE scope_id = 'chat_A'"
        ).fetchone()
        assert after == before, "a chat_B request rewrote chat_A"
        assert _rows(store) == [("chat_A", 1), ("chat_B", 3)]
    finally:
        store.close()


def test_cross_scope_write_does_not_need_a_retry_or_a_dead_letter(
    tmp_path: Path,
) -> None:
    """The old bug looked like success: zero retries, zero dead letters.

    A correct implementation also reports zero -- but because it wrote the
    right row on the first attempt, not because it wrote the wrong one
    successfully.  The assertion on ``chat_B``'s version is what tells the
    two apart.
    """
    store = _store(tmp_path)
    try:
        _seed(store, _snapshot(version=1, scope_id="chat_A"))
        with store.transaction() as tx:
            assert store.save(tx, _snapshot(version=2, scope_id="chat_B"), 1, GlobalContext(), 0)
            tx.commit()
        case = EmotionUseCase(store, _QuietObservability())
        asyncio.run(case.execute(_request(scope_id="chat_B")))
        assert case.pending_retry_count == 0
        assert store.dead_letter_count() == 0
        assert _rows(store) == [("chat_A", 1), ("chat_B", 3)]
    finally:
        store.close()
