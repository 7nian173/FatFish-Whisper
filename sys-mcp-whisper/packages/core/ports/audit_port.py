"""Causal audit port (counterfactual replay).

The audit must be a pure replay over historical events: no clock read, no
environment access, no writes to any persistence layer.  That constraint is what
makes the result reproducible -- an audit that consulted "now" would answer a
different question each time it ran.

``candidate_param`` carries the hypothesised parameter value, which is what
makes the replay counterfactual: the same events are re-evaluated under a value
that never actually applied.  The return type is ``Mapping`` rather than a bool
or a dataclass because the shape of an audit verdict -- what counts as a
divergence, how much detail to carry -- is a P4 decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from ..domain.contracts import EmotionalEvent


class AuditPort(Protocol):
    """Counterfactual audit boundary; no implementation in P2.5."""

    def counterfactual_audit(
        self,
        events: Sequence[EmotionalEvent],
        candidate_param: Mapping[str, object],
    ) -> Mapping[str, object]: ...
