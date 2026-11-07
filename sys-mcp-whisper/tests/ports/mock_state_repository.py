"""In-memory ``StateRepositoryPort`` for the P2.5 tests (task-FFW-P25-T4).

Deliberately lives under ``tests/`` rather than ``packages/core/ports/``: it is
not production code, and putting it next to the port would invite the P3 SQLite
implementation to sit beside a mock and eventually be imported by mistake.

Semantics (card section 3.4): the version is the concurrency token.  A writer
that passes a stale token is refused with ``False`` and the stored snapshot is
left untouched -- no silent overwrite, no automatic retry.
"""

from __future__ import annotations

from packages.core.domain.state import S1Snapshot


class MockStateRepository:
    """Single-slot repository with optimistic concurrency control."""

    def __init__(self) -> None:
        self._snapshot: S1Snapshot | None = None
        self._version: int = 0

    @property
    def version(self) -> int:
        """Current version token; the value a writer must echo back."""
        return self._version

    def load(self) -> S1Snapshot | None:
        return self._snapshot

    def save_if_version_match(self, snapshot: S1Snapshot, expected_version: int) -> bool:
        if expected_version != self._version:
            return False
        self._snapshot = snapshot
        self._version += 1
        return True
