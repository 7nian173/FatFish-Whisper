"""``StateRepositoryPort`` shape and the optimistic-lock mock (task-FFW-P25-T4).

Six criteria from card section 3.5, each with the negative side spelled out so
that a ``save_if_version_match`` which always returns ``False`` (or always
``True``) cannot pass.
"""

from __future__ import annotations

import inspect

import pytest

from packages.core.domain.state import IPCState, S1Snapshot, TraitState
from packages.core.ports import StateRepositoryPort
from tests.ports.mock_state_repository import MockStateRepository

TS = "2026-11-06T09:00:00Z"


def make_snapshot(version: int = 1, scope_id: str = "chat_8842") -> S1Snapshot:
    return S1Snapshot.model_validate(
        {
            "version": version,
            "scope_id": scope_id,
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


# 1. First save succeeds ---------------------------------------------------


def test_first_save_increments_and_is_loadable() -> None:
    repo = MockStateRepository()
    assert repo.load() is None, "a fresh repository must start empty"

    snapshot = make_snapshot()
    assert repo.save_if_version_match(snapshot, 0) is True
    assert repo.load() is snapshot
    assert repo.version == 1


# 2. Stale write is refused and does not overwrite ------------------------


def test_stale_write_is_refused_and_the_snapshot_is_untouched() -> None:
    repo = MockStateRepository()
    first = make_snapshot(version=1, scope_id="chat_A")
    second = make_snapshot(version=2, scope_id="chat_B")

    assert repo.save_if_version_match(first, 0) is True
    assert repo.save_if_version_match(second, 0) is False
    assert repo.load() is first, "a refused write must not replace the stored snapshot"
    assert repo.version == 1, "a refused write must not bump the token"


# 3. Two writers holding the same token -----------------------------------


def test_two_writers_with_the_same_token_second_one_fails() -> None:
    repo = MockStateRepository()
    writer_a = make_snapshot(scope_id="chat_A")
    writer_b = make_snapshot(scope_id="chat_B")

    token_seen_before = repo.version
    assert repo.save_if_version_match(writer_a, token_seen_before) is True
    assert repo.save_if_version_match(writer_b, token_seen_before) is False
    assert repo.load() is writer_a


# 4. The mock really runs (not just hasattr) -----------------------------


def test_the_mock_is_exercised_not_merely_inspected() -> None:
    repo = MockStateRepository()
    result = repo.save_if_version_match(make_snapshot(), 0)
    assert isinstance(result, bool)
    assert result is True
    assert repo.load() is not None


# 5. Protocol shape ------------------------------------------------------


def test_state_repository_port_signature() -> None:
    signature = inspect.signature(StateRepositoryPort.save_if_version_match)
    assert list(signature.parameters) == ["self", "snapshot", "expected_version"]
    assert signature.return_annotation in (bool, "bool")

    load_signature = inspect.signature(StateRepositoryPort.load)
    assert list(load_signature.parameters) == ["self"]


def test_mock_satisfies_the_port_protocol() -> None:
    """The mock's signature must match the port, parameter for parameter."""
    port_params = list(inspect.signature(StateRepositoryPort.save_if_version_match).parameters)
    mock_params = list(inspect.signature(MockStateRepository.save_if_version_match).parameters)
    assert mock_params == port_params


# 6. Reverse guard: the mock is not a constant ---------------------------


def test_correct_version_can_save_again_after_a_successful_write() -> None:
    """Guards against an implementation that always returns ``False``."""
    repo = MockStateRepository()
    assert repo.save_if_version_match(make_snapshot(scope_id="chat_A"), 0) is True
    assert repo.save_if_version_match(make_snapshot(scope_id="chat_B"), 1) is True
    assert repo.version == 2


def test_a_wrong_version_then_the_right_one_recovers() -> None:
    """Both directions on the same repository, so neither constant can pass."""
    repo = MockStateRepository()
    with pytest.raises(TypeError):
        repo.save_if_version_match(make_snapshot())  # type: ignore[call-arg]
    assert repo.save_if_version_match(make_snapshot(), 0) is True
    assert repo.save_if_version_match(make_snapshot(), 99) is False
    assert repo.save_if_version_match(make_snapshot(), 1) is True
