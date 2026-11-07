"""AsyncReconciliationQueue contract locks.

The queue is a pure dataclass (no IO, no threads): the failure path is that the
caller requeues a drained PendingDelta after its delivery attempt failed; the
queue alone bumps attempts.  Overflow must land in dropped() -- silent loss is
the bug this file exists to prevent.
"""

from __future__ import annotations

from packages.core.domain.config import PLACEHOLDER_MAX_QUEUE_RETRIES
from packages.core.domain.global_context import GlobalDelta
from packages.core.engine.reconciliation import (
    AsyncReconciliationQueue,
    PendingDelta,
)


def _delta(tension: float = 1.0) -> GlobalDelta:
    return GlobalDelta(pulse={}, tension=tension)


def test_fresh_queue_is_empty() -> None:
    q = AsyncReconciliationQueue()
    assert q.pending_count() == 0
    assert q.drain() == []
    assert q.dropped() == ()


def test_enqueue_then_drain_round_trip() -> None:
    q = AsyncReconciliationQueue()
    q.enqueue(_delta(), expected_version=7)
    assert q.pending_count() == 1
    batch = q.drain()
    assert len(batch) == 1
    assert batch[0].delta.tension == 1.0
    assert batch[0].expected_version == 7
    assert batch[0].attempts == 0
    assert q.pending_count() == 0


def test_drain_empties_entire_queue() -> None:
    q = AsyncReconciliationQueue()
    for i in range(3):
        q.enqueue(_delta(tension=float(i)), expected_version=i)
    assert len(q.drain()) == 3
    assert q.pending_count() == 0


def test_default_max_retries_is_the_placeholder() -> None:
    # NOT `is PLACEHOLDER_...`: CPython small-int caching would let a literal 3
    # pass identity too.  Source inspection is the only mutation-proof check
    # that the default references the constant instead of inlining its value.
    import inspect

    source = inspect.getsource(AsyncReconciliationQueue)
    assert "max_retries: int = PLACEHOLDER_MAX_QUEUE_RETRIES" in source


def test_overflow_goes_to_dropped_with_final_attempts() -> None:
    q = AsyncReconciliationQueue()
    q.enqueue(_delta(), expected_version=1)
    current = q.drain()[0]
    for _ in range(PLACEHOLDER_MAX_QUEUE_RETRIES + 1):
        q.requeue(current)
        # A real consumer drains again before its next apply attempt; the
        # queue hands back only items still within budget.
        batch = q.drain()
        current = batch[0] if batch else current
    assert len(q.dropped()) == 1
    assert q.dropped()[0].attempts == PLACEHOLDER_MAX_QUEUE_RETRIES + 1
    assert q.pending_count() == 0
    # The dropped receipt is the audit trail: final attempts, original payload.
    assert q.dropped()[0].delta == _delta()
    assert q.dropped()[0].expected_version == 1


def test_requeue_within_limit_stays_pending() -> None:
    # Realistic retry loop: drain hands the item out, a failed apply requeues
    # it, the next drain sees attempts bumped.  (requeue does not de-duplicate
    # by identity -- requeuing the same object twice without draining appends
    # it twice, which no real caller does.)
    q = AsyncReconciliationQueue()
    q.enqueue(_delta(), expected_version=1)
    p = q.drain()[0]
    q.requeue(p)
    batch = q.drain()
    assert len(batch) == 1
    assert batch[0].attempts == 1
    assert q.pending_count() == 0
    assert q.dropped() == ()
    q.requeue(batch[0])
    assert q.drain()[0].attempts == 2


def test_each_pendingdelta_tracks_its_own_attempts() -> None:
    q = AsyncReconciliationQueue()
    q.enqueue(_delta(), expected_version=1)
    q.enqueue(_delta(), expected_version=2)
    first, second = q.drain()
    q.requeue(first)
    assert q.drain()[0].attempts == 1 and second.attempts == 0


def test_pending_delta_is_a_plain_frozen_dataclass() -> None:
    import dataclasses

    assert dataclasses.is_dataclass(PendingDelta)
    fields = {f.name: f for f in dataclasses.fields(PendingDelta)}
    assert set(fields) == {"delta", "expected_version", "attempts"}
    assert fields["attempts"].default == 0
