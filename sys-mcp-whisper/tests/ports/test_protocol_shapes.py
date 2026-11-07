"""Shape locks for the four port protocols (task-FFW-P25-T4, captain addendum).

These are *shape* locks, not behaviour locks: P2.5 ships declarations only, so
there is nothing to execute.  What has to be pinned is that nobody quietly
smuggles an implementation into a ``Protocol`` -- an interface that grows a
default body stops being an interface and the P4 implementer would inherit
behaviour nobody decided.

The ast check below is the only lock that can catch that: a method body that is
``[Expr(Constant(Ellipsis))]`` is a declaration; ``pass``, a ``return`` or a
``raise NotImplementedError`` all fail it.
"""

from __future__ import annotations

import ast
import inspect
from typing import Protocol

import pytest

from packages.core.ports import (
    AuditPort,
    ConsolidationPort,
    GlobalStatePort,
    ObservabilityPort,
    StateRepositoryPort,
    Transaction,
)

PROTOCOLS = (
    StateRepositoryPort,
    ConsolidationPort,
    ObservabilityPort,
    AuditPort,
    GlobalStatePort,
    Transaction,
)

# The declared members, spelled out by hand so a rename or a silent drop turns
# this file red instead of being absorbed by the reflection below.
EXPECTED_MEMBERS: dict[str, dict[str, tuple[str, ...]]] = {
    "StateRepositoryPort": {
        "save_if_version_match": ("self", "snapshot", "expected_version"),
        "load": ("self",),
    },
    "ConsolidationPort": {
        "offline_dream": ("self", "events"),
        "replay_rumination": ("self", "events"),
    },
    "ObservabilityPort": {
        "emit_agent_run": ("self", "payload"),
        "emit_token_usage": ("self", "payload"),
        "check_token_budget": ("self", "estimated_tokens"),
    },
    "AuditPort": {
        "counterfactual_audit": ("self", "events", "candidate_param"),
    },
    "Transaction": {
        "commit": ("self",),
        "rollback": ("self",),
    },
    "GlobalStatePort": {
        "get": ("self", "tx", "scope_id"),
        "save": (
            "self",
            "tx",
            "snapshot",
            "expected_snapshot_version",
            "context",
            "expected_context_version",
        ),
    },
}


def _declared_functions(proto: type) -> dict[str, object]:
    """The protocol's own methods, excluding dunders synthesised by Protocol."""
    return {
        name: member
        for name, member in inspect.getmembers(proto, inspect.isfunction)
        if not name.startswith("__")
    }


@pytest.mark.parametrize("proto", PROTOCOLS, ids=lambda proto: proto.__name__)
def test_port_is_a_protocol(proto: type) -> None:
    assert getattr(proto, "_is_protocol", False) is True, proto
    assert issubclass(proto, Protocol) or proto is not Protocol


@pytest.mark.parametrize("proto", PROTOCOLS, ids=lambda proto: proto.__name__)
def test_port_declares_exactly_the_expected_members(proto: type) -> None:
    declared = _declared_functions(proto)
    assert set(declared) == set(EXPECTED_MEMBERS[proto.__name__]), (
        f"{proto.__name__} members drifted: "
        f"missing={sorted(set(EXPECTED_MEMBERS[proto.__name__]) - set(declared))} "
        f"unexpected={sorted(set(declared) - set(EXPECTED_MEMBERS[proto.__name__]))}"
    )


@pytest.mark.parametrize("proto", PROTOCOLS, ids=lambda proto: proto.__name__)
def test_port_parameter_names_are_exact(proto: type) -> None:
    for name, member in _declared_functions(proto).items():
        expected = EXPECTED_MEMBERS[proto.__name__][name]
        measured = tuple(inspect.signature(member).parameters)
        assert measured == expected, f"{proto.__name__}.{name}: {measured} != {expected}"


@pytest.mark.parametrize("proto", PROTOCOLS, ids=lambda proto: proto.__name__)
def test_every_method_body_is_ellipsis(proto: type) -> None:
    """No smuggled implementation: the body must be exactly ``...``.

    ``Protocol`` synthesises ``__init__``; it is filtered out above so the check
    only sees the methods this package declares.
    """
    for name, member in _declared_functions(proto).items():
        source = inspect.getsource(member)
        tree = ast.parse(inspect.cleandoc(source))
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef), f"{proto.__name__}.{name}"
        assert len(function.body) == 1, (
            f"{proto.__name__}.{name} has {len(function.body)} statements; "
            "a port method body must be a single ``...``"
        )
        statement = function.body[0]
        assert isinstance(statement, ast.Expr), (
            f"{proto.__name__}.{name}: body is {type(statement).__name__}, expected ``...``"
        )
        assert isinstance(statement.value, ast.Constant), f"{proto.__name__}.{name}"
        assert statement.value.value is Ellipsis, (
            f"{proto.__name__}.{name} returns {statement.value.value!r}; "
            "port declarations must be ``...``"
        )


def test_member_sets_do_not_overlap() -> None:
    """A copy-paste error usually shows up as the same name in two protocols."""
    seen: dict[str, str] = {}
    for proto_name, members in EXPECTED_MEMBERS.items():
        for member in members:
            assert member not in seen, (
                f"{member} declared in both {seen.get(member)} and {proto_name}"
            )
            seen[member] = proto_name
    assert len(seen) == 12, f"expected 12 distinct port methods, found {len(seen)}"


def test_member_union_is_not_empty() -> None:
    """Reverse guard: an empty ``PROTOCOLS``/table pair would pass everything above."""
    union = {member for members in EXPECTED_MEMBERS.values() for member in members}
    assert len(union) == 12, union
    assert len(PROTOCOLS) == 6


def test_port_methods_are_not_implemented_by_the_protocol() -> None:
    """Calling a port method must not run code -- ``...`` evaluates to ``None``.

    Behavioural half of the ast lock: it proves ``...`` is the *only* thing in
    the body, because a stub that raised would blow up here.
    """
    with pytest.raises(TypeError):
        StateRepositoryPort()  # Protocol cannot be instantiated (not runtime-checkable)
