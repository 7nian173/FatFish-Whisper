"""Contract locks for GlobalStatePort / Transaction (task-FFW-P3-A).

Declarations-only port: these tests pin the *shape* that blueprint
plan-p3-orchestration.md section 4 (P3-C) decided, so a later implementer
cannot quietly loosen it:

- R1: both carriers are read through a single ``get(tx)`` returning a pair,
  so no torn snapshot/context combination can be assembled by callers.
- U2 final ruling: every port method takes ``tx`` first -- there is no
  tx-less form, and the optimistic-lock parameters on ``save`` stay put.
"""

from __future__ import annotations

import inspect
import typing

from packages.core.ports import GlobalStatePort, Transaction

# T7: the package surface must export exactly these six names, sorted.
EXPECTED_ALL = [
    "AuditPort",
    "ConsolidationPort",
    "GlobalStatePort",
    "ObservabilityPort",
    "StateRepositoryPort",
    "Transaction",
]


def test_t1_global_state_port_is_structural_protocol() -> None:
    """P3-C declaration: Protocol shape must be reflectable, not instantiable."""
    assert typing.get_origin(GlobalStatePort) is None
    assert issubclass(GlobalStatePort, typing.Protocol)
    for name in ("get", "save"):
        assert callable(getattr(GlobalStatePort, name)), name
    assert not hasattr(GlobalStatePort, "__annotations__") or isinstance(
        GlobalStatePort.__annotations__, dict
    )


def test_t2_save_first_parameter_is_tx() -> None:
    """U2 ruling: ``save`` is always called through a transaction (tx first)."""
    sig = inspect.signature(GlobalStatePort.save)
    params = list(sig.parameters)
    assert params[1] == "tx", params
    assert params[0] == "self"


def test_t3_get_first_parameter_is_tx_and_returns_pair() -> None:
    """R1: single ``get(tx)`` whose annotation is a tuple of both carriers."""
    sig = inspect.signature(GlobalStatePort.get)
    params = list(sig.parameters)
    assert params[1] == "tx", params
    hints = typing.get_type_hints(GlobalStatePort.get)
    assert typing.get_origin(hints["return"]) is tuple, hints["return"]


def test_t4_no_repository_style_members_on_global_state_port() -> None:
    """StateRepositoryPort members must not bleed into this Protocol."""
    for forbidden in ("load", "save_if_version_match"):
        assert not hasattr(GlobalStatePort, forbidden), forbidden


def test_t5_transaction_has_exactly_commit_and_rollback() -> None:
    """Transaction boundary: exactly two methods, both returning None."""
    declared = {
        name: member
        for name, member in inspect.getmembers(Transaction, inspect.isfunction)
        if not name.startswith("__")
    }
    assert set(declared) == {"commit", "rollback"}, set(declared)
    for name in ("commit", "rollback"):
        member = declared[name]
        return_hints = typing.get_type_hints(member)
        assert return_hints["return"] is type(None), (name, return_hints)


def test_t6_tx_annotations_point_at_transaction() -> None:
    """The ``tx`` parameter must be typed as Transaction, not object/Any."""
    hints = typing.get_type_hints(GlobalStatePort.get)
    assert "Transaction" in str(hints["tx"]), hints["tx"]
    hints_save = typing.get_type_hints(GlobalStatePort.save)
    assert "Transaction" in str(hints_save["tx"]), hints_save["tx"]


def test_t7_all_contains_both_names_and_is_sorted() -> None:
    """Package surface: both new names exported, __all__ kept sorted (RUF022)."""
    from packages.core import ports

    assert ports.__all__ == EXPECTED_ALL
    assert ports.__all__ == sorted(ports.__all__)


def test_t8_bidirectional_import_works() -> None:
    """Both module-level and package-level imports must resolve."""
    import importlib

    module = importlib.import_module("packages.core.ports.global_state_port")
    assert module.GlobalStatePort is GlobalStatePort
    assert module.Transaction is Transaction
