"""Asynchronous reconciliation queue and tension feedback (task-FFW-P28-C3).

Unlike ``heat.py`` (pure arithmetic), this module is *stateful*: the queue
holds pending deltas between the drain calls, and the feedback function owns
an explicit call-supplied instant because the engine never reads a clock.
Nothing here persists -- durability is a port boundary (P3 will add
``ReconciliationQueuePort``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..domain.config import PLACEHOLDER_MAX_QUEUE_RETRIES
from ..domain.global_context import GlobalDelta, TensionState


@dataclass
class PendingDelta:
    """One queued global increment plus the version it was built against."""

    delta: GlobalDelta
    expected_version: int
    attempts: int = 0


@dataclass
class AsyncReconciliationQueue:
    """In-memory queue of pending global deltas awaiting reconciliation.

    Retry contract: ``drain()`` hands out every pending item and clears the
    queue.  A consumer that fails to apply an item re-queues it via
    ``requeue(pending)`` (chosen over overloading ``enqueue`` so the public
    API distinguishes first-submission from retry and ``attempts`` is bumped
    in exactly one place).  An item whose ``attempts`` exceed ``max_retries``
    is moved to ``dropped`` -- never silently discarded (ADR-008 section 3.2,
    no silent degradation: the dropped tuple is the audit trail).
    """

    max_retries: int = PLACEHOLDER_MAX_QUEUE_RETRIES
    _pending: list[PendingDelta] = field(default_factory=list)
    _dropped: list[PendingDelta] = field(default_factory=list)

    def enqueue(self, delta: GlobalDelta, expected_version: int) -> None:
        self._pending.append(PendingDelta(delta=delta, expected_version=expected_version))

    def requeue(self, pending: PendingDelta) -> None:
        """Put a failed item back with its attempt count bumped; drop if over budget."""
        pending.attempts += 1
        if pending.attempts > self.max_retries:
            self._dropped.append(pending)
        else:
            self._pending.append(pending)

    def drain(self) -> list[PendingDelta]:
        pending, self._pending = self._pending, []
        return pending

    def pending_count(self) -> int:
        return len(self._pending)

    def dropped(self) -> tuple[PendingDelta, ...]:
        return tuple(self._dropped)


def apply_feedback(
    tension: TensionState, *, at: datetime, delta: float
) -> TensionState:
    """Fold one feedback increment into the tension state.

    Returns a NEW object (the input is never mutated); ``last_feedback_at`` is
    the caller-injected ``at``.  ``value + delta`` must land in ``[0, 100]``:
    out-of-range input raises the model's own ``ValidationError`` -- this
    function never clamps, keeping ``TensionState`` the single source of truth
    for the legal range.
    """
    # model_copy(update=...) would bypass field validation entirely (a 50+60
    # delta would silently produce value=110), so the updated payload is
    # round-tripped through model_validate to make TensionState's bounds the
    # single clamp authority that actually fires.
    return TensionState.model_validate(
        tension.model_dump() | {"value": tension.value + delta, "last_feedback_at": at}
    )
