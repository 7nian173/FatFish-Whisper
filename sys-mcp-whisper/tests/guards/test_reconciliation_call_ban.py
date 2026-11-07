"""ADR-009 R1-4 criterion 1 -- the P3 orchestration layer must never touch the
archived reconciliation queue (R1-1: keep the code, ban the call).

Why this file exists
--------------------
The archived module ``packages/core/engine/reconciliation.py`` stays on disk and keeps
its tests green (criterion 2, asserted by ``test_c2_...``). What is forbidden is any
*call* reachable from ``apps/``. The literal wording of criterion 1 only recognises
three syntax shapes (``ast.Name``, ``ast.Attribute``, and ``ast.ImportFrom`` whose
module *ends with* ``reconciliation``). Three of the six attack vectors the lead
measured slip through that wording:

* ``from packages.core.engine import AsyncReconciliationQueue`` -- the engine package
  ``__init__`` deliberately re-exports both banned symbols, so this import really
  works and the literal judge never looks at the alias names.
* ``getattr(engine, "AsyncReconciliationQueue")`` -- no ``Name``/``Attribute`` node
  carries the symbol.
* ``importlib.import_module("packages.core.engine.reconciliation")`` -- the module is
  only named in a string literal.

So the scanner below judges **five** node classes instead of three, and this module
asserts each class fires exactly once on a minimal sample (A group), stays silent on
real-shaped clean code (B group), and that the tree it protects is really non-empty
(C group).

Known boundary (stated, not papered over)
-----------------------------------------
``getattr(engine, "Async" + "ReconciliationQueue")`` cannot be decided statically: the
AST holds two literals, neither of which is a banned symbol. This scanner therefore
takes the superset stance for dynamic access -- *any* string literal argument that is a
banned symbol or ends with ``reconciliation`` is a violation -- and refuses to claim it
covers concatenation. That residue is a code-review obligation, not a test obligation.

A second residue was expected and measured away. A plain ``import
packages.core.engine.reconciliation`` (no ``from``) is not one of the five classes, so it
looked like a hole; but reaching either banned symbol through that module alias requires
an ``Attribute`` whose ``attr`` *is* the symbol name (``reconciliation.PendingDelta``),
and the ``Attribute`` class catches exactly that. Verified by probe: the bare-import form
produces one ``Attribute`` hit. No sixth class is needed.

The scanner never imports the code it judges -- it only calls ``ast.parse``. Importing
a candidate module would execute it, which is a side effect a judge must not have.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

#: The two symbols exported by the archived module that apps must not name.
BANNED_SYMBOLS: Final[frozenset[str]] = frozenset({"AsyncReconciliationQueue", "PendingDelta"})

#: Any imported module whose dotted path ends here is off limits as a whole.
BANNED_MODULE_SUFFIX: Final[str] = "reconciliation"

#: The package root that re-exports the banned symbols (the A4 hole).
ENGINE_PACKAGE: Final[str] = "packages.core.engine"

#: Anchored off ``__file__`` on purpose. ``30_沙盒区/pytest.ini`` sets
#: ``testpaths = sys-mcp-whisper/tests`` and CI runs from ``30_沙盒区/``, while people
#: also run from ``sys-mcp-whisper/``; a bare ``Path("apps")`` is red in one of them.
APPS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "apps"

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Callees whose effect depends on a string argument we can still read.
_DYNAMIC_CALLEES: Final[frozenset[str]] = frozenset({"getattr", "__import__", "import_module"})

_MESSAGE: Final[str] = (
    "FFW-ADR009-R1: {filename}:{lineno} references banned reconciliation symbol ({why})"
)


def _callee_name(node: ast.Call) -> str | None:
    """Return the bare name of ``node.func`` for ``f(...)`` and ``mod.f(...)`` shapes."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_arguments(node: ast.Call) -> list[str]:
    """Every plain string literal passed to ``node``, positional or keyword.

    Only plain literals: an f-string or a concatenation is a ``JoinedStr`` / ``BinOp``
    and yields nothing here, which is exactly the boundary the module docstring records.
    """
    values: list[ast.expr] = [*node.args, *(kw.value for kw in node.keywords)]
    literals: list[str] = []
    for value in values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            literals.append(value.value)
    return literals


