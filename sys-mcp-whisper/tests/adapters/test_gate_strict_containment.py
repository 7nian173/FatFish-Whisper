"""Strict containment tests for ``packages.core.ports`` / ``packages.legacy_bridge``.

Why these tests exist (2026-11-07, follow-up on the first-pass gate review):

* both packages were *non-strict* until today, so ``import sqlite3`` / ``import os``
  / ``import requests`` slipped through on the "stdlib is always fine" branch --
  the exact hole TASK-023 closed for domain/engine;
* the FORBIDDEN table was matched against the **first** dotted segment only, so
  every multi-segment pattern (``packages\\.core\\.engine``) was decorative;
* those same patterns were written with *doubled* backslashes, so even the
  single-segment ones (``old\\\\d+``) could never match anything.

The load-bearing case is the reverse verification: a fake tree containing a real
violation must make ``main()`` return non-zero. A gate that cannot fail is not a
gate. A clean fake tree must still return zero, otherwise "always red" would pass
this suite just as happily as "always green".

Every fake workspace lives in ``tmp_path`` and is installed with
``monkeypatch.setattr(_GATE, "ROOT", root)`` -- the same idiom as
``tests/adapters/test_apps_core_boundary.py``. Nothing is ever written into, or
deleted from, the real source tree, so a concurrent test session or a crashed run
cannot leave a stray probe file behind (the reason this suite does *not* drop
``_probe.py`` into ``packages/core/ports/`` for real: that directory is asserted
clean by ``test_other_packages_stay_clean`` in the sibling module).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

#: Workspace root: ``<root>/sys-mcp-whisper/tests/adapters/<this file>``.
_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _load_gate(name: str, filename: str) -> ModuleType:
    """Load a gate script by path (same idiom as the other gate tests)."""
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GATE = _load_gate("check_imports", "check_imports.py")

#: The two packages that became strict on 2026-11-07. Paths are relative to
#: ``_GATE.ROOT``; the fake-tree cases mount them at the root directly, while
#: ``_REAL_*`` below adds the ``sys-mcp-whisper/`` prefix the real tree uses.
_PORTS = ("packages/core/ports", "packages.core.ports")
_BRIDGE = ("packages/legacy_bridge", "packages.legacy_bridge")

#: The same two packages as they actually sit on disk (``PACKAGES`` in the gate
#: carries the main-tree prefix, so scanning without it would silently find no
#: files and pass for the wrong reason).
_REAL_PORTS = ("sys-mcp-whisper/packages/core/ports", "packages.core.ports")
_REAL_BRIDGE = ("sys-mcp-whisper/packages/legacy_bridge", "packages.legacy_bridge")

#: Every package the strict machinery must now cover.
STRICT_EXPECTED = frozenset(
    {
        "packages.core.domain",
        "packages.core.engine",
        "packages.core.ports",
        "packages.legacy_bridge",
    }
)


def _fake_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Materialise ``files`` (relative posix path -> source) under a fake root.

    A ``pyproject.toml`` is written first so ``declared_deps`` exercises its real
    ancestor walk instead of short-circuiting on a missing file.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fake"\ndependencies = ["pydantic"]\n', encoding="utf-8"
    )
    for rel, source in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return tmp_path


# --- 1. the tables themselves: no package may be silently left out -------------


def test_ports_and_legacy_bridge_are_now_strict() -> None:
    assert _GATE.STRICT_PACKAGES == STRICT_EXPECTED
    # The startup assertion inside the gate requires these two dicts to agree;
    # asserting it here keeps a future edit from "fixing" the gate by deletion.
    assert set(_GATE.ALLOWED_TOP) == STRICT_EXPECTED
    assert "apps" not in _GATE.STRICT_PACKAGES


def test_every_strict_package_has_a_non_empty_forbidden_table() -> None:
    for module_name in sorted(STRICT_EXPECTED):
        assert _GATE.FORBIDDEN[module_name], f"{module_name} 的 FORBIDDEN 表为空"
    # apps keeps its explicit empty list: adapters may legitimately use sqlite3.
    assert _GATE.FORBIDDEN["apps"] == []


def test_ports_forbids_io_network_and_legacy_modules() -> None:
    banned = {pat.pattern for pat in _GATE.FORBIDDEN["packages.core.ports"]}
    assert "sqlite3" in banned
    assert "os" in banned
    assert "requests" in banned
    assert "socket" in banned
    assert "old\\d+" in banned


def test_legacy_bridge_forbids_io_and_network_but_not_old_modules() -> None:
    """The bridge is the one place old modules may eventually be wired in.

    Blacklisting ``old\\d+`` here would make wiring impossible; that constraint
    belongs to ``check_composition.py`` (legacy-isolation: only *this* directory
    may import them), so the two gates each own one half of the rule.
    """
    banned = {pat.pattern for pat in _GATE.FORBIDDEN["packages.legacy_bridge"]}
    assert {"sqlite3", "os", "requests", "socket"} <= banned
    assert "old\\d+" not in banned


# --- 2. the doubled-backslash regression ---------------------------------------


def test_no_forbidden_pattern_is_doubly_escaped() -> None:
    """``r"old\\\\d+"`` means "a literal backslash then d", i.e. it matches nothing.

    That typo made the old-module blacklist and the legacy-isolation rule both
    no-ops; this test is the tripwire for a recurrence.
    """
    for module_name, patterns in _GATE.FORBIDDEN.items():
        for pat in patterns:
            assert "\\\\" not in pat.pattern, f"{module_name}: {pat.pattern!r} 含双反斜杠"
            # Every pattern must be a usable regex, not a silently-broken one.
            pat.fullmatch("anything")


def test_forbidden_patterns_match_the_full_module_name() -> None:
    """Multi-segment patterns can only ever match the full name, not the head."""
    patterns = _GATE.FORBIDDEN["packages.core.domain"]
    assert any(pat.fullmatch("packages.core.engine.routing") for pat in patterns)
    assert any(pat.fullmatch("packages.legacy_bridge.guards") for pat in patterns)
    assert any(pat.fullmatch("old0002") for pat in patterns)
    # Head-only matching (the pre-2026-11-07 behaviour) never fires for these.
    assert not any(pat.fullmatch("packages") for pat in patterns)


def test_legacy_isolation_pattern_really_matches_old_modules() -> None:
    """``check_composition.LEGACY_PATTERN`` is the other half of the same typo."""
    saved_path = list(sys.path)
    try:
        composition = _load_gate("check_composition", "check_composition.py")
    finally:
        sys.path[:] = saved_path

    pattern = composition.LEGACY_PATTERN
    assert "\\\\" not in pattern.pattern
    for name in ("old0002", "old0003", "old9"):
        assert pattern.fullmatch(name), f"{name} 必须被 LEGACY_PATTERN 命中"
    for name in ("old", "olds", "old_0002", "tracka"):
        assert not pattern.fullmatch(name), f"{name} 不应被 LEGACY_PATTERN 命中"


# --- 3. reverse verification: the gate must actually fail ---------------------


def test_ports_import_sqlite3_is_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(tmp_path, {"packages/core/ports/_probe.py": "import sqlite3\n"})
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package(*_PORTS) == ["packages/core/ports/_probe.py:1 sqlite3"]


def test_ports_import_os_and_requests_are_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(
        tmp_path,
        {"packages/core/ports/_probe.py": "import os\nimport requests\nimport fastapi\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package(*_PORTS) == [
        "packages/core/ports/_probe.py:1 os",
        "packages/core/ports/_probe.py:2 requests",
        # fastapi is neither stdlib nor a declared dependency of the core tree.
        "packages/core/ports/_probe.py:3 fastapi",
    ]


def test_legacy_bridge_import_requests_is_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_tree(
        tmp_path, {"packages/legacy_bridge/_probe.py": "import requests\nimport socket\n"}
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package(*_BRIDGE) == [
        "packages/legacy_bridge/_probe.py:1 requests",
        "packages/legacy_bridge/_probe.py:2 socket",
    ]


def test_protocol_only_imports_are_still_allowed_in_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse control: going strict must not ban what ``ports`` actually imports.

    ``packages/core/ports/*.py`` only ever does ``__future__`` + ``collections.abc``
    + ``typing`` today; those three must stay green or the new strict flag would
    turn the whole tree red for no reason.
    """
    root = _fake_tree(
        tmp_path,
        {
            "packages/core/ports/_probe.py": (
                "from __future__ import annotations\n"
                "from collections.abc import Mapping\n"
                "from typing import Protocol\n"
            )
        },
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.check_package(*_PORTS) == []


def test_gate_main_returns_non_zero_for_a_ports_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end-to-end reverse check: a real ``import sqlite3`` turns rc into 1.

    ``check_imports.main()`` is the function ``raise SystemExit(main())`` maps to
    the process exit code, so a non-zero return here *is* "the gate script failed".

    The probe sits at the path ``main()`` actually scans (``PACKAGES`` keys are
    prefixed with the main tree, ``sys-mcp-whisper/``) -- a probe anywhere else
    would be silently invisible to the script and prove nothing.
    """
    root = _fake_tree(
        tmp_path,
        {"sys-mcp-whisper/packages/core/ports/_probe.py": "import sqlite3\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.main() != 0
    # The verdict must name the offending package and the offending import, not
    # just return 1 for some unrelated reason.
    out = capsys.readouterr().out
    assert "packages.core.ports FAIL" in out
    assert "_probe.py:1 sqlite3" in out


def test_gate_main_returns_zero_for_a_clean_fake_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Twin control: without the violation the very same tree passes."""
    root = _fake_tree(
        tmp_path,
        {"sys-mcp-whisper/packages/core/ports/_probe.py": "from typing import Protocol\n"},
    )
    monkeypatch.setattr(_GATE, "ROOT", root)

    assert _GATE.main() == 0
    out = capsys.readouterr().out
    assert "packages.core.ports PASS" in out


# --- 4. the real tree must not regress ----------------------------------------


@pytest.mark.parametrize(("pkg_dir", "module_name"), [_REAL_PORTS, _REAL_BRIDGE])
def test_real_tree_stays_clean(pkg_dir: str, module_name: str) -> None:
    """The genuine source tree must pass under the newly strict tables.

    Scanned through the ``sys-mcp-whisper/``-prefixed paths (what ``PACKAGES``
    uses); an unprefixed path points at a directory that does not exist, which
    would make this assertion green while proving nothing.
    """
    assert _GATE.check_package(pkg_dir, module_name) == []


def test_real_tree_actually_contains_files() -> None:
    """Guard against the empty-directory illusion behind the previous case."""
    for pkg_dir, _ in (_REAL_PORTS, _REAL_BRIDGE):
        assert _GATE.iter_py_files(_GATE.ROOT / pkg_dir), f"{pkg_dir} 下没有扫到任何 .py"


# --- 5. the literal end-to-end drill: a real file in the real ports/ ----------


def test_literal_probe_file_in_real_ports_turns_the_gate_red(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The drill asked for verbatim: drop a file into ports/, run the gate, delete it.

    This is the one case that touches the genuine source tree instead of a fake
    root, so the cleanup contract is explicit: the probe is removed in
    ``finally`` even when an assertion fails.  A leaked probe would turn the gate
    permanently red and would be loud about it (the file name is in every
    failure message), which is the safe direction for this kind of drill.

    The assertion is on ``main()`` rather than ``check_package()``: ``main()`` is
    what ``raise SystemExit(main())`` maps to the process exit code, so a
    non-zero return here is exactly "the gate script returns rc != 0".
    """
    probe = (
        _GATE.ROOT
        / "sys-mcp-whisper"
        / "packages"
        / "core"
        / "ports"
        / "_probe_import_sqlite3.py"
    )
    assert not probe.exists(), f"{probe} already exists -- cleanup from a previous run leaked"
    try:
        probe.write_text("import sqlite3\n", encoding="utf-8")
        rc = _GATE.main()
        out = capsys.readouterr().out
        assert rc != 0, f"ports 目录里放了 import sqlite3 但门禁仍返回 {rc}"
        assert "packages.core.ports FAIL" in out
        assert "_probe_import_sqlite3.py:1 sqlite3" in out
    finally:
        probe.unlink(missing_ok=True)

    # And the tree is green again once the probe is gone.
    assert _GATE.check_package(*_REAL_PORTS) == []
