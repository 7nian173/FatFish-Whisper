"""Behavioural locks for SqliteStateStore (task-FFW-P3-D, D1-D12).

Two ADR-009 rules are load-bearing here: R1 (the adapter never retries --
retry belongs to the UseCase) and the supplementary ruling S1 (``save``
returning ``False`` means nothing was written; the SAVEPOINT keeps both
tables byte-identical even if the caller commits anyway).  File-backed
databases everywhere except the one ``:memory:`` contrast test, because the
"two connections" semantics only mean something on disk.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from packages.core.domain.global_context import GlobalContext
from packages.core.domain.state import IPCState, S1Snapshot, TraitState

_NOW = datetime(2026, 11, 7, 10, 0, 0)


def make_snapshot(version: int = 1, scope_id: str = "s1") -> S1Snapshot:
    return S1Snapshot(
        version=version,
        scope_id=scope_id,
        timestamp=_NOW,
        last_decay_at=_NOW,
        is_conflict=False,
        ipc=IPCState(affiliation=0.0, dominance=0.0,
                     relationship_stage="friend", relationship_ceiling="lover"),
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        emotions={},
    )


@pytest.fixture()
def store(tmp_path: Path):
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    yield store
    store.close()


def _table_state(store: SqliteStateStore) -> bytes:
    """Byte-fingerprint of both carriers' payloads and versions."""
    assert store._conn is not None
    rows = store._conn.execute(
        "SELECT scope_id, version, payload FROM s1_snapshot"
        " UNION ALL SELECT CAST(id AS TEXT), version, payload FROM global_context"
    ).fetchall()
    return repr(sorted(rows)).encode("utf-8")


def test_d1_connect_creates_exactly_four_tables(store: SqliteStateStore) -> None:
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name != 'sqlite_sequence'"  # AUTOINCREMENT bookkeeping, not ours
    ).fetchall()
    assert {row[0] for row in rows} == {
        "global_context",
        "s1_snapshot",
        "dead_letter_deltas",
        "stimulus_ledger",  # task-FFW-P3-LIMIT: ADR-009 S5 window carrier
    }


def test_d2_empty_store_reads_none_pair(store: SqliteStateStore) -> None:
    with store.transaction() as tx:
        assert store.get(tx, "s1") == (None, None)


def test_d3_dual_write_persists_deserialized_payloads(store: SqliteStateStore) -> None:
    snap = make_snapshot(version=1)
    ctx = GlobalContext()
    with store.transaction() as tx:
        assert store.save(tx, snap, 1, ctx, 0) is True
        tx.commit()
    rows = store._conn.execute("SELECT COUNT(*) FROM s1_snapshot").fetchone()
    assert rows[0] == 1
    rows = store._conn.execute("SELECT COUNT(*) FROM global_context").fetchone()
    assert rows[0] == 1
    with store.transaction() as tx:
        read_snap, read_ctx = store.get(tx, snap.scope_id)
    assert read_snap == snap
    assert read_ctx == ctx


def test_d4_snapshot_version_conflict_leaves_both_tables_untouched(
    store: SqliteStateStore,
) -> None:
    with store.transaction() as tx:
        assert store.save(tx, make_snapshot(version=2), 1, GlobalContext(), 0) is True
        tx.commit()
    before = _table_state(store)
    stale = make_snapshot(version=9)
    with store.transaction() as tx:
        assert store.save(tx, stale, 1, GlobalContext(version=0), 0) is False
    assert _table_state(store) == before


def test_d5_context_version_conflict_reverts_the_snapshot_half_too(
    store: SqliteStateStore,
) -> None:
    with store.transaction() as tx:
        assert store.save(tx, make_snapshot(version=2), 1, GlobalContext(version=0), 0) is True
        tx.commit()
    before = _table_state(store)
    # stored context is v0; expecting v7 is the wrong credential
    with store.transaction() as tx:
        ok = store.save(tx, make_snapshot(version=3), 2, GlobalContext(version=1), 7)
        assert ok is False
    assert _table_state(store) == before, "the snapshot half must be reverted by the SAVEPOINT"


def test_d6_conflict_then_caller_commit_is_still_clean(store: SqliteStateStore) -> None:
    with store.transaction() as tx:
        assert store.save(tx, make_snapshot(version=2), 1, GlobalContext(version=0), 0) is True
        tx.commit()
    before = _table_state(store)
    with store.transaction() as tx:
        assert store.save(tx, make_snapshot(version=3), 2, GlobalContext(version=1), 7) is False
        tx.commit()  # the "misguided" caller commits anyway
    with store.transaction() as tx:
        snap, ctx = store.get(tx, "s1")
    assert _table_state(store) == before
    assert snap.version == 2 and ctx.version == 0


