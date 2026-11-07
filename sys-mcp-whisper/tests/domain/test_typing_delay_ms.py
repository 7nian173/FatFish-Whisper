"""``MCPResponse.typing_delay_ms`` -- P2.5 task-FFW-P25-T4 (contract 3.3).

Written before the field exists.  Seven criteria, each with both a positive and
a negative side so that an implementation which rejects everything (or accepts
everything) cannot pass.

The card warns that pydantic's ``int`` is lax by default and may coerce
``"100"`` to ``100``.  Measured on this machine (pydantic 2.14.1):

    '100'  -> 100        <-- lax, so ``strict=True`` is required
    True   -> 1          <-- also coerced on the lax path
    100.0  -> 100

With ``strict=True`` all three are rejected as ``int_type``.  Criterion 4 below
is the one that pins that decision.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.domain.contracts import (
    ActionIntent,
    MCPResponse,
)
from packages.core.domain.state import (
    IPCState,
    S1Snapshot,
    TraitState,
)

TS = "2026-11-06T09:00:00Z"


def make_snapshot() -> S1Snapshot:
    """Smallest snapshot that satisfies contract 3.2."""
    return S1Snapshot.model_validate(
        {
            "version": 1,
            "scope_id": "chat_8842",
            "timestamp": TS,
            "last_decay_at": TS,
            "is_conflict": False,
            "ipc": IPCState(
                affiliation=40.0,
                dominance=60.0,
                relationship_stage="friend",
                relationship_ceiling="lover",
            ),
            "traits": {"tsundere": TraitState(score=72, visibility="unlocked")},
            "emotions": {},
        }
    )


def make_response(**overrides: object) -> MCPResponse:
    payload: dict[str, object] = {
        "request_id": "req_1",
        "status": "success",
        "system_head": "seal text",
        "tail_message": "injection text",
        "state_snapshot": make_snapshot(),
        "action_intent": ActionIntent(emotion="annoyed", sticker_tag="angry"),
    }
    return MCPResponse(**{**payload, **overrides})


# 1. Default ---------------------------------------------------------------


def test_typing_delay_ms_defaults_to_zero() -> None:
    assert make_response().typing_delay_ms == 0
    # The declaration must carry the default too, otherwise every caller has to
    # repeat it and the field is effectively required.
    assert MCPResponse.model_fields["typing_delay_ms"].default == 0


# 2. Both bounds accepted --------------------------------------------------


def test_typing_delay_ms_accepts_both_bounds() -> None:
    assert make_response(typing_delay_ms=0).typing_delay_ms == 0
    assert make_response(typing_delay_ms=5000).typing_delay_ms == 5000


def test_typing_delay_ms_accepts_interior_values() -> None:
    for value in (1, 250, 4999):
        assert make_response(typing_delay_ms=value).typing_delay_ms == value


# 3. Out of range ----------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "expected_type"),
    [(-1, "greater_than_equal"), (5001, "less_than_equal")],
)
def test_typing_delay_ms_rejects_out_of_range(bad: int, expected_type: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_response(typing_delay_ms=bad)
    error = excinfo.value.errors()[0]
    assert error["loc"] == ("typing_delay_ms",)
    # Pin the *reason*: while the field is missing, ``extra_forbidden`` would
    # also land on this loc and let the test pass for the wrong cause.
    assert error["type"] == expected_type, error


# 4. String is not an int --------------------------------------------------


def test_typing_delay_ms_rejects_a_numeric_string() -> None:
    """Without ``strict=True`` pydantic coerces ``"100"`` to ``100``.

    A coercion here would mean a host sending a JSON string silently gets a
    different value class back, so the field is declared strict.
    """
    with pytest.raises(ValidationError) as excinfo:
        make_response(typing_delay_ms="100")
    error = excinfo.value.errors()[0]
    assert error["loc"] == ("typing_delay_ms",)
    assert error["type"] == "int_type", error


def test_typing_delay_ms_rejects_a_float() -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_response(typing_delay_ms=100.5)
    assert excinfo.value.errors()[0]["type"] == "int_type"


# 5. None is not an int ----------------------------------------------------


def test_typing_delay_ms_rejects_none() -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_response(typing_delay_ms=None)
    error = excinfo.value.errors()[0]
    assert error["loc"] == ("typing_delay_ms",)
    assert error["type"] == "int_type", error


# 6. extra="forbid" still holds -------------------------------------------


def test_typing_delay_ms_does_not_relax_extra_forbid() -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_response(typing_delay_ms2=100)
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


# 7. Reflected and ordered after action_intent ----------------------------


def test_typing_delay_ms_is_visible_to_reflection() -> None:
    assert "typing_delay_ms" in MCPResponse.model_fields
    fields = list(MCPResponse.model_fields)
    assert fields[-1] == "typing_delay_ms", fields
    assert fields.index("action_intent") < fields.index("typing_delay_ms")


def test_mcp_response_stays_mutable_and_forbidding() -> None:
    """The card pins ``extra="forbid", frozen=False`` -- do not drift."""
    config = MCPResponse.model_config
    assert config.get("extra") == "forbid"
    assert config.get("frozen") is False
