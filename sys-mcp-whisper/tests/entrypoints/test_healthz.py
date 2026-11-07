"""AC tests for the /healthz surface (task-FFW-P3-H, plan-p3 section 3).

Every case drives the real ``SqliteStateStore`` through a real ``TestClient`` on
purpose: the headline fact behind this card is that the store's connection
cannot cross threads, so a mocked store would exercise the mock instead of the
constraint that forces the read-only probe.

The ten cases below are exactly H1-H10 from the card; no extras, so that the
suite-count delta stays attributable.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from apps.composition.use_case import EmotionUseCase
from apps.entrypoints.healthz import create_healthz_app
from packages.core.domain.global_context import GlobalContext
from packages.core.domain.state import IPCState, S1Snapshot
from packages.core.ports import ObservabilityPort

_TS = datetime(2026, 11, 7, 12, 0, 0)

#: Repository root: the directory that contains ``apps/``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTHZ_PATH = _REPO_ROOT / "apps" / "entrypoints" / "healthz.py"


class RecordingObservability(ObservabilityPort):
    """Minimal port implementation; the health surface must never call it."""

    def __init__(self) -> None:
        self.agent_run: list[dict[str, object]] = []
        self.token_usage: list[dict[str, object]] = []

    def emit_agent_run(self, payload: dict[str, object]) -> None:
        self.agent_run.append(dict(payload))

    def emit_token_usage(self, payload: dict[str, object]) -> None:
        self.token_usage.append(dict(payload))

    def check_token_budget(self, estimated_tokens: int) -> bool:
        return True


def _snapshot(version: int = 7) -> S1Snapshot:
    return S1Snapshot(
        version=version,
        scope_id="scope-1",
        timestamp=_TS,
        last_decay_at=_TS,
        is_conflict=False,
        compute_tier="warm",
        ipc=IPCState(
            affiliation=10.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={},
        emotions={},
    )


def _seeded_store(tmp_path: Path, *, context_version: int = 5) -> SqliteStateStore:
    """Healthy database: one global_context row and one s1_snapshot row."""
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    with store.transaction() as tx:
        saved = store.save(tx, _snapshot(version=7), 0, GlobalContext(version=context_version), 0)
        assert saved is True
        tx.commit()
    return store


def _make(store: SqliteStateStore) -> tuple[TestClient, EmotionUseCase]:
    usecase = EmotionUseCase(store, RecordingObservability())
    return TestClient(create_healthz_app(store, usecase)), usecase


def _get(store: SqliteStateStore) -> httpx.Response:
    client, _ = _make(store)
    return client.get("/healthz")


# -- H1: healthy database -----------------------------------------------------


def test_h1_healthy_db_reports_context_version_and_pulse(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    try:
        response = _get(store)
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    # The snapshot row holds version 7, the context row holds version 5: the
    # reported number must be the context one, because pulse belongs to it.
    assert body["version"] == 5
    assert body["pulse"] == {
        "dopamine": 50.0,
        "cortisol": 50.0,
        "oxytocin": 50.0,
        "delayed_impact": {},
    }
    assert body["pulse_source"] == "global_context"
    assert body["database"] == {"connected": True, "detail": None}
    assert set(body["queue_backlog"]) == {
        "dead_letter_count",
        "pending_retry_count",
        "legacy_queue_pending",
    }


# -- H2: empty database (tables exist, no rows) -------------------------------


def test_h2_empty_db_reports_version_zero_and_no_pulse(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "empty.db")
    store.connect()
    try:
        response = _get(store)
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 0
    assert body["pulse"] is None
    assert body["pulse_source"] == "unavailable"
    assert body["database"]["connected"] is True


# -- H3: database file absent -------------------------------------------------


def test_h3_missing_db_file_still_answers_200(tmp_path: Path) -> None:
    db_path = tmp_path / "nope.db"
    store = SqliteStateStore(db_path)  # never connected: only _path is used
    try:
        response = _get(store)
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["database"]["connected"] is False
    assert "unable to open database file" in body["database"]["detail"]
    assert body["pulse"] is None
    assert body["pulse_source"] == "unavailable"
    # A read-only URI never creates the file -- that is why it beats a normal
    # connection for a diagnostic probe.
    assert db_path.exists() is False


# -- H4: database file is garbage --------------------------------------------


def test_h4_corrupt_db_file_still_answers_200(tmp_path: Path) -> None:
    db_path = tmp_path / "garbage.db"
    db_path.write_bytes(b"not a database")
    store = SqliteStateStore(db_path)
    try:
        response = _get(store)
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["database"]["connected"] is False
    assert "file is not a database" in body["database"]["detail"]
    assert body["pulse"] is None


# -- H5/H6: queue backlog -----------------------------------------------------


def test_h5_pending_retry_count_is_read_from_the_usecase(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    try:
        client, usecase = _make(store)
        usecase.pending_retry_count = 2
        before = client.get("/healthz").json()["queue_backlog"]["pending_retry_count"]
        usecase.pending_retry_count = 0
        after = client.get("/healthz").json()["queue_backlog"]["pending_retry_count"]
    finally:
        store.close()

    # Reading it twice around a mutation proves the number is not a constant.
    assert before == 2
    assert after == 0


def test_h6_legacy_queue_pending_is_always_zero(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    try:
        body = _get(store).json()
    finally:
        store.close()

    assert body["queue_backlog"]["legacy_queue_pending"] == 0


def test_h7_dead_letter_count_reads_the_dlq_table(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    try:
        with store.transaction() as tx:
            store.record_dead_letter(tx, "req-0", 5, 7, "conflict", _TS)
            store.record_dead_letter(tx, "req-1", 5, 7, "conflict", _TS)
            tx.commit()
        body = _get(store).json()
    finally:
        store.close()

    assert body["queue_backlog"]["dead_letter_count"] == 2


# -- H8: no authentication ----------------------------------------------------


def test_h8_healthz_needs_no_credentials(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    try:
        client, _ = _make(store)
        response = client.get("/healthz")  # no headers, no token, no query
    finally:
        store.close()

    assert response.status_code == 200
    assert "www-authenticate" not in response.headers


# -- H9: the probe must not write --------------------------------------------


def test_h9_healthz_does_not_touch_the_database_bytes(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    db_path = tmp_path / "state.db"
    try:
        before = db_path.read_bytes()
        before_sha = hashlib.sha256(before).hexdigest()
        assert _get(store).status_code == 200
        after = db_path.read_bytes()
    finally:
        store.close()

    assert len(after) == len(before)
    assert after == before
    assert hashlib.sha256(after).hexdigest() == before_sha


# -- H10: the surface stays out of the engine --------------------------------


def test_h10_healthz_never_imports_the_engine_or_the_legacy_bridge() -> None:
    tree = ast.parse(_HEALTHZ_PATH.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)

    assert [m for m in modules if m.startswith("packages.core.engine")] == []
    assert [m for m in modules if m.startswith("packages.legacy_bridge")] == []
