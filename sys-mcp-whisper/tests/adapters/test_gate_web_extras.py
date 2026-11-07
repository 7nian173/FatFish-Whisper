"""Locks for the dependency-declaration split added by task-FFW-P3-DEPS.

Human ruling m16532 Q1-A: web-only dependencies live in
``[project.optional-dependencies].web`` and are readable by ``apps/`` alone;
``packages/`` keeps reading ``[project].dependencies`` and nothing else.

Why the split must live in ``check_package`` and not in ``declared_deps``:
``declared_deps`` answers "what does this tree declare", a question with one
honest answer regardless of who asks. ``check_package`` answers "what may this
package import", which is the question that actually differs per directory.
Pinning that boundary here means a later "helpful" refactor that folds the
allowance into ``declared_deps`` breaks a test instead of silently handing
``packages/legacy_bridge`` the Web stack.

The gate script is loaded by path (``scripts/`` is not a package), same idiom as
``test_apps_core_boundary.py`` and ``test_gate_deps_normalize.py``.
"""

from __future__ import annotations

import importlib.util
import tempfile
import tomllib
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_imports.py"
_SPEC = importlib.util.spec_from_file_location("check_imports", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GATE)

normalize = _GATE.normalize
declared_deps = _GATE.declared_deps

_TREE = Path(__file__).resolve().parents[2]
PYPROJECT = _TREE / "pyproject.toml"
APPS_DIR = _TREE / "apps"
PORTS_DIR = _TREE / "packages" / "core" / "ports"

#: Directory -> the module_name the gate tables use for it (same key as ``PACKAGES``).
FAKE_PYPROJECT = """[project]
name = "fake"
dependencies = ["pydantic"]

[project.optional-dependencies]
web = ["fastapi>=0.162.0", "uvicorn>=0.54.0"]
"""


def _read_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_bytes().decode("utf-8"))


def _fake_tree(root: Path, rel: str) -> Path:
    """Build a fake tree under ``root`` with a pyproject and ``import fastapi`` at ``rel``."""
    (root / "pyproject.toml").write_text(FAKE_PYPROJECT, encoding="utf-8")
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("import fastapi\n", encoding="utf-8")
    return target


# --- D1: the default path cannot see the web group, which is what isolates it ---


def test_declared_deps_default_excludes_web_extras() -> None:
    for pkg_dir in (APPS_DIR, PORTS_DIR):
        deps = declared_deps(pkg_dir)
        assert "pydantic" in deps, f"{pkg_dir} should still see the core declaration"
        assert "fastapi" not in deps, f"{pkg_dir} must not see web extras by default"
        assert "uvicorn" not in deps, f"{pkg_dir} must not see web extras by default"


# --- D2: explicit opt-in merges extras without losing the core declarations ---


def test_declared_deps_with_extras_includes_web_group() -> None:
    deps = declared_deps(APPS_DIR, include_extras=True)
    assert "fastapi" in deps
    assert "uvicorn" in deps
    assert "pydantic" in deps, "extras must be merged in, not substituted for the core list"


# --- D3: reverse lock; declared_deps is path-blind, the split lives in check_package ---


def test_declared_deps_with_extras_still_matches_packages_dir() -> None:
    ports = declared_deps(PORTS_DIR, include_extras=True)
    assert "fastapi" in ports, (
        "declared_deps answers 'what does this tree declare'; if it ever started "
        "filtering by caller, the allowance would move out of check_package and "
        "any directory could switch itself on"
    )


# --- D4: the web group holds exactly two entries, no requests / httpx creep ---


def test_web_extras_are_declared_exactly_as_fastapi_and_uvicorn() -> None:
    data = _read_pyproject()
    extras = data["project"]["optional-dependencies"]
    assert set(extras) == {"web"}, "only one extras group is sanctioned; new groups need a ruling"
    names = {normalize(d) for d in extras["web"]}
    assert len(extras["web"]) == 2, f"web group has {len(extras['web'])} entries"
    assert names == {"fastapi", "uvicorn"}, f"web group names are {sorted(names)}"


# --- D5: Q1 ① 的机器锁——原有 dependencies 一字不动 --------------------------


def test_core_dependencies_are_untouched() -> None:
    data = _read_pyproject()
    core = {normalize(d) for d in data["project"]["dependencies"]}
    assert core == {"pydantic"}, f"core dependencies drifted: {sorted(core)}"


# --- D6: extras 不外溢到 strict 包 ------------------------------------------


def test_extras_do_not_leak_into_strict_packages() -> None:
    assert "fastapi" not in _GATE.ALLOWED_TOP["packages.core.domain"]
    assert "fastapi" not in _GATE.ALLOWED_TOP["packages.core.engine"]
    # Behaviour layer: strict packages are judged by ALLOWED_TOP,
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _fake_tree(root, "packages/core/domain/models.py")
        original = _GATE.ROOT
        _GATE.ROOT = root
        try:
            report = _GATE.check_package("packages/core/domain", "packages.core.domain")
        finally:
            _GATE.ROOT = original
    assert report == ["packages/core/domain/models.py:1 fastapi"], report


# --- D7: end to end; apps passes, packages is refused ---


def test_apps_may_import_fastapi_but_packages_may_not() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _fake_tree(root, "apps/entrypoints/main.py")
        _fake_tree(root, "packages/core/ports/store.py")
        original = _GATE.ROOT
        _GATE.ROOT = root
        try:
            apps_report = _GATE.check_package("apps", "apps")
            ports_report = _GATE.check_package("packages/core/ports", "packages.core.ports")
        finally:
            _GATE.ROOT = original
    assert apps_report == [], f"apps must be allowed the web stack, got {apps_report}"
    assert ports_report == ["packages/core/ports/store.py:1 fastapi"], ports_report
