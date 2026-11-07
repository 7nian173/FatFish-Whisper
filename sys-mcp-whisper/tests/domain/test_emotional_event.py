"""Contract tests for EmotionalEvent (plan-p0-contracts.md section 5, G5).

EmotionalEvent is the append-only record of an emotion that already happened, so
the whole file is written before the model exists: the first run must fail at
collection (ImportError) and only turns green once ``EmotionalEvent`` is
exported by ``packages.core.domain.contracts``.

Scope is structural only. The emotion vocabulary belongs to P3, so nothing here
constrains which ``emotion_key`` values are legal, and ``summary`` whitespace is
deliberately not stripped (see the card's known-gap section).

``pick_recent_high_medium`` at the bottom is a *preview* of the P3-04 retrieval
behaviour, not a delivery: it lives in the test file on purpose.  Putting it in
``packages/`` would invent a retrieval layer that the plan has not placed in the
domain yet.  Its only job is to show that the nine fields can support the four
consumption requirements without adding a tenth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from packages.core.domain.contracts import EmotionalEvent
from packages.core.domain.state import S1Snapshot
from tests.domain.test_contracts import _DOMAIN_MODELS

CREATED_AT = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

REQUIRED_FIELDS = (
    "id",
    "emotion_key",
    "intensity",
    "severity",
    "summary",
    "visibility_scope",
    "scope_id",
    "created_at",
)


def event_kwargs(**overrides: object) -> dict[str, object]:
    """All nine fields, so a single ``pop`` yields the missing-field case."""
    kwargs: dict[str, object] = {
        "id": "evt_0001",
        "emotion_key": "anger",
        "intensity": 72.5,
        "severity": "high",
        "summary": "被叫笨蛋时指尖发烫",
        "visibility_scope": "private",
        "scope_id": "chat_8842",
        "created_at": CREATED_AT,
        "raw_retained": False,
    }
    kwargs.update(overrides)
    return kwargs


def make_event(
    event_id: str,
    *,
    severity: str = "high",
    visibility_scope: str = "group",
    scope_id: str = "group_1:user_9527",
    created_at: datetime = CREATED_AT,
) -> EmotionalEvent:
    return EmotionalEvent(
        id=event_id,
        emotion_key="anger",
        intensity=50.0,
        severity=severity,
        summary="一句摘要",
        visibility_scope=visibility_scope,
        scope_id=scope_id,
        created_at=created_at,
    )


# --- AC-G5-1 -------------------------------------------------------------


def test_emotional_event_roundtrip() -> None:
    """All nine fields construct, and every required field is really required."""
    event = EmotionalEvent(**event_kwargs())

    assert event.id == "evt_0001"
    assert event.emotion_key == "anger"
    assert event.intensity == 72.5
    assert event.severity == "high"
    assert event.summary == "被叫笨蛋时指尖发烫"
    assert event.visibility_scope == "private"
    assert event.scope_id == "chat_8842"
    assert event.created_at == CREATED_AT
    assert event.raw_retained is False


@pytest.mark.parametrize("missing", REQUIRED_FIELDS)
def test_emotional_event_roundtrip_requires_each_field(missing: str) -> None:
    """Dropping any single required field must fail, not fall back to a default."""
    kwargs = event_kwargs()
    kwargs.pop(missing)

    # Reverse assertion: the same mapping minus nothing is valid, so the
    # rejection below is caused by the missing field and not by the fixture.
    assert EmotionalEvent(**event_kwargs()).id == "evt_0001"

    with pytest.raises(ValidationError) as excinfo:
        EmotionalEvent(**kwargs)
    assert [error["loc"] for error in excinfo.value.errors()] == [(missing,)]


# --- AC-G5-2 -------------------------------------------------------------


def test_emotional_event_no_raw_body() -> None:
    """`summary` is the only text field: no field may carry the raw body."""
    forbidden = {"content", "full_text", "raw", "raw_text"}

    assert not forbidden & set(EmotionalEvent.model_fields)
    # Reverse assertion: the summary field itself exists, so the check above is
    # not passing because the model has no fields at all.
    assert "summary" in EmotionalEvent.model_fields
    assert set(EmotionalEvent.model_fields) == {
        "id",
        "emotion_key",
        "intensity",
        "severity",
        "summary",
        "visibility_scope",
        "scope_id",
        "created_at",
        "raw_retained",
    }


# --- AC-G5-3 -------------------------------------------------------------


def test_emotional_event_summary_bounded() -> None:
    """Length 0 and 201 are rejected; 1 and 200 are accepted."""
    for bad in ("", "a" * 201):
        with pytest.raises(ValidationError) as excinfo:
            EmotionalEvent(**event_kwargs(summary=bad))
        assert [error["loc"] for error in excinfo.value.errors()] == [("summary",)]

    for good in ("a", "a" * 200):
        assert len(EmotionalEvent(**event_kwargs(summary=good)).summary) == len(good)

    # The 200 limit counts Unicode code points, not UTF-8 bytes: 200 CJK
    # characters are 600 bytes and must still be accepted.
    cjk = "字" * 200
    assert len(cjk.encode("utf-8")) == 600
    assert EmotionalEvent(**event_kwargs(summary=cjk)).summary == cjk


# --- AC-G5-4 -------------------------------------------------------------


def test_emotional_event_severity_enum() -> None:
    """`critical` is outside the enum; the three documented levels all work."""
    with pytest.raises(ValidationError) as excinfo:
        EmotionalEvent(**event_kwargs(severity="critical"))
    assert [error["loc"] for error in excinfo.value.errors()] == [("severity",)]

    for good in ("high", "medium", "low"):
        assert EmotionalEvent(**event_kwargs(severity=good)).severity == good


# --- AC-G5-5 -------------------------------------------------------------


def test_emotional_event_frozen() -> None:
    """The event is an append-only fact: assignment raises ValidationError."""
    event = EmotionalEvent(**event_kwargs())

    with pytest.raises(ValidationError):
        event.intensity = 1.0  # type: ignore[misc]
    assert event.intensity == 72.5


def test_only_the_two_immutable_models_are_frozen() -> None:
    """``EmotionalEvent`` is immutable, and *this* file's job is to say why.

    ``RoutingResult`` is frozen for its own reason (cross-layer DTO).  The full
    roster of frozen models is deliberately NOT duplicated here: it is locked in
    exactly one place, ``tests/domain/test_field_constraint_locks.py::
    test_frozen_config_is_locked_for_every_domain_model``, where ``model_config``
    is treated as a constraint alongside the four ``Field`` keys.  Two copies of
    the same roster would drift, and a drifted roster is worse than none --
    each copy would look authoritative.

    What this file still owns is the local claim (this one model is frozen) plus
    the behavioural proof further up (``test_emotional_event_frozen``).
    """
    assert EmotionalEvent.model_config.get("frozen") is True
    assert EmotionalEvent.model_config.get("extra") == "forbid"


# --- AC-G5-7 -------------------------------------------------------------


def test_emotional_event_visibility_scope_enum() -> None:
    """`public` is not part of the contract; private and group are."""
    with pytest.raises(ValidationError) as excinfo:
        EmotionalEvent(**event_kwargs(visibility_scope="public"))
    assert [error["loc"] for error in excinfo.value.errors()] == [("visibility_scope",)]

    for good in ("private", "group"):
        assert EmotionalEvent(**event_kwargs(visibility_scope=good)).visibility_scope == good


# --- AC-G5-8 -------------------------------------------------------------


def test_emotional_event_raw_retained_default() -> None:
    """The default is False, matching the absent-raw-body structure."""
    kwargs = event_kwargs()
    kwargs.pop("raw_retained")

    assert EmotionalEvent(**kwargs).raw_retained is False
    assert EmotionalEvent(**event_kwargs(raw_retained=True)).raw_retained is True


# --- created_at validator (supporting the ACs above) --------------------


def test_emotional_event_created_at_requires_timezone() -> None:
    """Naive timestamps are rejected; any aware offset is accepted."""
    with pytest.raises(ValidationError) as excinfo:
        EmotionalEvent(**event_kwargs(created_at=datetime(2026, 11, 5, 12, 0)))
    assert [error["loc"] for error in excinfo.value.errors()] == [("created_at",)]

    shifted = datetime(2026, 11, 5, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    assert EmotionalEvent(**event_kwargs(created_at=shifted)).created_at == shifted


# --- extra locks required by the card -----------------------------------


def test_emotional_event_is_discovered_by_domain_reflection() -> None:
    """The blanket extra=forbid lock must pick the new model up by itself.

    The discovery lives in ``tests/domain/test_contracts.py`` and is deliberately
    not given a hand-written roster, so reaching the model at all is the proof.
    The measured count went 12 -> 13 (the card's "11 -> 12" predates EmotionValue
    from task-FFW-P0-C3), then 13 -> 14 with RoutingResult from task-FFW-P0-C6;
    the assertion below states a floor rather than the exact number so that the
    next card adding a model does not have to edit this file, while duplicates
    and a shrinking roster still fail.
    """
    names = {model.__name__ for model in _DOMAIN_MODELS}

    assert "EmotionalEvent" in names
    assert len(_DOMAIN_MODELS) == len(names), "同一模型被重复计入反射集合"
    assert len(_DOMAIN_MODELS) >= 12
    assert sum(model.__name__ == "EmotionalEvent" for model in _DOMAIN_MODELS) == 1
    assert {model.__module__ for model in _DOMAIN_MODELS} == {
        "packages.core.domain.contracts",
        "packages.core.domain.state",
    }


def test_valid_paths_count_is_locked() -> None:
    """EmotionalEvent is a standalone model and must not enter the snapshot.

    The count was 12 after ADR-008 section 1.7 removed the hormone block from
    S1Snapshot, and 13 since P2.8-C3 added ``last_active_at``.  The assertion
    stays exact, so any further drift is still caught.  What this test guards
    is unchanged: EmotionalEvent must not contribute a path of its own.
    """
    paths = S1Snapshot.valid_paths()

    assert len(paths) == 13
    assert "emotions" in paths
    assert not hasattr(S1Snapshot, "emotional_event")
    assert "EMOTIONAL_EVENT" not in paths


# --- AC-G5-6: consumption preview (P3-04 behaviour, test-local) ----------


def pick_recent_high_medium(
    events: list[EmotionalEvent],
    *,
    viewer_scope_id: str,
    limit: int = 3,
) -> list[EmotionalEvent]:
    """Filter, order and truncate events the way P3-04 retrieval will.

    Preview only.  The real retrieval layer belongs to P3; this function exists
    to prove the nine fields are enough for the four consumption requirements.
    """
    visible = [
        event
        for event in events
        if event.severity in {"high", "medium"}
        and not (
            event.visibility_scope == "private" and event.scope_id != viewer_scope_id
        )
    ]
    visible.sort(key=lambda event: (-event.created_at.timestamp(), event.id))
    return visible[:limit]


def test_pick_recent_empty_returns_new_empty_list() -> None:
    """No memory yields a new empty list, never a fabricated event."""
    source: list[EmotionalEvent] = []

    result = pick_recent_high_medium(source, viewer_scope_id="group_1:user_9527")

    assert result == []
    assert result is not source


def test_pick_recent_excludes_low() -> None:
    high = make_event("evt_high")
    low = make_event("evt_low", severity="low")

    result = pick_recent_high_medium([high, low], viewer_scope_id="group_1:user_9527")

    assert result == [high]
    assert low not in result


def test_pick_recent_excludes_foreign_private() -> None:
    foreign = make_event(
        "evt_foreign",
        visibility_scope="private",
        scope_id="chat_9999",
    )
    mine = make_event("evt_mine", visibility_scope="private", scope_id="chat_8842")

    result = pick_recent_high_medium(
        [foreign, mine],
        viewer_scope_id="chat_8842",
    )

    assert foreign not in result
    assert mine in result


def test_pick_recent_keeps_own_private() -> None:
    """Reverse lock: filtering must not drop the viewer's own private events."""
    mine = make_event("evt_mine", visibility_scope="private", scope_id="chat_8842")

    result = pick_recent_high_medium([mine], viewer_scope_id="chat_8842")

    assert result == [mine]
    assert len(result) == 1


