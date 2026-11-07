"""SQLite-backed state store: transactional dual-write adapter (plan-p3 section 4, P3-D).

Three facts this adapter lives by (ADR-009 R1 and supplementary rule S1):

1. This adapter only does "read and write both carriers inside one
   transaction".  Retry policy and dead-letter triggering policy belong to
   the UseCase layer (P3-G) -- there is deliberately no retry loop here.
2. ``save`` returning ``False`` means "nothing was written", not "already
   rolled back" -- rolling the surrounding transaction back (or not) is the
   caller's decision.  Inside ``save`` a SAVEPOINT guarantees the two tables
   stay consistent even if the caller then commits anyway.
3. ``sqlite3`` is the standard library; importing it does not create a
   dependency on ``packages.core`` beyond the domain models and ports this
   adapter serves.  It must never import ``packages.core.engine``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from apps.adapters.outbound.schema import ALL_DDL
from apps.adapters.outbound.stimulus_ledger import (
    APPEND_SQL as STIMULUS_APPEND_SQL,
)
from apps.adapters.outbound.stimulus_ledger import (
    DAY_ACC_SQL as STIMULUS_DAY_ACC_SQL,
)
from apps.adapters.outbound.stimulus_ledger import (
    PRUNE_SQL as STIMULUS_PRUNE_SQL,
)
from apps.adapters.outbound.stimulus_ledger import (
    WINDOW_SQL as STIMULUS_WINDOW_SQL,
)
from apps.adapters.outbound.stimulus_ledger import (
    day_start_utc,
    make_record,
    retention_cutoff_utc,
    to_utc_naive,
    window_start_utc,
)
from packages.core.domain.global_context import GlobalContext
from packages.core.domain.state import S1Snapshot

_SAVEPOINT = "ffw_save_state"


class _TxHandle:
    """Narrow transaction handle: only commit/rollback, per the port."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.committed = False

    def commit(self) -> None:
        self._conn.commit()
        self.committed = True

    def rollback(self) -> None:
        self._conn.rollback()


