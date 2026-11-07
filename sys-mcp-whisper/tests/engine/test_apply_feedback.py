"""apply_feedback: out-of-range deltas must raise, never clamp.

The ValidationError escape is the whole point (card 1.4): TensionState's pydantic
bounds are the single clamp authority, and a silent clamp here would corrupt the
event-sourced feedback trail.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.core.domain.global_context import TensionState
from packages.core.engine.reconciliation import apply_feedback

AT = datetime(2026, 1, 1, tzinfo=UTC)


def test_normal_delta_accumulates() -> None:
    t = TensionState(value=50.0)
    out = apply_feedback(t, at=AT, delta=10.0)
    assert out.value == 60.0


def test_last_feedback_at_is_set_from_at() -> None:
    out = apply_feedback(TensionState(value=50.0), at=AT, delta=1.0)
    assert out.last_feedback_at == AT


def test_input_tension_state_is_not_mutated() -> None:
    t = TensionState(value=50.0)
    apply_feedback(t, at=AT, delta=10.0)
    assert t.value == 50.0
    assert t.last_feedback_at is None


@pytest.mark.parametrize("delta", [60.0, 100.0, -60.0])
def test_out_of_range_raises_validation_error(delta: float) -> None:
    with pytest.raises(ValidationError):
        apply_feedback(TensionState(value=50.0), at=AT, delta=delta)


def test_boundary_deltas_are_exact() -> None:
    up = apply_feedback(TensionState(value=50.0), at=AT, delta=50.0)
    down = apply_feedback(TensionState(value=50.0), at=AT, delta=-50.0)
    assert up.value == 100.0
    assert down.value == 0.0


def test_returns_a_new_tension_state_instance() -> None:
    t = TensionState(value=50.0)
    out = apply_feedback(t, at=AT, delta=1.0)
    assert out is not t
