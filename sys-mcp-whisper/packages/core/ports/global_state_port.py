"""Global state persistence contract (plan-p3-orchestration.md section 4, P3-C).

Declarations only: no implementation ships here.  Three decisions are pinned by
the type shape itself, so any later implementer inherits them structurally:

1. A single ``get(tx, scope_id)`` returns a *pair* ``(snapshot, context)``
   instead of two separate ``get_snapshot`` / ``get_context`` methods.  Both
   carriers must be read from one consistent view; splitting the reads would
   reopen the door to a torn combination such as snapshot v5 paired with
   global-context v4.

   ``scope_id`` is part of the read key, not a convenience: the store holds one
   snapshot row per dual-track scope, so a scope-less read is a wrong-row read
   the moment a second conversation exists.  It was optional-shaped once and
   that cost a silent cross-scope rewrite (2026-11-07 audit); the parameter is
   required so the mistake cannot be spelled again.
2. Both ``get`` and ``save`` take ``tx`` as their first parameter.  There is no
   tx-less form in this Protocol -- the ban on implicit connections is
   expressed by the type signature, not by a comment.
3. ``save`` carries an ``expected_*_version`` for each carrier; if either
   compare-and-swap check fails it returns ``False`` and the *caller* rolls
   back and retries.  The port never commits and never retries on its own.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.global_context import GlobalContext
from ..domain.state import S1Snapshot


class Transaction(Protocol):
    """Minimal transactional boundary handed to every port call."""

    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class GlobalStatePort(Protocol):
    """Read-modify-write port over both persisted carriers, always via tx."""

    def get(
        self, tx: Transaction, scope_id: str
    ) -> tuple[S1Snapshot | None, GlobalContext | None]: ...
    def save(
        self,
        tx: Transaction,
        snapshot: S1Snapshot,
        expected_snapshot_version: int,
        context: GlobalContext,
        expected_context_version: int,
    ) -> bool: ...
