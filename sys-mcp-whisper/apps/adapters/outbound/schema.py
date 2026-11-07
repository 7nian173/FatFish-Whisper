"""SQLite table definitions for the global state store (plan-p3 section 6).

Deliberately import-free string constants only: ``apps/`` cannot import
``packages.*`` yet (gate extension is a separate task), and this module must
stay loadable in isolation.  No connection logic here -- the store task owns
that; this file only pins the table shapes.

Each constant carries one persisted carrier and the iron rule it enforces.
"""

# global_context: the single-row global carrier -- ``id = 1`` is the only
# legal row (CHECK id = 1), and ``version`` is the optimistic-lock credential
# compared against ``expected_context_version`` by GlobalStatePort.save (R1).
GLOBAL_CONTEXT_DDL = """
CREATE TABLE IF NOT EXISTS global_context (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL CHECK (version >= 0),
    payload TEXT    NOT NULL
)
"""

# s1_snapshot: one row per dual-track scope -- ``scope_id`` is the R3 dual-track
# isolation key and matches MCPRequest.scope_id exactly; ``version`` is the
# snapshot-side optimistic-lock credential (expected_snapshot_version).
S1_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS s1_snapshot (
    scope_id TEXT    PRIMARY KEY,
    version  INTEGER NOT NULL CHECK (version >= 0),
    payload  TEXT    NOT NULL
)
"""

# dead_letter_deltas: local dead-letter table per U1 final ruling (R1-3) --
# five business columns plus the AUTOINCREMENT sequence number.  ``failed_at``
# records the clamped ``now`` passed in by the caller; nothing here reads the
# system clock.
DEAD_LETTER_DDL = """
CREATE TABLE IF NOT EXISTS dead_letter_deltas (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       TEXT    NOT NULL,
    context_version  INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    failure_reason   TEXT    NOT NULL,
    failed_at        TEXT    NOT NULL
)
"""

# stimulus_ledger: one row per applied stimulus unit, per scope and per
# hormone dimension.  It carries the business rate-limit window (ADR-009 S5):
# the 2h window and the daily accumulator are both SUMs over these rows with
# different lower bounds, so the two can never disagree.  ``delta`` stores the
# value *after* capping -- the ledger records what actually took effect, not
# what was requested; recording the request would make the window fill twice
# as fast as reality.  Rows older than min(day start, now - 2h) are pruned by
# the store inside the same transaction.  No index: per-scope per-dimension
# daily row counts are single digits to a few dozen, and a CREATE INDEX
# statement would break the "every ALL_DDL entry is a CREATE TABLE" lock.
STIMULUS_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS stimulus_ledger (
    scope_id  TEXT NOT NULL,
    dimension TEXT NOT NULL,
    at        TEXT NOT NULL,
    delta     REAL NOT NULL
)
"""

ALL_DDL: tuple[str, ...] = (
    GLOBAL_CONTEXT_DDL,
    S1_SNAPSHOT_DDL,
    DEAD_LETTER_DDL,
    STIMULUS_LEDGER_DDL,
)
