"""Inbound DTOs: the MCP wire shape, kept outside packages/core (ADR-009 R3/R5, U5).

The wire speaks this shape; ``packages.core`` only ever sees the domain
models.  Both mappings below are field-by-field: an explicit line per field
keeps "what may enter core" readable and diffable, where ``**dto.model_dump()``
would silently leak future DTO fields into the domain (R3: protocol fields
must not penetrate core).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.core.domain.contracts import ActionIntent, MCPRequest, MCPRequestContext, MCPResponse
from packages.core.domain.state import S1Snapshot


class MCPRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    user_id: str
    scope_id: str
    message: str
    persona_card: str  # P3-② human ruling: plain str; struct parsing stays inside the Adapter
    chat_mode: Literal["pure", "roleplay"]
    last_interaction_at: datetime  # flattened from MCPRequestContext
    current_time: datetime  # flattened from MCPRequestContext
    # 8 fields total (blueprint :327 said "9 fields" -- a typo, per task card section 0-4)


def to_domain(dto: MCPRequestDTO) -> MCPRequest:
    """Field-by-field mapping; the two flat times reassemble into the context."""
    return MCPRequest(
        request_id=dto.request_id,
        user_id=dto.user_id,
        scope_id=dto.scope_id,
        message=dto.message,
        persona_card=dto.persona_card,
        chat_mode=dto.chat_mode,
        context=MCPRequestContext(
            last_interaction_at=dto.last_interaction_at,
            current_time=dto.current_time,
        ),
    )


class MCPResponseDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: Literal["success"]
    system_head: str
    tail_message: str
    state_snapshot: S1Snapshot
    action_intent: ActionIntent
    typing_delay_ms: int = Field(default=0, ge=0, le=5000, strict=True)
    # 7 fields, mirrors MCPResponse


def from_domain(response: MCPResponse) -> MCPResponseDTO:
    """Field-by-field mapping; ``typing_delay_ms`` is passed explicitly."""
    return MCPResponseDTO(
        request_id=response.request_id,
        status=response.status,
        system_head=response.system_head,
        tail_message=response.tail_message,
        state_snapshot=response.state_snapshot,
        action_intent=response.action_intent,
        typing_delay_ms=response.typing_delay_ms,
    )
