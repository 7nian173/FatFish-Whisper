"""Boundary tests for the ``apps`` -> Core allowance added by task-FFW-P3-GATE.

The gate script is loaded by path (same idiom as ``tests/domain/test_contracts.py``)
so the assertions read the one authoritative table instead of a copy that can drift.

Why these tests exist: ``apps`` is *not* a strict package, so its only constraint is
"stdlib + declared deps". P3-GATE adds one narrow exception -- ``apps`` may import
``packages.core.domain.*`` and ``packages.core.ports.*`` -- and that exception is the
kind of rule that silently widens if it is ever rewritten with a bare
``str.startswith``. The prefix cases below pin the dot boundary from both sides.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_imports.py"
_SPEC = importlib.util.spec_from_file_location("check_imports", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GATE)

#: Real package directories, mapped to the module name the gate keys its tables by.
REAL_PACKAGES = (
    ("sys-mcp-whisper/packages/core/domain", "packages.core.domain"),
    ("sys-mcp-whisper/packages/core/engine", "packages.core.engine"),
    ("sys-mcp-whisper/packages/core/ports", "packages.core.ports"),
    ("sys-mcp-whisper/packages/legacy_bridge", "packages.legacy_bridge"),
)


def _fake_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Materialise ``files`` (relative posix path -> source) under a fake workspace root.

    A ``pyproject.toml`` is included so ``declared_deps`` exercises its real ancestor
    walk rather than short-circuiting on a missing file.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fake"\ndependencies = ["pydantic"]\n', encoding="utf-8"
    )
    for rel, source in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return tmp_path


# --- A11: the allowlist is exactly two entries, and engine is not one of them ---


def test_allowed_core_prefixes_are_exactly_domain_and_ports() -> None:
    prefixes = _GATE.APPS_ALLOWED_CORE_PREFIXES
    assert isinstance(prefixes, tuple)
    assert set(prefixes) == {"packages.core.domain", "packages.core.ports"}
    assert len(prefixes) == 2
    assert "packages.core.engine" not in prefixes
    assert "packages.core" not in prefixes


# --- A1 / A2 / A3: the two allowed prefixes and their submodules ---


@pytest.mark.parametrize(
    "modname",
    [
        "packages.core.domain",
        "packages.core.domain.contracts",
        "packages.core.domain.state",
        "packages.core.domain.exceptions",
    ],
)
def test_domain_and_its_submodules_are_allowed(modname: str) -> None:
    # Path is an adapters file on purpose: domain is allowed regardless of caller.
    path = "apps/adapters/inbound/dto.py"
    assert _GATE._is_allowed_core_import(modname, path) is True


@pytest.mark.parametrize(
    "modname",
    [
        "packages.core.ports",
        "packages.core.ports.global_state_port",
        "packages.core.ports.state_repository_port",
    ],
)
def test_ports_and_its_submodules_are_allowed(modname: str) -> None:
    # A different apps subtree, same verdict: ports is path-independent too.
    path = "apps/adapters/outbound/store.py"
    assert _GATE._is_allowed_core_import(modname, path) is True


# --- A4 / A5: engine must stay out, package and submodule alike ---


@pytest.mark.parametrize(
    "modname",
    [
        "packages.core.engine",
        "packages.core.engine.routing",
        "packages.core.engine.judge",
        "packages.core.engine.assembler",
    ],
)
def test_engine_and_its_submodules_are_not_allowed(modname: str) -> None:
    # The caller is an inbound adapter, so engine must be refused (P3-GATE2).
    path = "apps/adapters/inbound/dto.py"
    assert _GATE._is_allowed_core_import(modname, path) is False


# --- A6: the dot boundary. A bare startswith() would let these through ---


@pytest.mark.parametrize(
    "modname",
    [
        "packages.core.domainXYZ",
        "packages.core.domainXYZ.contracts",
        "packages.core.domains",
        "packages.core.ports_extra",
        "packages.core.portsX",
    ],
)
def test_a_sibling_that_shares_the_textual_prefix_is_not_allowed(modname: str) -> None:
    # An orchestrator path: proving the path allowance does not drag in fake names.
    path = "apps/composition/use_case.py"
    assert _GATE._is_allowed_core_import(modname, path) is False


def test_the_prefix_must_include_the_core_segment() -> None:
    path = "apps/composition/use_case.py"
    assert _GATE._is_allowed_core_import("packages.core", path) is False
    assert _GATE._is_allowed_core_import("packages", path) is False


# --- A7 / A8: everything else keeps its pre-existing verdict ---


def test_legacy_bridge_is_not_allowed() -> None:
    # Even from the orchestrator layer legacy is shut: it is reachable only
    # through the bridge package, never by direct import.
    path = "apps/composition/use_case.py"
    assert _GATE._is_allowed_core_import("packages.legacy_bridge", path) is False
    assert _GATE._is_allowed_core_import("packages.legacy_bridge.guards", path) is False


def test_stdlib_and_third_party_names_are_not_allowed() -> None:
    # Same orchestrator path: the answer is about the target, not the caller.
    path = "apps/composition/use_case.py"
    for modname in ("sqlite3", "os", "requests", "httpx", "pydantic"):
        assert _GATE._is_allowed_core_import(modname, path) is False


# --- A9 / A10: the real tree must not regress ---


def test_real_apps_tree_has_no_violations() -> None:
    assert _GATE.check_package("sys-mcp-whisper/apps", "apps") == []


@pytest.mark.parametrize(("pkg_dir", "module_name"), REAL_PACKAGES)
def test_other_packages_stay_clean(pkg_dir: str, module_name: str) -> None:
    assert _GATE.check_package(pkg_dir, module_name) == []


# --- behavioural: the allowance is keyed on the module name, not on the path ---


def test_only_apps_gets_the_core_allowance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same file, same import, two module names: only ``apps`` is exempted."""
    root = _fake_tree(
        tmp_path,
        {"apps/adapters/inbound/dto.py": "from packages.core.domain.contracts import MCPRequest\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package("apps", "apps") == []

    ports_report = _GATE.check_package("apps", "packages.core.ports")
    assert ports_report == ["apps/adapters/inbound/dto.py:1 packages.core.domain.contracts"]


def test_engine_import_is_still_a_violation_for_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the card: domain/ports open, engine firmly shut."""
    root = _fake_tree(
        tmp_path,
        {
            "apps/adapters/inbound/dto.py": (
                "from packages.core.engine.routing import route_track_a\n"
            )
        },
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    report = _GATE.check_package("apps", "apps")
    expected = ["apps/adapters/inbound/dto.py:1 packages.core.engine.routing"]
    assert report == expected


def test_sibling_module_is_still_a_violation_for_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(
        tmp_path,
        {"apps/adapters/inbound/dto.py": "import packages.core.domainXYZ\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    expected = ["apps/adapters/inbound/dto.py:1 packages.core.domainXYZ"]
    assert _GATE.check_package("apps", "apps") == expected


def test_stdlib_import_from_apps_is_still_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse control: the new branch must not shadow the stdlib path."""
    root = _fake_tree(
        tmp_path,
        {"apps/adapters/outbound/store.py": "import sqlite3\nimport json\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package("apps", "apps") == []


def test_legacy_bridge_import_from_apps_is_still_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(
        tmp_path,
        {"apps/adapters/outbound/bridge.py": "import packages.legacy_bridge.guards\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package("apps", "apps") == [
        "apps/adapters/outbound/bridge.py:1 packages.legacy_bridge.guards"
    ]


def test_reverse_boundary_core_may_not_import_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing rule, asserted here so the new branch cannot weaken it."""
    root = _fake_tree(
        tmp_path,
        {"packages/core/domain/_probe.py": "import apps\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package("packages/core/domain", "packages.core.domain") == [
        "packages/core/domain/_probe.py:1 apps"
    ]


# --- P3-GATE2: engine allowance is keyed on the calling file's directory ------


def test_engine_dir_prefixes_are_exactly_use_cases_and_composition() -> None:
    """The new table is a closed, two-entry, directory-only list."""
    prefixes = _GATE.APPS_ENGINE_ALLOWED_DIR_PREFIXES
    assert isinstance(prefixes, tuple)
    assert len(prefixes) == 2
    assert set(prefixes) == {"apps/use_cases/", "apps/composition/"}
    assert not any(p.startswith("apps/adapters/") for p in prefixes)
    # The module-level table keeps its old meaning: domain/ports only, no engine.
    assert "packages.core.engine" not in _GATE.APPS_ALLOWED_CORE_PREFIXES


def test_engine_import_is_allowed_from_use_cases_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(
        tmp_path,
        {
            "apps/use_cases/use_case.py": (
                "from packages.core.engine.dynamics import update_hormones\n"
            )
        },
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package("apps", "apps") == []


def test_engine_import_is_allowed_from_composition_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(
        tmp_path,
        {
            "apps/composition/use_case.py": (
                "from packages.core.engine.dynamics import update_hormones\n"
            )
        },
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package("apps", "apps") == []


def test_engine_import_is_rejected_from_adapters_inbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The red line: the transport layer may never reach the engine directly."""
    root = _fake_tree(
        tmp_path,
        {"apps/adapters/inbound/dto.py": "from packages.core.engine.routing import route\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    expected = ["apps/adapters/inbound/dto.py:1 packages.core.engine.routing"]
    assert _GATE.check_package("apps", "apps") == expected


def test_engine_import_is_rejected_from_adapters_outbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(
        tmp_path,
        {"apps/adapters/outbound/store.py": "from packages.core.engine.judge import judge\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    expected = ["apps/adapters/outbound/store.py:1 packages.core.engine.judge"]
    assert _GATE.check_package("apps", "apps") == expected


def test_use_cases_dir_prefix_is_not_matched_by_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A substring test would let this path through; a prefix test must not."""
    root = _fake_tree(
        tmp_path,
        {
            "apps/adapters/inbound/apps/use_cases/x.py": (
                "from packages.core.engine.dynamics import update_hormones\n"
            )
        },
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    expected = [
        "apps/adapters/inbound/apps/use_cases/x.py:1 packages.core.engine.dynamics"
    ]
    assert _GATE.check_package("apps", "apps") == expected


def test_unknown_apps_subdir_is_rejected_for_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: a real apps subdir that nobody listed buys no engine access."""
    root = _fake_tree(
        tmp_path,
        {"apps/entrypoints/main.py": "from packages.core.engine.router import route\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    expected = ["apps/entrypoints/main.py:1 packages.core.engine.router"]
    assert _GATE.check_package("apps", "apps") == expected
