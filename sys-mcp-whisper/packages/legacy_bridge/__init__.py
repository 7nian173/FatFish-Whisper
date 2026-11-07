"""Anti-corruption layer for data crossing from the frozen legacy stack.

The legacy packages under ``40_冻结依赖库/`` are frozen "template A, keep the
defects": they stay byte-identical, so every defect they carry is intercepted on
this side of the boundary instead.  :mod:`packages.legacy_bridge.guards` holds
the criteria from ``P0-防腐层要求.md`` as pure functions.

Nothing here imports the legacy packages and nothing performs IO.  Wiring these
guards into a live adapter is P3-02's task.
"""

from __future__ import annotations

from .guards import (
    REQUIRED_PAYLOAD_KEYS,
    assemble_payload,
    guard_xff_chain,
    normalize_message_id,
    require_event_key,
    require_user_id,
    sanitize_error_body,
    validate_core_extra_hosts,
)

__all__ = [
    "REQUIRED_PAYLOAD_KEYS",
    "assemble_payload",
    "guard_xff_chain",
    "normalize_message_id",
    "require_event_key",
    "require_user_id",
    "sanitize_error_body",
    "validate_core_extra_hosts",
]
