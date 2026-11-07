"""Observability port for background agent runs and token accounting.

Every background agent run, model debate and token spend is expected to leave
the process through this interface so the Admin panel can display it.  The
payloads are ``Mapping`` rather than ``dict`` on purpose: the interface must not
dictate a concrete container, and a caller may well hand over a frozen mapping
or a read-only proxy.

``check_token_budget`` is the only method here that returns a value.  ``True``
means the estimate is inside the budget, ``False`` means it is over the
threshold.  What the caller does with a ``False`` -- refuse the call, degrade to
the cheap path, queue it -- is a P4 orchestration decision; this interface does
not decide it, and the circuit-breaker threshold itself is not defined here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class ObservabilityPort(Protocol):
    """Telemetry boundary; no implementation in P2.5."""

    def emit_agent_run(self, payload: Mapping[str, object]) -> None: ...

    def emit_token_usage(self, payload: Mapping[str, object]) -> None: ...

    def check_token_budget(self, estimated_tokens: int) -> bool: ...
