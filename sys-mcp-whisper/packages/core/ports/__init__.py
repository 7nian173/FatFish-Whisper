"""Port interface declarations for the core (P3 contract surface).

Each module here declares a boundary the engine needs but must not implement:
persistence, memory consolidation, observability and causal audit.  P2.5 ships
the declarations only -- implementations arrive in P3/P4.
"""

from __future__ import annotations

from .audit_port import AuditPort
from .consolidation_port import ConsolidationPort
from .global_state_port import GlobalStatePort, Transaction
from .observability_port import ObservabilityPort
from .state_port import StateRepositoryPort

__all__ = [
    "AuditPort",
    "ConsolidationPort",
    "GlobalStatePort",
    "ObservabilityPort",
    "StateRepositoryPort",
    "Transaction",
]