def test_d7_get_returns_atomic_pair_of_both_versions(store: SqliteStateStore) -> None:
    with store.transaction() as tx:
        assert store.save(tx, make_snapshot(version=3), 1, GlobalContext(version=1), 0) is True
        tx.commit()
    with store.transaction() as tx:
        snap, ctx = store.get(tx, "s1")
    assert snap.version == 3
    assert ctx.version == 1
    # Strengthened criterion (card M5): get(tx) must not commit/terminate the
    # surrounding transaction.  If get() sneaks in a commit mid-read, the
    # following save() degenerates to autocommit and survives the rollback.
    with store.transaction() as tx:
        store.get(tx, "s1")
        # stored pair after setup is snapshot v3 / context v1: echo those back
        assert store.save(tx, make_snapshot(version=4), 3, GlobalContext(version=2), 1) is True
        # no tx.commit() here
    # the v4/v2 half must be gone, the committed v3/v1 half intact
    with store.transaction() as tx:
        snap, ctx = store.get(tx, "s1")
    assert snap.version == 3 and ctx.version == 1


def test_d8_single_snapshot_semantics_match_the_port_mock(store: SqliteStateStore) -> None:
    first = make_snapshot(version=1, scope_id="chat_A")
    second = make_snapshot(version=2, scope_id="chat_B")
    assert store.save_if_version_match(first, 0) is True
    assert store.load() == first
    # stale credential must be refused and leave the row untouched
    before = _table_state(store)
    assert store.save_if_version_match(second, 0) is False
    assert store.load() == first
    assert _table_state(store) == before
    # the correct credential then succeeds
    assert store.save_if_version_match(make_snapshot(version=2, scope_id="chat_A"), 1) is True
    assert store.load().version == 2


def test_d9_exception_inside_transaction_rolls_back(store: SqliteStateStore) -> None:
    with store.transaction() as tx:
        assert store.save(tx, make_snapshot(version=1), 1, GlobalContext(), 0) is True
        tx.commit()
    before = _table_state(store)
    with pytest.raises(RuntimeError), store.transaction() as tx:
            assert store.save(tx, make_snapshot(version=2), 1, GlobalContext(), 0) is True
            raise RuntimeError("boom")
    assert _table_state(store) == before


def test_d10_exit_without_explicit_commit_rolls_back(store: SqliteStateStore) -> None:
    with store.transaction() as tx:
        assert store.save(tx, make_snapshot(version=1), 1, GlobalContext(), 0) is True
    with store.transaction() as tx:
        snap, ctx = store.get(tx, "s1")
    assert snap is None and ctx is None


def test_d11_dead_letter_insert_and_count(store: SqliteStateStore) -> None:
    assert store.dead_letter_count() == 0
    with store.transaction() as tx:
        store.record_dead_letter(tx, "req-9", 4, 5, "cas-mismatch", _NOW)
        assert store.dead_letter_count() == 1  # uncommitted row visible in-tx
        tx.commit()
    row = store._conn.execute(
        "SELECT request_id, context_version, snapshot_version, failure_reason, failed_at"
        " FROM dead_letter_deltas"
    ).fetchone()
    assert row[0] == "req-9"
    assert row[1] == 4 and row[2] == 5 and row[3] == "cas-mismatch"
    assert row[4] == _NOW.isoformat()


def test_d12_save_has_no_loop_and_no_retry_attribute(store: SqliteStateStore) -> None:
    source = Path(
        Path(__file__).resolve().parents[2]
        / "apps/adapters/outbound/sqlite_state_store.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    save_fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "save"
    )
    for node in ast.walk(save_fn):
        assert not isinstance(node, (ast.For, ast.While, ast.AsyncFor)), (
            f"retry loop inside save: {ast.dump(node)[:80]}"
        )
        if isinstance(node, ast.Attribute):
            assert "retry" not in node.attr.lower(), node.attr


def test_d13_memory_db_contrast_still_works() -> None:
    store = SqliteStateStore(":memory:")
    store.connect()
    try:
        with store.transaction() as tx:
            assert store.save(tx, make_snapshot(version=1), 1, GlobalContext(), 0) is True
            tx.commit()
        with store.transaction() as tx:
            snap, _ = store.get(tx, "s1")
        assert snap.version == 1
    finally:
        store.close()
