"""Stimulus ledger: the persistence carrier for business rate limiting.

ADR-009 section S5 asks three questions and this module answers two of them
with one mechanism: the 2h window and the daily accumulator are both *sums
over the same rows*, taken at different lower bounds.  Two carriers would
mean two truths; one carrier means the window and the day can never disagree.

Time is normalised to naive UTC before it ever reaches SQL: SQLite compares
ISO-8601 text lexicographically, which only works when every row shares one
flavour of timestamp.

This module stays import-free of ``packages.core.engine`` on purpose: it
lives under ``apps/adapters/`` and the P3-GATE2 boundary forbids adapters
from importing the engine (see scripts/check_imports.py).  It therefore
returns raw ``(datetime, delta)`` rows; the composition layer owns the
conversion into ``dynamics.StimulusRecord`` because it is the only layer
allowed to touch engine types.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: The 2h rate-limit window length is duplicated here as a literal because
#: importing it would mean importing ``packages.core.engine.dynamics``.
#: The composition layer's tests (test_rate_limit.py) pin both constants to
#: the same value, so a divergence turns red instead of drifting silently.
TWO_HOUR_WINDOW: timedelta = timedelta(hours=2)

WINDOW_SQL = (
    "SELECT at, delta FROM stimulus_ledger"
    " WHERE scope_id = ? AND dimension = ? AND at > ? AND at <= ?"
    " ORDER BY at"
)

DAY_ACC_SQL = (
    "SELECT"
    " COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0.0),"
    " COALESCE(SUM(CASE WHEN delta < 0 THEN delta END), 0.0)"
    " FROM stimulus_ledger"
    " WHERE scope_id = ? AND dimension = ? AND at >= ? AND at <= ?"
)

APPEND_SQL = (
    "INSERT INTO stimulus_ledger (scope_id, dimension, at, delta)"
    " VALUES (?, ?, ?, ?)"
)

PRUNE_SQL = "DELETE FROM stimulus_ledger WHERE scope_id = ? AND at < ?"


def to_utc_naive(moment: datetime) -> datetime:
    """Naive input is treated as UTC; aware input converts to UTC then strips."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def day_start_utc(now: datetime) -> datetime:
    """Natural-day start taken in the caller's own timezone, then to naive UTC.

    "The caller's own timezone" means the tzinfo attached to ``now`` (S5-②:
    a naive ``now`` is treated as UTC).  So the day boundary is computed on
    the *aware* value first and only then normalised to naive UTC -- e.g.
    2026-11-07 01:00 +08:00 has its day start at 2026-11-06T16:00:00Z.
    """
    day_start_local = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    return to_utc_naive(day_start_local)


def window_start_utc(now: datetime) -> datetime:
    """Lower bound of the 2h sliding window: left-open, right-closed."""
    return to_utc_naive(now) - TWO_HOUR_WINDOW


def retention_cutoff_utc(now: datetime) -> datetime:
    """Rows older than min(day start, now - 2h) can no longer affect either.

    min, NOT day_start: right after midnight ``now - 2h`` falls into the
    previous day, and those rows still belong to the window.  Cutting at
    day_start would delete live window rows.
    """
    return min(day_start_utc(now), window_start_utc(now))


def make_record(row_at: str, delta: float) -> tuple[datetime, float]:
    """Parse one ledger row into ``(datetime, delta)``.

    Deviation note (task-FFW-P3-LIMIT section 1.1 sketched a
    ``dynamics.StimulusRecord`` return): adapters must not import the engine
    (P3-GATE2, scripts/check_imports.py), so the raw pair is returned here and
    the composition layer wraps it in ``dynamics.StimulusRecord``.
    """
    return datetime.fromisoformat(row_at), delta
