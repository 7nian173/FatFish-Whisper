"""AC tests for EmotionUseCase (task-FFW-P3-G, plan-p3 section 4)."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from apps.composition.use_case import TX_MAX_RETRIES, EmotionUseCase
from packages.core.domain.contracts import MCPRequest, MCPRequestContext
from packages.core.domain.global_context import GlobalContext
from packages.core.domain.state import IPCState, S1Snapshot, TraitState
from packages.core.engine.router import COLD_NARRATION
from packages.core.ports import ObservabilityPort

_TS = datetime(2026, 11, 7, 12, 0, 0)

#: Repository root (the directory that contains ``apps/``).
#: Anchored on this file so the suite behaves the same no matter which
#: directory pytest is launched from -- the CI job runs ``pytest`` from
#: ``30_沙盒区/`` while developers run it from ``sys-mcp-whisper/``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APPS_DIR = _REPO_ROOT / "apps"
_USE_CASE_PATH = _APPS_DIR / "composition" / "use_case.py"


class RecordingObservability(ObservabilityPort):
    """Probe that records every payload it is handed."""

    def __init__(self) -> None:
        self.agent_run: list[dict[str, object]] = []
        self.token_usage: list[dict[str, object]] = []

    def emit_agent_run(self, payload: dict[str, object]) -> None:
        self.agent_run.append(dict(payload))

    def emit_token_usage(self, payload: dict[str, object]) -> None:
        self.token_usage.append(dict(payload))

    def check_token_budget(self, estimated_tokens: int) -> bool:
        return True


def _snapshot(version: int = 1, tier: str = "warm") -> S1Snapshot:
    return S1Snapshot(
        version=version,
        scope_id="scope-1",
        timestamp=_TS,
        last_decay_at=_TS,
        is_conflict=False,
        compute_tier=tier,
        ipc=IPCState(
            affiliation=10.0,
            dominance=0.0,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"tsundere": TraitState(score=85, visibility="unlocked")},
        emotions={},
    )


def _request(current_time: datetime | None = None, mode: str = "roleplay") -> MCPRequest:
    ct = current_time if current_time is not None else _TS
    return MCPRequest(
        request_id="uuid-probe-1",
        user_id="u-1",
        scope_id="scope-1",
        message="你真好",
        persona_card="",
        chat_mode=mode,
        context=MCPRequestContext(last_interaction_at=ct, current_time=ct),
    )


def _store(tmp_path: Path, preseed: S1Snapshot | None = None) -> SqliteStateStore:
    store = SqliteStateStore(tmp_path / "state.db")
    store.connect()
    if preseed is not None:
        with store.transaction() as tx:
            assert store.save(tx, preseed, 0, GlobalContext(), 0) is True
            tx.commit()
    return store


def _run(case: EmotionUseCase, request: MCPRequest) -> object:
    return asyncio.run(case.execute(request))


# -- G1: clamped clock (R2) ---------------------------------------------------


def test_g1_time_travel_is_clamped(tmp_path: Path) -> None:
    seed = _snapshot()
    store = _store(tmp_path, seed)
    case = EmotionUseCase(store, RecordingObservability())
    # forward run: message time is 1h after last_decay_at
    fwd = _run(case, _request(current_time=_TS + timedelta(hours=1)))
    # time-travel run: message time is 1h BEFORE last_decay_at -> clamped to +0h
    back = _run(case, _request(current_time=_TS - timedelta(hours=1)))
    snap_back = back.state_snapshot  # type: ignore[attr-defined]
    # Clamp held, no negative decay: the anchor is pinned to the *forward*
    # run's stamp (_TS + 1h), never rewound to the time-travelled _TS - 1h.
    assert snap_back.last_decay_at == _TS + timedelta(hours=1)
    # the forward response must exist and be a success
    assert fwd.status == "success"  # type: ignore[attr-defined]


# -- G2a / G2a-rev: cold tier still stacks hormones (rule A) ------------------


def _run_store(tmp_path: Path, tier: str) -> tuple[object, S1Snapshot]:
    seed = _snapshot(version=1, tier=tier)
    store = _store(tmp_path, seed)
    case = EmotionUseCase(store, RecordingObservability())
    resp = _run(case, _request(current_time=_TS))
    stored, _ = _read_stored(store)
    return resp, stored


def _read_stored(
    store: SqliteStateStore, scope_id: str = "scope-1"
) -> tuple[S1Snapshot, GlobalContext]:
    """Open a short-lived tx, read the pair, then roll it back."""
    with store.transaction() as tx:
        pair = store.get(tx, scope_id)
        tx.rollback()
    return pair


def test_g2a_cold_still_stacks_hormones(tmp_path: Path) -> None:
    seed = _snapshot(version=1, tier="cold")
    store = _store(tmp_path, seed)
    case = EmotionUseCase(store, RecordingObservability())
    # "滚" maps to stimulus_delta {pulse.cortisol: +2.0, ipc.affiliation:
    # -1.5}; a message with an empty delta would no-op and prove nothing.
    req = _request(current_time=_TS)
    resp = _run(case, req.model_copy(update={"message": "滚"}))
    cold_line = COLD_NARRATION[1:-1]  # strip brackets to avoid RUF001 markers
    assert resp.tail_message.find(cold_line) >= 0  # type: ignore[attr-defined]
    _, stored_ctx = _read_stored(store)
    pulse = stored_ctx.pulse
    assert pulse.dopamine != 50.0 or pulse.cortisol != 50.0


def test_g2a_rev_cold_and_warm_identical_delta(tmp_path: Path) -> None:
    results = {}
    for tier in ("cold", "warm"):
        seed = _snapshot(version=1, tier=tier)
        tier_dir = tmp_path / tier
        tier_dir.mkdir(parents=True, exist_ok=True)
        store = _store(tier_dir, seed)
        case = EmotionUseCase(store, RecordingObservability())
        req = _request(current_time=_TS)
        _run(case, req.model_copy(update={"message": "滚"}))
        _, stored_ctx = _read_stored(store)
        results[tier] = (
            stored_ctx.pulse.dopamine,
            stored_ctx.pulse.cortisol,
            stored_ctx.pulse.oxytocin,
        )
    assert results["cold"] == results["warm"]


# -- G2b / G2b-lock: verdict on the OLD snapshot (rule B) ----------------------


def test_g2b_conflict_uses_old_snapshot(tmp_path: Path) -> None:
    # Rule table probe: "哈哈 我难过" maps to {pulse.dopamine: +1.0,
    # pulse.cortisol: +1.5} which satisfies the dopamine-up+cortisol-up rule
    # (verdict on the deltas derived from the OLD snapshot), while "哈哈"
    # alone (dopamine only) must not conflict.
    seed = _snapshot(version=1, tier="warm")
    store = _store(tmp_path, seed)
    case = EmotionUseCase(store, RecordingObservability())
    req = _request(current_time=_TS)
    resp_both = _run(case, req.model_copy(update={"message": "哈哈 我难过"}))
    assert resp_both.state_snapshot.is_conflict is True  # type: ignore[attr-defined]
    store2_dir = tmp_path / "single"
    store2_dir.mkdir(parents=True, exist_ok=True)
    store2 = _store(store2_dir, _snapshot(version=1, tier="warm"))
    case2 = EmotionUseCase(store2, RecordingObservability())
    resp_single = _run(case2, req.model_copy(update={"message": "哈哈"}))
    assert resp_single.state_snapshot.is_conflict is False  # type: ignore[attr-defined]


def test_g2b_lock_no_in_place_assignment(tmp_path: Path) -> None:
    source = _USE_CASE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and isinstance(tgt.value, ast.Name)
                    and "snapshot" in tgt.value.id.lower()
                ):
                    bad.append(ast.dump(tgt))
    assert bad == []


# -- G2c: delay is fed the TRUNCATED tail length, not len(request.message) ----


def test_g2c_delay_fed_truncated_tail_length(tmp_path: Path) -> None:
    """M7 mutation (feed len(request.message)) must turn this red.

    The probe sends a 61-char message whose tail stays 77 chars; the two
    candidate delay sources differ, so an exact delay comparison discriminates.
    """
    from packages.core.engine.assembler import assemble_messages
    from packages.core.engine.delay import calculate_typing_delay

    seed = _snapshot(version=1, tier="warm")
    store = _store(tmp_path, seed)
    case = EmotionUseCase(store, RecordingObservability())
    long_msg = "滚" + "啊" * 60
    req = _request(current_time=_TS)
    resp = _run(case, req.model_copy(update={"message": long_msg}))
    with store.transaction() as tx:
        snap, ctx = store.get(tx, "scope-1")
        tx.rollback()
    _, direct_tail = assemble_messages(
        snap, pure=False, pulse=ctx.pulse, persona_card=req.persona_card
    )
    expected = calculate_typing_delay(
        ctx.pulse, snap.is_conflict, user_msg_len=len(direct_tail)
    )
    wrong = calculate_typing_delay(
        ctx.pulse, snap.is_conflict, user_msg_len=len(long_msg)
    )
    assert expected != wrong  # the two candidates must be distinguishable
    assert resp.typing_delay_ms == expected  # type: ignore[attr-defined]


# -- G3: optimistic lock exhausts retries -> dead letter, response intact -----


class _AlwaysFalseStore(SqliteStateStore):
    """CAS never succeeds; the UseCase must exhaust retries then dead-letter."""

    def save(self, tx, snapshot, expected_snapshot_version, context, expected_context_version):  # type: ignore[no-untyped-def]
        return False


def test_g3_cas_exhaustion_dead_letters_and_response_survives(tmp_path: Path) -> None:
    seed = _snapshot(version=1, tier="warm")
    store = _store(tmp_path, seed)
    fake = _AlwaysFalseStore.__new__(_AlwaysFalseStore)
    fake.__dict__.update(store.__dict__)
    case = EmotionUseCase(fake, RecordingObservability())
    resp = _run(case, _request(current_time=_TS))
    assert resp.status == "success"  # type: ignore[attr-defined]
    assert resp.system_head != "" and resp.tail_message != ""  # type: ignore[attr-defined]
    assert store.dead_letter_count() == 1
    assert case.pending_retry_count == TX_MAX_RETRIES
    # failed rounds must leave no half-written rows
    stored, ctx = _read_stored(store)
    assert stored.version == 1
    assert ctx.version == 0


# -- G4: first save fails, second succeeds -> normal return, no dead letter ---


class _FlakyStore(SqliteStateStore):
    """First CAS fails, the retry succeeds; no half rows may survive."""

    calls = 0

    def save(self, tx, snapshot, expected_snapshot_version, context, expected_context_version):  # type: ignore[no-untyped-def]
        _FlakyStore.calls += 1
        if _FlakyStore.calls == 1:
            return False
        return super().save(
            tx, snapshot, expected_snapshot_version, context, expected_context_version
        )


def test_g4_retry_recovers_without_dead_letter(tmp_path: Path) -> None:
    seed = _snapshot(version=1, tier="warm")
    store = _store(tmp_path, seed)
    _FlakyStore.calls = 0
    fake = _FlakyStore.__new__(_FlakyStore)
    fake.__dict__.update(store.__dict__)
    case = EmotionUseCase(fake, RecordingObservability())
    resp = _run(case, _request(current_time=_TS))
    assert resp.status == "success"  # type: ignore[attr-defined]
    assert case.pending_retry_count == 0
    assert store.dead_letter_count() == 0
    stored, _ = _read_stored(store)
    assert stored.version == 2  # the successful retry persisted version+1


# -- G5: observability payloads carry the shared request_id -------------------


def test_g5_observability_payloads_carry_request_id(tmp_path: Path) -> None:
    seed = _snapshot(version=1, tier="warm")
    store = _store(tmp_path, seed)
    obs = RecordingObservability()
    case = EmotionUseCase(store, obs)
    resp = _run(case, _request(current_time=_TS))
    assert len(obs.agent_run) >= 1 and len(obs.token_usage) >= 1
    assert obs.agent_run[-1]["request_id"] == resp.request_id  # type: ignore[attr-defined]
    assert obs.token_usage[-1]["request_id"] == resp.request_id  # type: ignore[attr-defined]


# -- G6: no contextvars / threading.local anywhere in apps/ -------------------


def test_g6_no_contextvars_or_threading_local_in_apps() -> None:
    hits = []
    for path in _APPS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "contextvars" in text or "threading.local" in text:
            hits.append(str(path))
    assert hits == []


# -- G7: TX_MAX_RETRIES is a Final module-level constant == 3 ------------------


def test_g7_retry_budget_is_final_module_constant() -> None:
    import apps.composition.use_case as uc

    assert uc.TX_MAX_RETRIES == 3
    assert str(uc.__annotations__["TX_MAX_RETRIES"]) in ("Final[int]", str(Final[int]))
    source = _USE_CASE_PATH.read_text(encoding="utf-8")
    assert "reconciliation" not in source


# -- G8: all three tiers execute without raising -------------------------------


def test_g8_all_three_tiers_execute(tmp_path: Path) -> None:
    for tier in ("cold", "warm", "hot"):
        seed = _snapshot(version=1, tier=tier)
        tier_dir = tmp_path / tier
        tier_dir.mkdir(parents=True, exist_ok=True)
        store = _store(tier_dir, seed)
        case = EmotionUseCase(store, RecordingObservability())
        resp = _run(case, _request(current_time=_TS))
        assert resp.status == "success"  # type: ignore[attr-defined]


# -- G9: response fields are byte-identical to a direct assemble_messages call -


def test_g9_response_matches_direct_assemble_messages(tmp_path: Path) -> None:
    from packages.core.engine.assembler import assemble_messages

    seed = _snapshot(version=1, tier="warm")
    store = _store(tmp_path, seed)
    case = EmotionUseCase(store, RecordingObservability())
    resp = _run(case, _request(current_time=_TS))
    stored, ctx = _read_stored(store)
    head, tail = assemble_messages(
        stored, pure=False, pulse=ctx.pulse, persona_card=None
    )
    assert resp.system_head == head  # type: ignore[attr-defined]
    assert resp.tail_message == tail  # type: ignore[attr-defined]


# -- G10: the placeholder markers are all present ------------------------------


def test_g10_placeholder_markers_present() -> None:
    source = _USE_CASE_PATH.read_text(encoding="utf-8")
    assert source.count("PLACEHOLDER-P4") >= 4


# -- G11: empty store cold start ------------------------------------------------


def test_g11_empty_store_cold_start(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "cold_start.db")
    store.connect()
    case = EmotionUseCase(store, RecordingObservability())
    resp = _run(case, _request(current_time=_TS))
    assert resp.status == "success"  # type: ignore[attr-defined]
    stored, ctx = _read_stored(store)
    assert stored.version == 2
    assert ctx.version == 1
    assert stored.last_decay_at == _TS  # clamped now seeded the fresh snapshot
