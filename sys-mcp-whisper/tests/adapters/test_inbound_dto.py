"""Contract locks for the inbound DTO pair (task-FFW-P3-DTO).

U5 final ruling: the DTOs live only in apps/adapters/inbound -- nothing in
packages/ may know them.  m14781 hard requirement: system_head and
tail_message stay two independent required fields; prompt_injection was
deleted by U6 and must not return.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.adapters.inbound.dto import (
    MCPRequestDTO,
    MCPResponseDTO,
    from_domain,
    to_domain,
)
from packages.core.domain.contracts import ActionIntent, MCPResponse
from packages.core.domain.state import IPCState, S1Snapshot, TraitState

# Exact field names AND order (blueprint :327 said "9 fields" -- typo, real count 8).
REQUEST_DTO_FIELDS = (
    "request_id",
    "user_id",
    "scope_id",
    "message",
    "persona_card",
    "chat_mode",
    "last_interaction_at",
    "current_time",
)
RESPONSE_DTO_FIELDS = (
    "request_id",
    "status",
    "system_head",
    "tail_message",
    "state_snapshot",
    "action_intent",
    "typing_delay_ms",
)

_NOW = datetime(2026, 11, 7, 12, 0, 0)


def _snapshot() -> S1Snapshot:
    return S1Snapshot(
        version=1,
        scope_id="s1",
        timestamp=_NOW,
        last_decay_at=_NOW,
        is_conflict=False,
        ipc=IPCState(
            affiliation=0.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        emotions={},
    )


def _request_payload() -> dict:
    return {
        "request_id": "uuid-probe-1",
        "user_id": "u-1",
        "scope_id": "s1",
        "message": "hello there",
        "persona_card": "tsundere",
        "chat_mode": "pure",
        "last_interaction_at": _NOW - timedelta(minutes=5),
        "current_time": _NOW,
    }


def _response() -> MCPResponse:
    return MCPResponse(
        request_id="uuid-probe-1",
        status="success",
        system_head="head-text",
        tail_message="tail-text",
        state_snapshot=_snapshot(),
        action_intent=ActionIntent(emotion="calm", sticker_tag="ok"),
        typing_delay_ms=1234,
    )


def test_t1_request_dto_has_exactly_eight_fields_in_order() -> None:
    assert tuple(MCPRequestDTO.model_fields) == REQUEST_DTO_FIELDS


def test_t2_response_dto_has_exactly_seven_fields_in_order() -> None:
    assert tuple(MCPResponseDTO.model_fields) == RESPONSE_DTO_FIELDS


def test_t3_extra_key_is_rejected_as_extra_forbidden() -> None:
    """Blueprint :333 wanted 'physical discard' but extra='forbid' raises; the
    captain ruled fail-closed: reject at the boundary instead of dropping.  A
    dropped key would let the host believe the field was processed."""
    with pytest.raises(ValidationError) as exc_info:
        MCPRequestDTO(**_request_payload(), x_secret_header="v")
    assert any(e["type"] == "extra_forbidden" for e in exc_info.value.errors())


def test_t3b_domain_key_set_is_exact_after_to_domain() -> None:
    """Second half of the fail-closed ruling: to_domain leaks nothing extra."""
    dto = MCPRequestDTO(**_request_payload())
    request = to_domain(dto)
    assert set(request.model_dump()) == {
        "request_id",
        "user_id",
        "scope_id",
        "message",
        "persona_card",
        "chat_mode",
        "context",
    }
    assert set(request.model_dump()["context"]) == {"last_interaction_at", "current_time"}


def test_t4_to_domain_round_trips_every_field() -> None:
    payload = _request_payload()
    request = to_domain(MCPRequestDTO(**payload))
    assert request.request_id == payload["request_id"]
    assert request.user_id == payload["user_id"]
    assert request.scope_id == payload["scope_id"]
    assert request.message == payload["message"]
    assert request.persona_card == payload["persona_card"]
    assert request.chat_mode == payload["chat_mode"]
    assert request.context.last_interaction_at == payload["last_interaction_at"]
    assert request.context.current_time == payload["current_time"]


def test_t5_from_domain_round_trips_every_field_with_non_default_delay() -> None:
    response = _response()
    dto = from_domain(response)
    assert dto.request_id == response.request_id
    assert dto.status == response.status
    assert dto.system_head == response.system_head
    assert dto.tail_message == response.tail_message
    assert dto.state_snapshot == response.state_snapshot
    assert dto.action_intent == response.action_intent
    assert dto.typing_delay_ms == 1234
    assert dto.typing_delay_ms == response.typing_delay_ms


def test_t6_chat_mode_is_strictly_literal() -> None:
    payload = _request_payload()
    assert MCPRequestDTO(**{**payload, "chat_mode": "roleplay"}).chat_mode == "roleplay"
    for bad in ("ROLEPLAY", "shout"):
        with pytest.raises(ValidationError):
            MCPRequestDTO(**{**payload, "chat_mode": bad})


def test_t7_typing_delay_bounds_and_strict_int() -> None:
    response = _response()
    assert MCPResponseDTO.model_fields["typing_delay_ms"].default == 0
    base = {
        "request_id": response.request_id,
        "status": response.status,
        "system_head": response.system_head,
        "tail_message": response.tail_message,
        "state_snapshot": response.state_snapshot,
        "action_intent": response.action_intent,
    }
    for bad in (-1, 5001, "100"):
        with pytest.raises(ValidationError):
            MCPResponseDTO(**base, typing_delay_ms=bad)
    assert MCPResponseDTO(**base, typing_delay_ms=5000).typing_delay_ms == 5000


def test_t8_dtos_never_appear_in_packages() -> None:
    """T-B1 as corrected by the captain: scan packages/ only -- the new test
    file itself necessarily contains both class names, so tests/ can never be
    zero-hit (blueprint :438 double-scope was unsatisfiable).

    Rewritten by task-FFW-P3-T8FIX.  The previous body shelled out to
    ``git grep`` anchored at ``parents[1]`` (``tests/``), which resolves to the
    non-existent ``tests/packages`` and returns rc=1 with empty stdout -- so
    it passed vacuously and never once looked at ``packages/``.  The research
    checkout has no git repository at all (constitution 12.7), which is what
    finally surfaced it: every run returned 128.
    """
    package_root = Path(__file__).resolve().parents[2] / "packages"
    pattern = re.compile(r"MCPRequestDTO|MCPResponseDTO")
    scanned_files = sorted(package_root.rglob("*.py"))
    # Non-vacuum guard: a wrong anchor or a narrower glob would otherwise scan
    # nothing at all and still report green.
    assert len(scanned_files) > 0, f"no .py files under {package_root}"
    # Captain measured 28 .py files under packages/ on 2026-11-08.  The lower
    # bound of 20 leaves room for deletions while still catching an anchor
    # that lands on some tiny unrelated directory.
    assert len(scanned_files) >= 20, (
        f"scan surface collapsed: {len(scanned_files)} files under {package_root}"
    )
    hits = [path for path in scanned_files if pattern.search(path.read_text(encoding="utf-8"))]
    assert hits == [], f"DTO names leaked into packages/: {[str(path) for path in hits]}"


def test_t9_request_id_is_stable_across_the_whole_chain() -> None:
    payload = _request_payload()
    dto = MCPRequestDTO(**payload)
    domain_request = to_domain(dto)
    response = _response()
    back = from_domain(response)
    assert dto.request_id == domain_request.request_id == back.request_id == "uuid-probe-1"


def test_t10_head_and_tail_are_two_independent_fields() -> None:
    """m14781: system_head/tail_message must never collapse into prompt_injection."""
    assert not hasattr(MCPResponseDTO, "prompt_injection")
    assert "prompt_injection" not in MCPResponseDTO.model_fields
    head_info = MCPResponseDTO.model_fields["system_head"]
    tail_info = MCPResponseDTO.model_fields["tail_message"]
    assert head_info is not tail_info
    response = _response().model_copy(update={"system_head": "A", "tail_message": "B"})
    dto = from_domain(response)
    assert dto.system_head == "A" and dto.tail_message == "B"


def test_t11_dto_never_imports_the_engine() -> None:
    """Static self-proof: no ImportFrom targets packages.core.engine."""
    source = Path(__file__).resolve().parents[2].joinpath(
        "apps", "adapters", "inbound", "dto.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("packages.core.engine"), node.module