def _import_from_reasons(node: ast.ImportFrom) -> list[str]:
    """Classify one ``from ... import ...`` statement into zero or more reasons."""
    module = node.module or ""
    # Class 3: the whole archived module is banned, whatever name is pulled out of it.
    # Reported once for the statement -- the ``elif`` below keeps A3 at exactly one hit.
    if module.endswith(BANNED_MODULE_SUFFIX):
        return ["ImportFrom"]
    if module != ENGINE_PACKAGE and not module.startswith(ENGINE_PACKAGE + "."):
        return []
    reasons: list[str] = []
    for alias in node.names:
        if module == ENGINE_PACKAGE and alias.name == "*":
            # Class 4a: the star import drags every re-exported name in at once, so it
            # is banned regardless of whether the caller ever uses the symbol.
            reasons.append("StarImport")
        elif alias.name in BANNED_SYMBOLS:
            # Class 4b: the re-export hole -- engine/__init__.py really does expose
            # these two names at the package root.
            reasons.append("ImportFrom-alias")
    return reasons


def _classify(node: ast.AST) -> list[str]:
    """Return every reason ``node`` violates the ban (usually zero or one)."""
    if isinstance(node, ast.Name) and node.id in BANNED_SYMBOLS:
        return ["Name"]
    if isinstance(node, ast.Attribute) and node.attr in BANNED_SYMBOLS:
        return ["Attribute"]
    if isinstance(node, ast.ImportFrom):
        return _import_from_reasons(node)
    if isinstance(node, ast.Call):
        name = _callee_name(node)
        if name in _DYNAMIC_CALLEES:
            for value in _string_arguments(node):
                if value in BANNED_SYMBOLS or value.endswith(BANNED_MODULE_SUFFIX):
                    return ["DynamicAccess"]
    return []


def scan_source(source: str, *, filename: str = "<memory>") -> list[str]:
    """Return one message per violation found in ``source``, ordered by line number."""
    tree = ast.parse(source, filename=filename)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        for why in _classify(node):
            hits.append((node.lineno, why))
    hits.sort()
    return [_MESSAGE.format(filename=filename, lineno=lineno, why=why) for lineno, why in hits]


def apps_py_files(apps_dir: Path) -> list[Path]:
    """Enumerate the scan surface, sorted so the output is stable.

    This is the single owner of the ``*.py`` pattern.  Callers that want to assert the
    surface is non-empty must ask this function, never roll their own ``rglob``: a test
    that counts files with its own pattern keeps passing after the scanner's pattern is
    narrowed, and a scanner that enumerates nothing reports no violations.
    """
    return sorted(apps_dir.rglob("*.py"))


def scan_apps_tree(apps_dir: Path) -> list[str]:
    """Scan every ``*.py`` below ``apps_dir``; sorted so the output is stable."""
    violations: list[str] = []
    for path in apps_py_files(apps_dir):
        source = path.read_bytes().decode("utf-8")
        violations.extend(scan_source(source, filename=path.as_posix()))
    return violations


# --- A group: one sample per node class, each must fire exactly once --------------


def test_a1_bare_name_is_caught() -> None:
    hits = scan_source("Q = AsyncReconciliationQueue()")
    assert len(hits) == 1, hits
    assert "(Name)" in hits[0]
    assert hits[0].startswith("FFW-ADR009-R1: <memory>:1 ")


def test_a2_attribute_is_caught() -> None:
    source = "import packages.core.engine as e\nK = e.PendingDelta"
    hits = scan_source(source)
    assert len(hits) == 1, hits
    assert "(Attribute)" in hits[0]
    assert ":2 " in hits[0]

    # Same class, second surface: the bare module import (no ``from``) is not itself one
    # of the five classes, but every route to a banned symbol through that alias is an
    # ``Attribute`` on the symbol name -- so the hole closes without a sixth class.
    bare = "import packages.core.engine.reconciliation\nK = reconciliation.PendingDelta"
    bare_hits = scan_source(bare)
    assert len(bare_hits) == 1, bare_hits
    assert "(Attribute)" in bare_hits[0]
    assert ":2 " in bare_hits[0]


def test_a3_importfrom_reconciliation_module_is_caught() -> None:
    # apply_feedback is NOT in BANNED_SYMBOLS on purpose: the ban is on the whole
    # module, which is why the statement is reported as ImportFrom and not by name.
    assert "apply_feedback" not in BANNED_SYMBOLS
    hits = scan_source("from packages.core.engine.reconciliation import apply_feedback")
    assert len(hits) == 1, hits
    assert "(ImportFrom)" in hits[0]


