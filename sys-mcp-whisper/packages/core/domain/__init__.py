"""Public domain contracts: S1 snapshot, host request/response, proposals, config."""

from __future__ import annotations

from .config import (
    DEFAULT_ENGINE_CONFIG,
    DEFAULT_RULES,
    AssemblyConfig,
    ConflictConfig,
    ConflictRule,
    DynamicsConfig,
    EngineConfig,
    config_fingerprint,
)
from .contracts import (
    ActionIntent,
    CandidateProposal,
    EmotionalEvent,
    MCPRequest,
    MCPRequestContext,
    MCPResponse,
    ProposalItem,
    UserProfileRef,
)
from .exceptions import TimeDecayError
from .global_context import (
    DELTA_ELIGIBLE_PATHS,
    BaselineState,
    GlobalContext,
    GlobalDelta,
    PulseState,
    SuppressionDelta,
    TensionState,
)
from .state import EmotionValue, IPCState, S1Snapshot, TraitState

__all__ = [
    "DEFAULT_ENGINE_CONFIG",
    "DEFAULT_RULES",
    "DELTA_ELIGIBLE_PATHS",
    "ActionIntent",
    "AssemblyConfig",
    "BaselineState",
    "CandidateProposal",
    "ConflictConfig",
    "ConflictRule",
    "DynamicsConfig",
    "EmotionValue",
    "EmotionalEvent",
    "EngineConfig",
    "GlobalContext",
    "GlobalDelta",
    "IPCState",
    "MCPRequest",
    "MCPRequestContext",
    "MCPResponse",
    "ProposalItem",
    "PulseState",
    "S1Snapshot",
    "SuppressionDelta",
    "TensionState",
    "TimeDecayError",
    "TraitState",
    "UserProfileRef",
    "config_fingerprint",
]
