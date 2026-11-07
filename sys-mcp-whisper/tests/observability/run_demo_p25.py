"""P2.5 end-to-end demo - heat, tier gate, cross-domain merge, assembly, delay.

Run it directly. The file name has no ``test_`` prefix so pytest never
collects it; it needs nothing but the standard library and ``packages.core``:

    python tests/observability/run_demo_p25.py

Every step prints the value it measured and asserts that value immediately,
so a wrong number exits non-zero instead of printing a pretty lie. There is
no try/except around the assertions and no skipped branch: an assertion that
cannot fail would be worse than no assertion at all.

The four acts follow the P2.5 data flow, one act per layer boundary:

  1. heat -> tier        pure arithmetic, nine table cells plus four boundaries
  2. tier -> gate        should_call_track_b and sanitize_tier_state
  3. cross-domain merge  same instant (no decay) and 24h later (decayed)
  4. assembly -> delay   cold short-circuit, truncation, and the delay it causes
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

TS = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
SEAL_PREFIX = "【输出格式封印】"


def _act(index: int, title: str) -> None:
    """Print an act banner."""
    print()
    print(f"===== Act {index} - {title} =====")


def _body_lines(prompt: str, *, pure: bool) -> list[str]:
    """Return the injection body lines, dropping the label line and the seal."""
    lines = prompt.split("\n")
    body = lines[1:]
    if pure and body and body[-1].startswith(SEAL_PREFIX):
        body = body[:-1]
    return body


def _body_len(prompt: str, *, pure: bool) -> int:
    """Body length in code points, marker lines excluded."""
    return sum(len(line) for line in _body_lines(prompt, pure=pure))


def _close(actual: float, expected: float) -> bool:
    """Float equality with a tolerance small enough to still catch drift."""
    return abs(actual - expected) < 1e-9


def main() -> int:
    """Run the four acts; raise on the first number that does not match."""
    from packages.core.domain.global_context import PulseState
    from packages.core.domain.state import (
        EmotionValue,
        IPCState,
        S1Snapshot,
        TraitState,
    )
    from packages.core.engine.assembler import MAX_INJECTION_BODY_CHARS, assemble_prompt
    from packages.core.engine.delay import calculate_typing_delay
    from packages.core.engine.dynamics import apply_time_decay
    from packages.core.engine.router import (
        COLD_NARRATION,
        calculate_heat,
        evaluate_compute_tier,
        merge_cross_domain_state,
        sanitize_tier_state,
        should_call_track_b,
    )

    def make_snapshot(
        tier: str,
        *,
        affiliation: float = 0.0,
        is_conflict: bool = False,
        last_decay_at: datetime = TS,
    ) -> S1Snapshot:
        """Build a fully populated S1Snapshot; every field is required."""
        return S1Snapshot(
            version=1,
            scope_id="group_8842",
            timestamp=last_decay_at,
            last_decay_at=last_decay_at,
            is_conflict=is_conflict,
            compute_tier=tier,
            ipc=IPCState(
                affiliation=affiliation,
                dominance=0.0,
                relationship_stage="friend",
                relationship_ceiling="lover",
            ),
            traits={
                "tsundere": TraitState(score=85, visibility="unlocked"),
                "shyness": TraitState(score=30, visibility="unlocked"),
            },
            emotions={"joy": EmotionValue(intensity=20.0, ttl_expires_at=TTL)},
        )

    # ------------------------------------------------------------------
    # Act 1 - heat table and tier boundaries
    # ------------------------------------------------------------------
    _act(1, "heat -> tier")

    table = [
        ((0, 0, 0), 0.0, "cold"),
        ((2, 0, 0), 8.0, "cold"),
        ((3, 0, 0), 12.0, "warm"),
        ((0, 1, 0), 12.0, "warm"),
        ((0, 3, 0), 37.0, "warm"),
        ((0, 4, 0), 50.0, "hot"),
        ((10, 0, 5), 42.0, "warm"),
        ((10, 2, 10), 70.0, "hot"),
        ((5, 1, 3), 34.0, "warm"),
    ]
    for (msg, mention, turn), expected_heat, expected_tier in table:
        heat = calculate_heat(msg, mention, turn)
        tier = evaluate_compute_tier(heat)
        print(f"[1] calculate_heat({msg}, {mention}, {turn}) = {heat} -> tier {tier}")
        assert isinstance(heat, float), f"heat must be float, got {type(heat)!r}"
        assert _close(heat, expected_heat), f"heat {heat} != {expected_heat}"
        assert tier == expected_tier, f"tier {tier} != {expected_tier}"

    for heat, expected_tier in ((9.0, "cold"), (10.0, "warm"), (50.0, "hot"), (51.0, "hot")):
        tier = evaluate_compute_tier(heat)
        print(f"[1] evaluate_compute_tier({heat}) = {tier}")
        assert tier == expected_tier, f"boundary {heat} -> {tier} != {expected_tier}"

    saturations = [
        ((100, 0, 0), 40.0, "warm"),
        ((0, 100, 0), 50.0, "hot"),
        ((0, 0, 100), 10.0, "warm"),
    ]
    for (msg, mention, turn), expected_heat, expected_tier in saturations:
        heat = calculate_heat(msg, mention, turn)
        tier = evaluate_compute_tier(heat)
        print(f"[1] saturation {msg}/{mention}/{turn} = {heat} -> {tier}")
        assert _close(heat, expected_heat), f"saturation heat {heat} != {expected_heat}"
        assert tier == expected_tier, f"saturation tier {tier} != {expected_tier}"

    # ------------------------------------------------------------------
    # Act 2 - tier gate
    # ------------------------------------------------------------------
    _act(2, "tier -> gate")

    for tier, expected in (("hot", True), ("warm", False), ("cold", False)):
        allowed = should_call_track_b(tier)
        print(f"[2] should_call_track_b({tier!r}) = {allowed}")
        assert allowed is expected, f"track B for {tier!r} -> {allowed} != {expected}"

    for bad in ("Cold", "warmm", "", "hot "):
        try:
            got = should_call_track_b(bad)
        except ValueError as exc:
            print(f"[2] should_call_track_b({bad!r}) raised ValueError - {exc}")
        else:
            raise AssertionError(f"should_call_track_b({bad!r}) returned {got} instead of raising")

    warm_snap = make_snapshot("warm", is_conflict=True)
    same = sanitize_tier_state(warm_snap)
    print(f"[2] sanitize_tier_state(warm) is input = {same is warm_snap}")
    assert same is warm_snap, "a warm snapshot must pass through as the same object"
    assert same.is_conflict is True, "warm keeps is_conflict"

    cold_conflicted = make_snapshot("cold", is_conflict=True)
    sanitized = sanitize_tier_state(cold_conflicted)
    print(f"[2] sanitize_tier_state(cold is_conflict=True) -> {sanitized.is_conflict}")
    assert sanitized.is_conflict is False, "cold must force is_conflict False"
    assert sanitized is not cold_conflicted, "cold must return a copy, not the input"
    assert cold_conflicted.is_conflict is True, "the input snapshot must stay untouched"

    # ------------------------------------------------------------------
    # Act 3 - cross-domain merge
    # ------------------------------------------------------------------
    _act(3, "cross-domain merge")

    # ADR-008 section 1.6: merge stopped blending hormones.  Under the old
    # contract this act asserted 52.0 (delta_t=0) and 44.8 (delta_t=24h) for
    # merged dopamine; both are now deliberately absent, and the assertions
    # below pin them as *not* happening so a future regression is visible.
    # The pulse is a second carrier since ADR-008 section 1.7: every call now
    # returns a (snapshot, pulse) pair and both halves are asserted.
    hot_lda = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    cold_snap = make_snapshot("cold", affiliation=-10.0, last_decay_at=hot_lda)
    hot_snap = make_snapshot("hot", affiliation=30.0, last_decay_at=hot_lda)
    cold_pulse = PulseState(dopamine=40.0)
    hot_pulse = PulseState(dopamine=80.0)

    merged_now, merged_now_pulse = merge_cross_domain_state(
        cold_snap, cold_pulse, hot_snap, hot_pulse, now=hot_lda
    )
    print(
        f"[3] delta_t=0 cold.dopamine={cold_pulse.dopamine} "
        f"hot.dopamine={hot_pulse.dopamine} -> {merged_now_pulse.dopamine}"
    )
    assert _close(merged_now_pulse.dopamine, 40.0), (
        f"the cold side's pulse must come back verbatim (40.0), "
        f"got {merged_now_pulse.dopamine}"
    )
    assert merged_now_pulse.dopamine != 52.0, (
        "52.0 is the OLD blended value (0.7*40.0 + 0.3*80.0); merge must not blend "
        "hormones any more (ADR-008 section 1.6)"
    )
    print(
        f"[3] delta_t=0 cold.affiliation={cold_snap.ipc.affiliation} "
        f"hot.affiliation={hot_snap.ipc.affiliation} -> {merged_now.ipc.affiliation}"
    )
    assert _close(merged_now.ipc.affiliation, 2.0), (
        f"0.7*(-10.0) + 0.3*30.0 = 2.0, got {merged_now.ipc.affiliation}"
    )
    assert merged_now.compute_tier == "cold", "the merged result keeps the cold side tier"

    now_24h = hot_lda + timedelta(hours=24)
    decayed_hot, decayed_hot_pulse = apply_time_decay(hot_snap, hot_pulse, timedelta(hours=24))
    print(
        f"[3] hot side after 24h decay dopamine={decayed_hot_pulse.dopamine} "
        f"affiliation={decayed_hot.ipc.affiliation}"
    )
    assert _close(decayed_hot_pulse.dopamine, 56.0), (
        f"80.0 decays 1.0/h towards 50 for 24h -> 56.0, got {decayed_hot_pulse.dopamine}"
    )
    assert _close(decayed_hot.ipc.affiliation, 18.0), (
        f"30.0 decays 0.5/h towards 0 for 24h -> 18.0, got {decayed_hot.ipc.affiliation}"
    )

    merged_24h, merged_24h_pulse = merge_cross_domain_state(
        cold_snap, cold_pulse, hot_snap, hot_pulse, now=now_24h
    )
    print(
        f"[3] delta_t=24h merged.dopamine={merged_24h_pulse.dopamine} "
        f"(the old blended value was 44.8)"
    )
    assert _close(merged_24h_pulse.dopamine, 40.0), (
        f"the cold side's pulse must come back verbatim (40.0), "
        f"got {merged_24h_pulse.dopamine}"
    )
    assert merged_24h_pulse.dopamine != 44.8, (
        "44.8 is the OLD blended value (0.7*40.0 + 0.3*56.0); merge must not blend "
        "hormones any more (ADR-008 section 1.6)"
    )
    print(
        f"[3] delta_t=24h merged.affiliation={merged_24h.ipc.affiliation} "
        f"(0.7*(-10.0) + 0.3*18.0)"
    )
    assert _close(merged_24h.ipc.affiliation, -1.6), (
        f"0.7*(-10.0) + 0.3*18.0 = -1.6, got {merged_24h.ipc.affiliation}"
    )
    assert merged_24h.last_decay_at == now_24h, "the merge stamps last_decay_at with now"
    assert cold_pulse.dopamine == 40.0, "the cold input pulse stays unmodified"
    assert hot_pulse.dopamine == 80.0, "the hot input pulse stays unmodified"
    assert cold_snap.ipc.affiliation == -10.0, "the cold input snapshot stays unmodified"
    assert hot_snap.ipc.affiliation == 30.0, "the hot input snapshot stays unmodified"

    # ------------------------------------------------------------------
    # Act 4 - assembly and delay
    # ------------------------------------------------------------------
    _act(4, "assembly -> delay")

    cold_prompt = assemble_prompt(make_snapshot("cold"))
    print(f"[4] cold tier prompt length = {len(cold_prompt)}")
    print(f"[4] cold tier prompt equals COLD_NARRATION = {cold_prompt == COLD_NARRATION}")
    assert cold_prompt == COLD_NARRATION, "the cold tier short-circuits to the fixed narration"
    assert len(COLD_NARRATION) == 28, (
        f"COLD_NARRATION must stay 28 chars, got {len(COLD_NARRATION)}"
    )

    calm = make_snapshot("warm", is_conflict=False)
    calm_pure = assemble_prompt(calm, pure=True)
    calm_host = assemble_prompt(calm, pure=False)
    calm_pure_len = _body_len(calm_pure, pure=True)
    calm_host_len = _body_len(calm_host, pure=False)
    print(f"[4] warm no-conflict body pure=True={calm_pure_len} pure=False={calm_host_len}")
    assert calm_pure_len == 39, f"warm no-conflict body should be 39, got {calm_pure_len}"
    assert calm_host_len == calm_pure_len, "a short body is not truncated at all"

    conflicted = make_snapshot("warm", is_conflict=True)
    conflict_pure = assemble_prompt(conflicted, pure=True)
    conflict_host = assemble_prompt(conflicted, pure=False)
    conflict_pure_len = _body_len(conflict_pure, pure=True)
    conflict_host_len = _body_len(conflict_host, pure=False)
    print(f"[4] warm conflict body pure=True={conflict_pure_len} pure=False={conflict_host_len}")
    print(f"[4] cap = {MAX_INJECTION_BODY_CHARS} code points")
    assert conflict_pure_len == 59, (
        f"conflict body before truncation should be 59, got {conflict_pure_len}"
    )
    assert conflict_host_len == 44, (
        f"conflict body after truncation should be 44, got {conflict_host_len}"
    )
    assert conflict_host_len <= MAX_INJECTION_BODY_CHARS, "the truncated body must respect the cap"
    assert len(_body_lines(conflict_host, pure=False)) == 2, "truncation drops a whole line"

    three = [
        ("furious", PulseState(dopamine=30.0, cortisol=90.0, oxytocin=50.0), 500, 500),
        ("baseline", PulseState(dopamine=50.0, cortisol=50.0, oxytocin=50.0), 500, 650),
        ("shy", PulseState(dopamine=50.0, cortisol=60.0, oxytocin=80.0), 1000, 1300),
    ]
    for label, hormones, expected_calm, expected_conflict in three:
        calm_ms = calculate_typing_delay(hormones, False, 10)
        conflict_ms = calculate_typing_delay(hormones, True, 10)
        print(
            f"[4] delay {label:9s} dopamine={hormones.dopamine} "
            f"cortisol={hormones.cortisol} oxytocin={hormones.oxytocin} "
            f"-> calm={calm_ms} conflict={conflict_ms}"
        )
        assert calm_ms == expected_calm, f"{label} calm delay {calm_ms} != {expected_calm}"
        assert conflict_ms == expected_conflict, (
            f"{label} conflict delay {conflict_ms} != {expected_conflict}"
        )

    base_hormones = PulseState(dopamine=50.0, cortisol=50.0, oxytocin=50.0)
    before_ms = calculate_typing_delay(base_hormones, True, conflict_pure_len)
    after_ms = calculate_typing_delay(base_hormones, True, conflict_host_len)
    print(
        f"[4] order matters: delay(len {conflict_pure_len}) = {before_ms} vs "
        f"delay(len {conflict_host_len}) = {after_ms}"
    )
    assert before_ms == 1924, f"300 + 20*59 = 1480, times 1.3 = 1924, got {before_ms}"
    assert after_ms == 1534, f"300 + 20*44 = 1180, times 1.3 = 1534, got {after_ms}"
    assert before_ms - after_ms == 390, (
        f"the truncation is worth 390ms here, got {before_ms - after_ms}"
    )

    print()
    print("ALL ACTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
