"""Stimulus ledger helper tests (task-FFW-P3-LIMIT section 1.6, U1-U4).

These pin the time-normalisation semantics that the SQL side depends on:
SQLite compares ISO-8601 text lexicographically, so every helper must agree
on naive-UTC as the one canonical flavour, and the retention cutoff must be
``min(day_start, window_start)`` -- a hard ``day_start`` would delete live
window rows right after midnight (mutation M7's target).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apps.adapters.outbound.stimulus_ledger import (
    day_start_utc,
    retention_cutoff_utc,
    to_utc_naive,
)


def test_u1_to_utc_naive_passes_naive_through_and_converts_aware() -> None:
    naive = datetime(2026, 11, 6, 16, 30, 0)
    assert to_utc_naive(naive) == naive
    assert to_utc_naive(naive).tzinfo is None
    aware = datetime(2026, 11, 7, 0, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    converted = to_utc_naive(aware)
    assert converted == datetime(2026, 11, 6, 16, 30, 0)
    assert converted.tzinfo is None


def test_u2_day_start_uses_caller_own_timezone() -> None:
    # 2026-11-07 01:00 +08:00 == 2026-11-06 17:00 UTC -> day start is the
    # caller's local day (Nov 7 00:00 +08) expressed in naive UTC:
    # 2026-11-06T16:00:00.
    now = datetime(2026, 11, 7, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert day_start_utc(now) == datetime(2026, 11, 6, 16, 0, 0)


def test_u3_retention_cutoff_switches_between_window_and_day() -> None:
    # Early in the caller's day: now - 2h reaches back before day start,
    # so the window bound wins.
    early = datetime(2026, 11, 7, 0, 30, 0)
    assert retention_cutoff_utc(early) == early - timedelta(hours=2)
    # Mid-day: day start is older than the window bound, so it wins.
    mid = datetime(2026, 11, 7, 12, 0, 0)
    assert retention_cutoff_utc(mid) == datetime(2026, 11, 7, 0, 0, 0)


def test_u4_retention_cutoff_is_not_hardwired_to_day_start() -> None:
    # Mutation M7 (cutoff == day_start always) must turn red here: right
    # after midnight the cutoff must reach back into the previous day.
    just_after_midnight = datetime(2026, 11, 7, 0, 30, 0)
    assert retention_cutoff_utc(just_after_midnight) < datetime(2026, 11, 7, 0, 0, 0)
