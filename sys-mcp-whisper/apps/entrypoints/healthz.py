"""Health-check HTTP surface (P3-H, plan-p3-orchestration section 3).

Why this module opens its own connection
----------------------------------------
``SqliteStateStore.connect()`` calls ``sqlite3.connect(self._path, isolation_level=None)``,
which keeps ``check_same_thread=True``.  A request handler -- ``def`` or
``async def`` -- runs on a worker thread of the ASGI server, so calling
``store.load()`` / ``store.get(tx)`` / ``store.dead_letter_count()`` from inside
a handler raises ``ProgrammingError: SQLite objects created in a thread can
only be used in that same thread``.  Both handler styles were measured to fail,
only the thread number differs.

Changing the adapter is out of scope here (it would drag the P3-D CAS suite and
the P3-A DDL locks along with it), so the probe opens a fresh read-only
connection per request and closes it in ``finally``.  Three consequences, each
measured on this machine:

* ``mode=ro`` never creates the file.  A missing database therefore surfaces as
  ``connected: False`` instead of being silently healed into an empty one,
  which is what a read-write connection would do.
* ``timeout=1.0`` bounds the wait when another thread holds the write lock.  A
  health probe that blocks forever is worse than one that reports trouble.
* Nothing here writes: the database file is byte-identical before and after a
  call (locked by H9).

The surface never answers with 5xx.  A health check that fails with 5xx is
indistinguishable from "the process is gone" to a monitor, while
``database.connected: False`` already carries the real information.  There is
no authentication either: a status summary is not a privileged resource.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from packages.core.domain.global_context import GlobalContext

if TYPE_CHECKING:  # pragma: no cover
    from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
    from apps.composition.use_case import EmotionUseCase

#: Upper bound on how long the probe waits for a lock held elsewhere.
READ_TIMEOUT_SECONDS = 1.0

#: ``detail`` is a diagnostic string, not a log sink: keep it short enough to
#: stay readable in a JSON body and to avoid echoing a whole traceback.
DETAIL_MAX_CHARS = 200

#: The only row ``global_context`` may hold (``CHECK id = 1`` in the schema).
GLOBAL_CONTEXT_ROW_ID = 1

#: Ruling P3-C-3: the legacy queue is never built, so its depth is permanently
#: zero.  Not a TODO -- there is no queue to report on.
LEGACY_QUEUE_PENDING = 0


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    """Open one read-only connection to ``db_path``.

    ``mode=ro`` is the point of this helper: it cannot create the file, so the
    probe reports a missing database rather than manufacturing an empty one.
    """
    uri = f"file:{db_path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS)


def _format_detail(exc: BaseException) -> str:
    """Render an exception as ``"<Type>: <message>"``, truncated for the body."""
    return f"{type(exc).__name__}: {exc}"[:DETAIL_MAX_CHARS]


def _probe(path: Path) -> tuple[int, dict[str, object] | None, bool, str | None]:
    """Read the global context with a throwaway connection.

    Returns ``(version, pulse, connected, detail)``.  ``version`` and ``pulse``
    both come from the same carrier on purpose: the returned pulse describes
    ``global_context``, so the version next to it must be that carrier's
    optimistic-lock version.  Pairing an ``s1_snapshot`` version with a
    ``global_context`` pulse would put two sources of truth behind one field.

    The payload is parsed by ``GlobalContext`` itself rather than by
    ``json.loads``: the shape of that payload has exactly one definition, in
    ``packages/core/domain/global_context.py``, and hand-reading it would fork
    a second copy that drifts.
    """
    try:
        conn = _read_only_connection(path)
    except sqlite3.Error as exc:
        return 0, None, False, _format_detail(exc)
    try:
        row = conn.execute(
            "SELECT payload FROM global_context WHERE id = ?",
            (GLOBAL_CONTEXT_ROW_ID,),
        ).fetchone()
        if row is None:
            return 0, None, True, None
        context = GlobalContext.model_validate_json(row[0])
        return context.version, context.pulse.model_dump(), True, None
    except (sqlite3.Error, ValueError) as exc:
        return 0, None, False, _format_detail(exc)
    finally:
        conn.close()


def _dead_letter_count(path: Path) -> tuple[int, str | None]:
    """Count the local dead-letter rows with a throwaway connection.

    Returns ``(count, detail)``.  On failure the count is zero *and* the reason
    travels back to the caller, which merges it into ``database.detail``: a
    probe that cannot read the table must neither pretend it read zero silently
    nor take the whole endpoint down.
    """
    try:
        conn = _read_only_connection(path)
    except sqlite3.Error as exc:
        return 0, _format_detail(exc)
    try:
        row = conn.execute("SELECT COUNT(*) FROM dead_letter_deltas").fetchone()
        return int(row[0]), None
    except sqlite3.Error as exc:
        return 0, _format_detail(exc)
    finally:
        conn.close()


def create_healthz_app(store: SqliteStateStore, usecase: EmotionUseCase) -> FastAPI:
    """Build the FastAPI app that serves ``GET /healthz``.

    ``store._path`` is private and read on purpose: this is a diagnostic probe
    that only needs to know *where* the database is, and adding a public getter
    would mean editing the adapter, which this card must not do.  Nothing in
    this module writes through that path.
    """
    app = FastAPI(title="fatfish-whisper healthz")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Report liveness plus a read-only view of the persisted state."""
        # Private path read on purpose: this is a diagnostic probe, and adding a
        # public getter would mean editing the adapter, which this card must not.
        db_path = store._path
        version, pulse, connected, probe_detail = _probe(db_path)
        dead_letters, dlq_detail = _dead_letter_count(db_path)
        detail = probe_detail if probe_detail is not None else dlq_detail
        return {
            "version": version,
            "pulse": pulse,
            "pulse_source": "global_context" if pulse is not None else "unavailable",
            "database": {"connected": connected, "detail": detail},
            "queue_backlog": {
                "dead_letter_count": dead_letters,
                "pending_retry_count": usecase.pending_retry_count,
                "legacy_queue_pending": LEGACY_QUEUE_PENDING,
            },
        }

    return app


__all__ = ["DETAIL_MAX_CHARS", "LEGACY_QUEUE_PENDING", "READ_TIMEOUT_SECONDS", "create_healthz_app"]