class SqliteStateStore:
    """Satisfies StateRepositoryPort and GlobalStatePort structurally."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        for ddl in ALL_DDL:
            self._conn.executescript(ddl)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def transaction(self):
        store = self

        class _Tx:
            current: _TxHandle | None = None
            def __enter__(self) -> _TxHandle:
                assert store._conn is not None
                store._conn.execute("BEGIN IMMEDIATE")
                handle = _TxHandle(store._conn)
                _Tx.current = handle
                return handle

            def __exit__(self, exc_type, exc, tb) -> bool:
                assert store._conn is not None
                handle = _Tx.current
                if exc_type is not None or handle is None or not handle.committed:
                    store._conn.rollback()
                return exc_type is None

        return _Tx()

    # -- StateRepositoryPort (single-snapshot semantics) ---------------------

    def save_if_version_match(self, snapshot: S1Snapshot, expected_version: int) -> bool:
        """MockStateRepository semantics: the version token is global.

        The stored row (whatever its scope_id) holds the only concurrency
        token; a writer must echo back the version it read.  See
        tests/ports/mock_state_repository.py for the reference semantics.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "UPDATE s1_snapshot SET scope_id = ?, version = ?, payload = ?"
            " WHERE version = ?",
            (snapshot.scope_id, snapshot.version, snapshot.model_dump_json(),
             expected_version),
        )
        if cur.rowcount == 1:
            return True
        exists = self._conn.execute("SELECT 1 FROM s1_snapshot").fetchone()
        if exists is not None:
            return False  # row present but version token mismatch: real conflict
        if expected_version != 0:
            return False  # empty store only accepts the virgin token 0
        self._conn.execute(
            "INSERT INTO s1_snapshot (scope_id, version, payload) VALUES (?, ?, ?)",
            (snapshot.scope_id, snapshot.version, snapshot.model_dump_json()),
        )
        return True

    def load(self) -> S1Snapshot | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT payload FROM s1_snapshot ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return S1Snapshot.model_validate_json(row[0])

    # -- GlobalStatePort ------------------------------------------------------

    def get(
        self, tx: _TxHandle, scope_id: str
    ) -> tuple[S1Snapshot | None, GlobalContext | None]:
        """One transaction, one read of both tables: no torn combination.

        ``scope_id`` is mandatory and used as a WHERE key, because the schema
        holds one snapshot row *per* dual-track scope (``s1_snapshot.scope_id``
        is the R3 isolation key and mirrors ``MCPRequest.scope_id`` exactly).
        Reading "whatever row comes back" was a real cross-scope corruption:
        with ``chat_A`` and ``chat_B`` both present, a request for ``chat_B``
        loaded ``chat_A``, ran the whole pipeline on it and then CAS-wrote it
        back — the retry counter stayed at 0 and no dead letter was recorded,
        so a silent success rewrote the wrong conversation.
        """
        assert self._conn is not None
        snap_row = self._conn.execute(
            "SELECT payload FROM s1_snapshot WHERE scope_id = ?", (scope_id,)
        ).fetchone()
        ctx_row = self._conn.execute(
            "SELECT payload FROM global_context WHERE id = 1"
        ).fetchone()
        snapshot = S1Snapshot.model_validate_json(snap_row[0]) if snap_row else None
        context = GlobalContext.model_validate_json(ctx_row[0]) if ctx_row else None
        return snapshot, context

    def save(
        self,
        tx: _TxHandle,
        snapshot: S1Snapshot,
        expected_snapshot_version: int,
        context: GlobalContext,
        expected_context_version: int,
    ) -> bool:
        assert self._conn is not None
        self._conn.execute(f"SAVEPOINT {_SAVEPOINT}")
        snap_ok = self._cas_upsert_snapshot(snapshot, expected_snapshot_version)
        if snap_ok:
            snap_ok = self._cas_upsert_context(context, expected_context_version)
        if not snap_ok:
            self._conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
            self._conn.execute(f"RELEASE {_SAVEPOINT}")
            return False
        self._conn.execute(f"RELEASE {_SAVEPOINT}")
        return True

    def _cas_upsert_snapshot(self, snapshot: S1Snapshot, expected_version: int) -> bool:
        assert self._conn is not None
        cur = self._conn.execute(
            "UPDATE s1_snapshot SET version = ?, payload = ? WHERE scope_id = ? AND version = ?",
            (snapshot.version, snapshot.model_dump_json(), snapshot.scope_id, expected_version),
        )
        if cur.rowcount == 1:
            return True
        exists = self._conn.execute(
            "SELECT 1 FROM s1_snapshot WHERE scope_id = ?", (snapshot.scope_id,)
        ).fetchone()
        if exists is not None:
            return False  # row present but version mismatch: real conflict
        self._conn.execute(
            "INSERT INTO s1_snapshot (scope_id, version, payload) VALUES (?, ?, ?)",
            (snapshot.scope_id, snapshot.version, snapshot.model_dump_json()),
        )
        return True

    def _cas_upsert_context(self, context: GlobalContext, expected_version: int) -> bool:
        assert self._conn is not None
        cur = self._conn.execute(
            "UPDATE global_context SET version = ?, payload = ? WHERE id = 1 AND version = ?",
            (context.version, context.model_dump_json(), expected_version),
        )
        if cur.rowcount == 1:
            return True
        exists = self._conn.execute(
            "SELECT 1 FROM global_context WHERE id = 1"
        ).fetchone()
        if exists is not None:
            return False  # row present but version mismatch: real conflict
        self._conn.execute(
            "INSERT INTO global_context (id, version, payload) VALUES (1, ?, ?)",
            (context.version, context.model_dump_json()),
        )
        return True

    # -- Dead letter & probe support (not part of any port) -------------------

    def record_dead_letter(
        self,
        tx: _TxHandle,
        request_id: str,
        context_version: int,
        snapshot_version: int,
        reason: str,
        failed_at: datetime,
    ) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO dead_letter_deltas (request_id, context_version,"
            " snapshot_version, failure_reason, failed_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, context_version, snapshot_version, reason, failed_at.isoformat()),
        )

    def dead_letter_count(self) -> int:
        assert self._conn is not None
        row = self._conn.execute("SELECT COUNT(*) FROM dead_letter_deltas").fetchone()
        return int(row[0])

    # -- Stimulus ledger (ADR-009 S5, task-FFW-P3-LIMIT) ----------------------
    # Not part of any Protocol on purpose: tests/ports/test_protocol_shapes.py
    # hard-locks the six-Protocol member sets, and EmotionUseCase.__init__
    # already receives the concrete SqliteStateStore (apps/composition/use_case.py),
    # so there is no "swap the implementation" abstraction to preserve.

    def load_stimulus(
        self, tx, scope_id: str, dimension: str, now: datetime
    ) -> tuple[list[tuple[datetime, float]], tuple[float, float]]:
        """Read the 2h window and the current day's accumulator for one dimension.

        Returns ``(rows, day_acc)`` where ``rows`` are ``(at, delta)`` pairs of
        the window contents and ``day_acc`` is ``(positive_sum, negative_sum)``.
        Raw pairs, not engine dataclasses: adapters must not import
        ``packages.core.engine`` (P3-GATE2); the composition layer converts.
        """
        assert self._conn is not None
        window_rows = self._conn.execute(
            STIMULUS_WINDOW_SQL,
            (
                scope_id,
                dimension,
                to_utc_naive(window_start_utc(now)).isoformat(),
                to_utc_naive(now).isoformat(),
            ),
        ).fetchall()
        rows = [
            make_record(row[0], float(row[1])) for row in window_rows
        ]
        pos, neg = self._conn.execute(
            STIMULUS_DAY_ACC_SQL,
            (
                scope_id,
                dimension,
                to_utc_naive(day_start_utc(now)).isoformat(),
                to_utc_naive(now).isoformat(),
            ),
        ).fetchone()
        return rows, (float(pos), float(neg))

    def append_stimulus(
        self, tx, scope_id: str, dimension: str, at: datetime, applied_delta: float
    ) -> None:
        """Record one capped stimulus unit.  Caller must be inside a transaction."""
        assert self._conn is not None
        self._conn.execute(
            STIMULUS_APPEND_SQL,
            (scope_id, dimension, to_utc_naive(at).isoformat(), applied_delta),
        )

    def prune_stimulus(self, tx, scope_id: str, now: datetime) -> None:
        """Drop rows that can no longer affect either the window or the day."""
        assert self._conn is not None
        self._conn.execute(
            STIMULUS_PRUNE_SQL,
            (scope_id, to_utc_naive(retention_cutoff_utc(now)).isoformat()),
        )
