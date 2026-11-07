"""DDL shape locks for apps.adapters.outbound.schema (task-FFW-P3-A).

The three tables are the persistence surface decided by
plan-p3-orchestration.md section 6.  These tests run the *real* DDL strings
against an in-memory sqlite3 database (standard library, no new dependency)
so a loosened CHECK or a renamed column turns red instead of surviving until
the store implementation ships.  S8 keeps the module import-free -- that is
the precondition that lets this task ship in parallel with the gate-extension
task.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.adapters.outbound.schema import ALL_DDL

TABLE_NAMES = ("global_context", "s1_snapshot", "dead_letter_deltas",
               "stimulus_ledger")


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for ddl in ALL_DDL:
        conn.executescript(ddl)
    return conn


def test_s1_all_ddl_holds_exactly_four_statements() -> None:
    """Section 6: exactly the four decided tables, no more, no fewer."""
    assert len(ALL_DDL) == 4
    assert [name for name in TABLE_NAMES] == [
        "global_context",
        "s1_snapshot",
        "dead_letter_deltas",
        "stimulus_ledger",
    ]
    for ddl, name in zip(ALL_DDL, TABLE_NAMES, strict=True):
        assert f"CREATE TABLE IF NOT EXISTS {name}" in ddl


def test_s2_every_ddl_executes_on_real_sqlite() -> None:
    """The DDL must be valid as written -- executescript, not string surgery."""
    conn = _memory_db()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {row[0] for row in rows}
    assert set(TABLE_NAMES) <= names, names
    conn.close()


def test_s3_global_context_has_pinned_columns() -> None:
    """R1: id/version/payload with version NOT NULL (optimistic-lock column)."""
    conn = _memory_db()
    info = conn.execute("PRAGMA table_info(global_context)").fetchall()
    columns = {row[1]: row for row in info}
    assert {"id", "version", "payload"} <= set(columns), set(columns)
    assert columns["version"][3] == 1, columns["version"]  # notnull flag
    conn.close()


def test_s4_global_context_rejects_second_row() -> None:
    """R1: single-row carrier -- CHECK (id = 1) must refuse id = 2."""
    conn = _memory_db()
    conn.execute(
        "INSERT INTO global_context (id, version, payload) VALUES (1, 0, '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO global_context (id, version, payload) VALUES (2, 0, '{}')"
        )
    conn.close()


def test_s5_global_context_rejects_negative_version() -> None:
    """R1: versions never go backwards -- CHECK (version >= 0) must hold."""
    conn = _memory_db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO global_context (id, version, payload) VALUES (1, -1, '{}')"
        )
    conn.close()


def test_s6_s1_snapshot_scope_id_is_primary_key() -> None:
    """R3: one row per dual-track scope; a repeated scope_id must conflict."""
    conn = _memory_db()
    conn.execute(
        "INSERT INTO s1_snapshot (scope_id, version, payload) VALUES ('s1', 0, '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO s1_snapshot (scope_id, version, payload) VALUES ('s1', 1, '{}')"
        )
    conn.close()


def test_s7_dead_letter_table_has_six_pinned_columns() -> None:
    """U1 final ruling: five business columns + AUTOINCREMENT seq."""
    conn = _memory_db()
    info = conn.execute("PRAGMA table_info(dead_letter_deltas)").fetchall()
    columns = [row[1] for row in info]
    assert columns == [
        "seq",
        "request_id",
        "context_version",
        "snapshot_version",
        "failure_reason",
        "failed_at",
    ], columns
    seq_row = next(row for row in info if row[1] == "seq")
    assert seq_row[5] == 1, seq_row  # pk flag
    conn.execute(
        "INSERT INTO dead_letter_deltas (request_id, context_version,"
        " snapshot_version, failure_reason, failed_at)"
        " VALUES ('r1', 4, 5, 'cas-mismatch', '2026-01-01T00:00:00')"
    )
    seq_value = conn.execute("SELECT seq FROM dead_letter_deltas").fetchone()[0]
    assert seq_value == 1
    conn.close()


def test_s8_schema_module_is_import_free() -> None:
    """Gate-parallel precondition: zero import statements in schema.py."""
    source = Path(
        Path(__file__).resolve().parents[2] / "apps/adapters/outbound/schema.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    import_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert import_nodes == [], import_nodes


def test_s9_stimulus_ledger_has_pinned_columns() -> None:
    """ADR-009 S5: the rate-limit window carrier; delta is REAL (capped value)."""
    conn = _memory_db()
    info = conn.execute("PRAGMA table_info(stimulus_ledger)").fetchall()
    columns = [row[1] for row in info]
    assert columns == ["scope_id", "dimension", "at", "delta"], columns
    delta_row = next(row for row in info if row[1] == "delta")
    assert delta_row[2].upper() == "REAL", delta_row  # type column
    assert delta_row[3] == 1, delta_row  # notnull flag
    conn.close()
