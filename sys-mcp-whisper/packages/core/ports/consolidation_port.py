"""Offline memory consolidation port (dream / rumination).

P4 implements this.  ``offline_dream`` is dream-style recombination of the
event set; ``replay_rumination`` is rumination-style replay.  Both return
``None`` on purpose: the real output shape (what consolidation leaves behind,
whether it mutates traits, whether it writes memories) is a P4 decision, and
declaring a return type now would invent a contract ahead of its design.

``Sequence`` rather than ``list`` so an implementation may be handed a tuple,
a read-only view, or a generator already materialised by the caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..domain.contracts import EmotionalEvent


class ConsolidationPort(Protocol):
    """Memory consolidation boundary; no implementation in P2.5."""

    def offline_dream(self, events: Sequence[EmotionalEvent]) -> None: ...

    def replay_rumination(self, events: Sequence[EmotionalEvent]) -> None: ...
