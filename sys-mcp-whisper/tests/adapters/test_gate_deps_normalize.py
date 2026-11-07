"""Regression locks for ``check_imports.normalize`` (dependency-name normalisation).

Why this file exists
--------------------
``normalize`` decides which distribution names count as "declared" for the
non-strict packages (``apps`` / ``ports`` / ``legacy_bridge``). It used to read

    re.split(r"[<>=!~;\\[\\s]", ...)

which is a character class of ``{backslash, `[`, `s`}`` -- not "whitespace" -- so
any name containing the letter ``s`` was chopped at that letter:

    "fastapi==0.162.0"   -> "fa"        (should be "fastapi")
    "starlette>=1.10"    -> ""          (should be "starlette")
    "sqlite-utils"       -> ""          (should be "sqlite_utils")

The bug shipped green because the only names the tree declared at the time were
``pydantic`` / ``uvicorn`` / ``httpx`` -- none of which contain an ``s`` before
their specifier. It surfaced when P3-H needed to ``import fastapi`` under
``apps/``: ``declared_deps`` returned ``{'fa', ...}`` so the gate reported
``apps ... :1 fastapi`` as an undeclared import and blocked the very dependency
the project declares.

The assertions below are written against the *behaviour* (name in, stripped name
out), not against the regex source text, so a different but correct
implementation keeps passing. ``test_normalize_is_not_vacuous`` is the guard
against the whole class collapsing into "returns something for everything".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_imports.py"
_SPEC = importlib.util.spec_from_file_location("check_imports", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GATE)

normalize = _GATE.normalize

#: name-with-specifier -> PEP 503 name. Every entry whose raw name contains an
#: ``s`` is a regression case for the character-class bug; the others pin the
#: specifier-stripping that already worked.
CASES = (
    ("pydantic>=2.0", "pydantic"),
    ("fastapi==0.162.0", "fastapi"),
    ("starlette>=1.10", "starlette"),
    ("starlette>=1.10,<2", "starlette"),
    ("uvicorn==0.54.0", "uvicorn"),
    ("uvicorn[standard]==0.54.0", "uvicorn"),
    ("httpx", "httpx"),
    ("typing-extensions", "typing_extensions"),
    ("zope.interface>=5", "zope_interface"),
    ("SQLAlchemy", "sqlalchemy"),
    ("sqlite-utils", "sqlite_utils"),
    ('dishka; python_version>="3.11"', "dishka"),
    ("  pydantic >= 2.0  ", "pydantic"),
    ("numpy~=1.26", "numpy"),
)


def test_normalize_strips_specifiers_and_keeps_names_containing_s() -> None:
    failures = [
        f"{raw!r} -> {normalize(raw)!r}, expected {expected!r}"
        for raw, expected in CASES
        if normalize(raw) != expected
    ]
    assert failures == []


def test_normalize_is_case_and_separator_insensitive() -> None:
    assert normalize("Typing-Extensions") == normalize("typing_extensions")
    assert normalize("zope.interface") == normalize("zope-interface")


def test_normalize_is_not_vacuous() -> None:
    """A guard that returned ``""`` for everything would also pass a naive test."""
    results = {normalize(raw) for raw, _ in CASES}
    assert "" not in results
    assert len(results) >= 10


def test_declared_deps_reads_the_real_pyproject_and_normalizes_each_name() -> None:
    """End-to-end: the names the gate will actually compare against imports.

    ``declared_deps`` walks ancestors from the package directory, so pointing it
    at ``apps/`` exercises the same path the gate uses in production.
    """
    apps_dir = Path(__file__).resolve().parents[2] / "apps"
    declared = _GATE.declared_deps(apps_dir)
    assert "pydantic" in declared
    # Every entry must be a bare PEP 503 name: no specifier, no extras bracket,
    # no leftover fragment from a bad split.
    for name in declared:
        assert name == normalize(name), f"{name!r} is not a normalized name"
        assert all(ch.isalnum() or ch == "_" for ch in name), f"{name!r} has junk"
