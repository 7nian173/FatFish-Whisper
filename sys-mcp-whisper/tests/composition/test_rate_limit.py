"""Business rate limiting tests (task-FFW-P3-LIMIT, ADR-009 section S5).

These tests pin the three-step unit conversion (points / 8 -> cap -> * 8),
the pulse-only scope of the cap, the 2h window boundaries, the caller-
timezone day accumulator, the retention prune, the CAS-retry accounting,
and the per-scope isolation of the stimulus ledger.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from apps.composition.use_case import EmotionUseCase
from packages.core.domain.config import PLACEHOLDER_STIMULUS_UNIT_POINTS
from packages.core.domain.contracts import MCPRequest, MCPRequestContext
from packages.core.domain.global_context import GlobalContext
from packages.core.domain.state import IPCState, S1Snapshot, TraitState
from packages.core.ports import ObservabilityPort

_TZ08 = timezone(timedelta(hours=8))
_TS = datetime(2026, 11, 7, 12, 0, 0)


class _Rec(ObservabilityPort):
    """Minimal observability probe."""

    def emit_agent_run(self, payload: dict[str, object]) -> None:
        pass

    def emit_token_usage(self, payload: dict[str, object]) -> None:
        pass

    def check_token_budget(self, estimated_tokens: int) -> bool:
        return True


def _snapshot(scope_id: str = "scope-1", version: int = 1) -> S1Snapshot:
    return S1Snapshot(
        version=version,
        scope_id=scope_id,
        timestamp=_TS,
        last_decay_at=_TS,
        is_conflict=False,
        compute_tier="warm",
        ipc=IPCState(
            affiliation=10.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        emotions={},
    )


def _store(tmp_path: Path, scope_id: str = "scope-1") -> SqliteStateStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    with store.transaction() as tx:
        assert store.save(tx, _snapshot(scope_id), 0, GlobalContext(), 0) is True
        tx.commit()
    return store


def _request(
    message: str, now: datetime | None = None, scope_id: str = "scope-1"
) -> MCPRequest:
    ct = now if now is not None else _TS
    return MCPRequest(
        request_id="uuid-probe-1",
        user_id="u-1",
        scope_id=scope_id,
        message=message,
        persona_card="",
        chat_mode="roleplay",
        context=MCPRequestContext(last_interaction_at=ct, current_time=ct),
    )


def _run(case: EmotionUseCase, request: MCPRequest) -> object:
    return asyncio.run(case.execute(request))


def _read_pulse_change(store: SqliteStateStore, request: MCPRequest) -> tuple[float, float]:
    """Run one request and return (cortisol change, affiliation change)."""
    case = EmotionUseCase(store, _Rec())
    with store.transaction() as tx:
        before_snap, before_ctx = store.get(tx, request.scope_id)
        tx.rollback()
    _run(case, request)
    with store.transaction() as tx:
        after_snap, after_ctx = store.get(tx, request.scope_id)
        tx.rollback()
    return (
        after_ctx.pulse.cortisol - before_ctx.pulse.cortisol,
        after_snap.ipc.affiliation - before_snap.ipc.affiliation,
    )


def _ledger_rows(store: SqliteStateStore) -> list[tuple[str, str, str, float]]:
    assert store._conn is not None
    return store._conn.execute(
        "SELECT scope_id, dimension, at, delta FROM stimulus_ledger ORDER BY at"
    ).fetchall()


def _insert_row(
    store: SqliteStateStore,
    scope_id: str,
    dimension: str,
    at: datetime,
    delta: float,
) -> None:
    with store.transaction() as tx:
        store.append_stimulus(tx, scope_id, dimension, at, delta)
        tx.commit()


# -- L1: a lexicon hit is capped at one unit ----------------------------------


def test_l1_lexicon_hit_is_capped_at_one_unit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cortisol_change, _ = _read_pulse_change(store, _request("滚"))
    # merged = track_a 2.0 + lexicon 8.4 = 10.4 points = 1.3 units;
    # SINGLE_STIMULUS_CAP = 1.0 -> 8.0 points (not 10.4, not 8.4).
    assert cortisol_change == 8.0
    rows = [r for r in _ledger_rows(store) if r[1] == "cortisol"]
    assert len(rows) == 1
    assert rows[0][3] == 8.0


def test_l1_rev_sub_cap_value_passes_through_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    case = EmotionUseCase(store, _Rec())
    with store.transaction() as tx:
        _, before_ctx = store.get(tx, "scope-1")
        tx.rollback()
    _run(case, _request("我好喜欢你"))
    with store.transaction() as tx:
        _, after_ctx = store.get(tx, "scope-1")
        tx.rollback()
    dopamine_change = after_ctx.pulse.dopamine - before_ctx.pulse.dopamine
    # 4.2 points = 0.525 units < 1.0 -> untouched (within float noise).
    assert abs(dopamine_change - 4.2) < 1e-9
    rows = [r for r in _ledger_rows(store) if r[1] == "dopamine"]
    assert len(rows) == 1 and abs(rows[0][3] - 4.2) < 1e-9


def test_l1_rev_track_a_only_hit_passes_through(tmp_path: Path) -> None:
    # Track A's own scale is far below the cap: 滚 emits 2.0 points on the
    # track-A side (0.25 units).  In the full pipeline the lexicon hit co-fires
    # (L1), so the isolated track-A magnitude is pinned at the conversion seam
    # the use case calls, and a pure track-A-only lexicon hit (我难过 -> 1.5
    # points, no dictionary match) is proven end-to-end.
    from packages.core.engine.dynamics import DayAccumulator, apply_limit_cap

    units = apply_limit_cap(
        2.0 / PLACEHOLDER_STIMULUS_UNIT_POINTS,
        [],
        _TS,
        DayAccumulator(pos=0.0, neg=0.0),
    )
    assert units * PLACEHOLDER_STIMULUS_UNIT_POINTS == 2.0
    store = _store(tmp_path)
    cortisol_change, _ = _read_pulse_change(store, _request("我难过"))
    assert cortisol_change == 1.5
    rows = [r for r in _ledger_rows(store) if r[1] == "cortisol"]
    assert len(rows) == 1 and rows[0][3] == 1.5


# -- L2: ipc.* is never limited ------------------------------------------------


def test_l2_ipc_is_not_limited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, aff_change = _read_pulse_change(store, _request("滚"))
    # merged affiliation = track_a -1.5 + lexicon -4.05 = -5.55, raw.
    assert aff_change == -5.55
    assert all(r[1] != "affiliation" for r in _ledger_rows(store))


# -- L3: the window saturates after three units --------------------------------


def test_l3_window_saturates_after_three_units(tmp_path: Path) -> None:
    store = _store(tmp_path)
    case = EmotionUseCase(store, _Rec())
    changes: list[float] = []
    before = 50.0
    for _ in range(4):
        _run(case, _request("滚"))
        with store.transaction() as tx:
            _, ctx2 = store.get(tx, "scope-1")
            tx.rollback()
        changes.append(ctx2.pulse.cortisol - before)
        before = ctx2.pulse.cortisol
    # Window usage 1.0 -> 2.0 -> 3.0 -> full: the fourth round gets 0.
    assert changes == [8.0, 8.0, 8.0, 0.0]


# -- L4: the window is left-open right-closed ----------------------------------


def test_l4_window_is_left_open_right_closed(tmp_path: Path) -> None:
    # A row exactly at now - 2h is OUTSIDE the window (left-open): the cap
    # stays 1.0 units -> 8.0 points.
    store_a = _store(tmp_path / "a")
    _insert_row(store_a, "scope-1", "cortisol", _TS - timedelta(hours=2), 24.0)
    cortisol_a, _ = _read_pulse_change(store_a, _request("滚"))
    assert cortisol_a == 8.0
    # Adapter-level probe on a FRESH store holding only the boundary row:
    # the window READ itself must exclude it (left-open at the SQL operator
    # too) -- otherwise the engine-side predicate would silently mask a
    # closed-left window.
    store_probe = _store(tmp_path / "probe")
    _insert_row(store_probe, "scope-1", "cortisol", _TS - timedelta(hours=2), 24.0)
    with store_probe.transaction() as tx:
        boundary_history, _ = store_probe.load_stimulus(tx, "scope-1", "cortisol", _TS)
        tx.rollback()
    assert boundary_history == []
    # One second inside the window: 24.0 points = 3.0 units fill the window,
    # so the new stimulus is capped to 0.
    store_b = _store(tmp_path / "b")
    _insert_row(
        store_b, "scope-1", "cortisol", _TS - timedelta(hours=2) + timedelta(seconds=1), 24.0
    )
    cortisol_b, _ = _read_pulse_change(store_b, _request("滚"))
    assert cortisol_b == 0.0


# -- L5: the day accumulator uses the caller's timezone ------------------------


def test_l5_day_accumulator_uses_caller_timezone(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    # now = +08:00 01:00 == 2026-11-06T17:00Z.  The caller's day starts at
    # 2026-11-06T16:00Z, so 16:30Z is "today" and 15:30Z is "yesterday".
    now = datetime(2026, 11, 7, 1, 0, 0, tzinfo=_TZ08)
    _insert_row(
        store, "scope-1", "cortisol", datetime(2026, 11, 6, 16, 30, tzinfo=UTC), 8.0
    )
    _insert_row(
        store, "scope-1", "cortisol", datetime(2026, 11, 6, 15, 30, tzinfo=UTC), 8.0
    )
    with store.transaction() as tx:
        history, (pos, neg) = store.load_stimulus(tx, "scope-1", "cortisol", now)
        tx.rollback()
    # Both rows sit inside the 2h window (15:00Z, 17:00Z].
    assert len(history) == 2
    # Only the 16:30Z row belongs to the caller's day: pos == 8.0 (a UTC-day
    # implementation would return 16.0).
    assert abs(pos - 8.0) < 1e-9
    assert neg == 0.0


# -- L6: prune keeps rows inside the window across midnight --------------------


def test_l6_prune_keeps_rows_inside_the_window_across_midnight(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    now = datetime(2026, 11, 7, 0, 30, 0)
    # now - 1h = 2026-11-06T23:30Z: before the new day start but inside the
    # 2h window (window starts 2026-11-06T22:30Z).  Prune must keep it.
    _insert_row(store, "scope-1", "cortisol", now - timedelta(hours=1), 8.0)
    with store.transaction() as tx:
        store.prune_stimulus(tx, "scope-1", now)
        tx.commit()
    rows = _ledger_rows(store)
    assert len(rows) == 1 and rows[0][2] == "2026-11-06T23:30:00"


def test_l6_rev_pruned_rows_are_gone(tmp_path: Path) -> None:
    # A row from before the retention cutoff (min(day start, now - 2h)) is
    # gone after prune.  Note: a same-day row (e.g. now - 5h with now at
    # noon) sits AFTER the day start, so it still feeds the daily
    # accumulator and must be KEPT -- only rows older than the earlier of
    # the two lower bounds are prunable.
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    now = datetime(2026, 11, 7, 12, 0, 0)
    _insert_row(store, "scope-1", "cortisol", now - timedelta(hours=30), 8.0)
    with store.transaction() as tx:
        store.prune_stimulus(tx, "scope-1", now)
        tx.commit()
    assert _ledger_rows(store) == []


# -- L7: a CAS retry does not double count --------------------------------------


class _FlakyLedgerStore(SqliteStateStore):
    """First save fails (CAS conflict), the retry succeeds."""

    calls = 0

    def save(self, tx, snapshot, expected_snapshot_version, context, expected_context_version):  # type: ignore[no-untyped-def]
        _FlakyLedgerStore.calls += 1
        if _FlakyLedgerStore.calls == 1:
            return False
        return super().save(
            tx, snapshot, expected_snapshot_version, context, expected_context_version
        )


def test_l7_retry_does_not_double_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fake = _FlakyLedgerStore.__new__(_FlakyLedgerStore)
    fake.__dict__.update(store.__dict__)
    _FlakyLedgerStore.calls = 0
    case = EmotionUseCase(fake, _Rec())
    _run(case, _request("滚"))
    rows = [r for r in _ledger_rows(store) if r[1] == "cortisol"]
    # The ledger insert is inside the transaction that failed its CAS: the
    # rollback must take the row with it, leaving exactly one record.
    assert len(rows) == 1 and rows[0][3] == 8.0


# -- L8: the ledger is per scope and per dimension ------------------------------


def test_l8_ledger_is_per_scope_and_per_dimension(tmp_path: Path) -> None:
    # The store's get() reads the single active snapshot row (P3-A/P3-D
    # ruling), so two scopes each drive their own store file end-to-end;
    # the per-scope SQL keying itself is pinned directly on one database
    # via load_stimulus with two scope ids.
    store_1 = _store(tmp_path / "s1", "scope-1")
    store_2 = _store(tmp_path / "s2", "scope-2")
    change_1, _ = _read_pulse_change(store_1, _request("滚", scope_id="scope-1"))
    change_2, _ = _read_pulse_change(store_2, _request("滚", scope_id="scope-2"))
    assert change_1 == 8.0
    # scope-2's window is empty: the second 滚 is not eaten by scope-1's use.
    assert change_2 == 8.0
    # Direct probe: one database, two scope ids, disjoint windows.
    _insert_row(store_1, "scope-1", "cortisol", _TS - timedelta(minutes=30), 8.0)
    with store_1.transaction() as tx:
        history_1, _ = store_1.load_stimulus(tx, "scope-1", "cortisol", _TS)
        history_2, _ = store_1.load_stimulus(tx, "scope-2", "cortisol", _TS)
        tx.rollback()
    assert len(history_1) == 2
    assert history_2 == []


def test_l8_rev_empty_deltas_write_no_ledger_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before = len(_ledger_rows(store))
    case = EmotionUseCase(store, _Rec())
    _run(case, _request("今天天气不错"))
    assert len(_ledger_rows(store)) == before