def test_a4_importfrom_engine_reexport_is_caught() -> None:
    # The vector the literal ADR wording misses: the module does not end with
    # "reconciliation", so only the alias-name rule can see it.
    hits = scan_source("from packages.core.engine import AsyncReconciliationQueue")
    assert len(hits) == 1, hits
    assert "(ImportFrom-alias)" in hits[0]


def test_a5_star_import_of_engine_is_caught() -> None:
    hits = scan_source("from packages.core.engine import *")
    assert len(hits) == 1, hits
    assert "(StarImport)" in hits[0]


def test_a6_dynamic_getattr_string_is_caught() -> None:
    source = "import packages.core.engine as e\nK = getattr(e, 'AsyncReconciliationQueue')"
    hits = scan_source(source)
    assert len(hits) == 1, hits
    assert "(DynamicAccess)" in hits[0]
    assert ":2 " in hits[0]


# --- B group: the scanner must not become "report everything" ---------------------


def test_b1_clean_apps_shaped_source_is_clean() -> None:
    # Verbatim shape of apps/composition/use_case.py:27-35 -- the engine package root
    # IS imported, and that is legal; only the two archived symbols are not.
    source = (
        "from packages.core.engine import (\n"
        "    assembler, commit, delay, dynamics, judge, routing, stimulus,\n"
        ")\n"
        "from packages.core.engine.arbiter import DictionaryArbiter\n"
        "from packages.core.engine.judge import HormoneDeltas\n"
        "from packages.core.engine.router import (\n"
        "    sanitize_tier_state,\n"
        "    settle_before_tier_upgrade,\n"
        ")\n"
        "from packages.core.engine.routing import MessageEnvelope\n"
    )
    assert scan_source(source) == []


def test_b2_unrelated_dynamic_import_is_clean() -> None:
    assert scan_source("import importlib\nm = importlib.import_module('json')") == []


def test_b3_scanner_is_discriminating_not_always_green_and_not_always_red() -> None:
    # One function, both directions: a judge that always returns [] passes B1/B2 and
    # a judge that always returns a message passes the A group. Only this pins both.
    dirty = scan_source("from packages.core.engine import PendingDelta")
    clean = scan_source("from packages.core.engine import commit")
    assert dirty != []
    assert clean == []


# --- C group: the protected tree is non-empty, and the archive survives -----------


def test_c1_real_apps_tree_has_zero_references() -> None:
    assert APPS_DIR.is_dir(), APPS_DIR
    # The denominator comes from the scanner's own enumerator.  Counting with a private
    # rglob here would let a narrowed scan surface pass: the test would still see 12
    # files while the scanner saw none (mutation M4 in the task card).
    files = apps_py_files(APPS_DIR)
    # A zero-width scan surface is the classic false green in this repo (five prior
    # incidents), so the denominator is asserted before the verdict is trusted.
    assert len(files) >= 12, len(files)
    assert scan_apps_tree(APPS_DIR) == []


def test_c2_archived_module_and_tests_survive() -> None:
    module = _REPO_ROOT / "packages" / "core" / "engine" / "reconciliation.py"
    tests = _REPO_ROOT / "tests" / "engine" / "test_reconciliation.py"
    config = _REPO_ROOT / "packages" / "core" / "domain" / "config.py"

    module_source = module.read_bytes().decode("utf-8")
    assert module.is_file() and len(module_source) > 0
    for symbol in [*sorted(BANNED_SYMBOLS), "apply_feedback"]:
        assert f"class {symbol}" in module_source or f"def {symbol}" in module_source

    tests_source = tests.read_bytes().decode("utf-8")
    assert tests_source.count("\ndef test_") >= 8

    assert "PLACEHOLDER_MAX_QUEUE_RETRIES: Final[int] = 3" in config.read_bytes().decode("utf-8")


def test_c3_banned_symbols_are_still_reexported_by_engine_init() -> None:
    # Deliberately counter-intuitive: this locks the *fact* that the package root
    # still exports both names. If someone deletes those re-exports, A4's vector
    # disappears -- and this assertion turns red, forcing A4 and the whole judge to be
    # re-evaluated instead of the attack surface quietly shrinking.
    # Imported here, not at module scope: the scanner module must stay importable from
    # any cwd (that is exactly the APPS_DIR anchoring lesson), and an engine import at
    # module scope makes loading this file depend on sys.path containing the project root.
    import packages.core.engine as engine_pkg

    exported = set(engine_pkg.__all__)
    for symbol in sorted(BANNED_SYMBOLS):
        assert symbol in exported
        assert hasattr(engine_pkg, symbol)