def test_pick_recent_keeps_group() -> None:
    """Reverse lock: a group event stays readable from another scope."""
    group = make_event("evt_group", visibility_scope="group", scope_id="group_1:user_9527")

    result = pick_recent_high_medium([group], viewer_scope_id="chat_8842")

    assert result == [group]


def test_pick_recent_limit_and_order() -> None:
    newest = make_event("evt_5", created_at=CREATED_AT + timedelta(minutes=5))
    fourth = make_event("evt_4", created_at=CREATED_AT + timedelta(minutes=4))
    third = make_event("evt_3", created_at=CREATED_AT + timedelta(minutes=3))
    second = make_event("evt_2", created_at=CREATED_AT + timedelta(minutes=2), severity="medium")
    oldest = make_event("evt_1", created_at=CREATED_AT + timedelta(minutes=1))
    dropped = make_event("evt_0", created_at=CREATED_AT, severity="low")
    events = [third, newest, dropped, oldest, fourth, second]

    result = pick_recent_high_medium(events, viewer_scope_id="group_1:user_9527", limit=3)

    assert len(result) == 3
    assert [event.id for event in result] == ["evt_5", "evt_4", "evt_3"]


def test_pick_recent_ties_break_by_id_ascending() -> None:
    """Equal timestamps order by id so the result never depends on input order."""
    later_id = make_event("evt_b")
    earlier_id = make_event("evt_a")

    forward = pick_recent_high_medium([later_id, earlier_id], viewer_scope_id="group_1:user_9527")
    backward = pick_recent_high_medium([earlier_id, later_id], viewer_scope_id="group_1:user_9527")

    assert [event.id for event in forward] == ["evt_a", "evt_b"]
    assert [event.id for event in forward] == [event.id for event in backward]
