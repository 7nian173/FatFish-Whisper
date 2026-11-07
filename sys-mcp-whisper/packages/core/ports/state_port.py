"""State persistence port (``StateRepositoryPort``).

This is the planned landing point for P3's state repository.  P2.5 ships only
the port plus an in-memory mock under ``tests/ports/``; the real SQLite
implementation belongs to P3 and must not be added here.

The write path is optimistically locked: the caller passes the version token it
read, and a mismatch means the write is refused.  There is no silent overwrite
and no automatic retry -- retry policy belongs to the caller, which is the only
party that knows whether re-reading and re-applying the mutation is safe.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.state import S1Snapshot


class StateRepositoryPort(Protocol):
    """Persistence boundary for the S1 snapshot.

    Implementations are expected to be single-writer-safe by comparing
    ``expected_version`` against the stored token inside the same critical
    section that performs the write.
    """

    def save_if_version_match(self, snapshot: S1Snapshot, expected_version: int) -> bool: ...

    def load(self) -> S1Snapshot | None: ...
