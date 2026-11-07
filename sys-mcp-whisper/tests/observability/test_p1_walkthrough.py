# ruff: noqa: RUF001, RUF002, RUF003
"""P1 端到端可观测脚本（SOP 附录 A.4.4 人类视角可观测脚本，不是单元测试的替代品）。

本脚本端到端走一遍 P1 主链路（domain 契约 + engine 动力学）的三条核心路径：
  1. 时间衰减（激素向基线 50 回归、好感度向 0 衰减、delta_t=0 不衰减、极大 delta_t 截断在基线）
  2. 三级限幅（单次 ±1 / 2 小时窗 ±3 / 单日 ±4）
  3. 快照往返（构造 → 衰减 → 取激素值 → model_copy 回写，九字段完整 + last_decay_at 推进）

每一步都用 `print()` 打印 `[步骤名] 字段=值`，让人肉眼能看懂数值流转；
同时每一步都必须 `assert`（不允许只 print 不断言）——数值与 plan-dynamics-engine.md §2.1 手算段一致。

运行方式（抓取真实 stdout）：
    python -m pytest tests/observability -s -v
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from packages.core.domain.global_context import PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.dynamics import (
    DayAccumulator,
    DecayRates,
    StimulusRecord,
    apply_limit_cap,
    apply_time_decay,
)

# 为便于手算对齐 plan-dynamics-engine.md §2.1 自测段：激素与好感度速率统一取 1.0 单位/小时。
RATES = DecayRates(
    hormone_per_hour={"dopamine": 1.0, "cortisol": 1.0, "oxytocin": 1.0},
    affiliation_per_hour=1.0,
)

TS = datetime(2026, 11, 4, 12, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {"joy": EmotionValue(intensity=20.0, ttl_expires_at=TTL)}


def make_snapshot(
    *,
    affiliation: float = 0.0,
    dominance: float = 0.0,
    is_conflict: bool = False,
) -> S1Snapshot:
    """构造一个合法的 S1Snapshot（P2.8 起激素不在快照上，见 ADR-008 §1.7）。"""
    return S1Snapshot(
        version=1,
        scope_id="group_8888",
        timestamp=TS,
        last_decay_at=datetime(2026, 11, 4, 10, 0, 0),
        is_conflict=is_conflict,
        ipc=IPCState(
            affiliation=affiliation,
            dominance=dominance,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"warmth": TraitState(score=50, visibility="unlocked")},
        emotions=EMOTIONS,
    )


def make_pulse(
    *,
    dopamine: float = 50.0,
    cortisol: float = 50.0,
    oxytocin: float = 50.0,
) -> PulseState:
    """构造激素载体 GlobalContext.pulse（P2.8 起三条激素道住在这里）。"""
    return PulseState(dopamine=dopamine, cortisol=cortisol, oxytocin=oxytocin)


# ---------------------------------------------------------------------------
# 链路 1：时间衰减
# ---------------------------------------------------------------------------


def test_walkthrough_decay() -> None:
    """激素向 50 回归、好感度向 0 衰减、delta_t=0 不衰减、极大 delta_t 停在基线。

    P2.8 起 apply_time_decay 返回 (快照, 激素载体) 两个值：三条激素道在
    GlobalContext.pulse 上衰减，ipc.affiliation 仍在快照上衰减。
    """
    print("\n===== 链路 1：时间衰减 apply_time_decay =====")

    snap = make_snapshot(affiliation=10.0, dominance=20.0)
    pulse = make_pulse(dopamine=60.0, cortisol=55.0, oxytocin=10.0)
    print(
        "[输入] pulse.dopamine=60.0 pulse.cortisol=55.0 pulse.oxytocin=10.0 "
        "affiliation=10.0 dominance=20.0"
    )

    # 1 小时衰减：激素向 50、好感度向 0 各移动 1.0。
    one_hour, one_hour_pulse = apply_time_decay(snap, pulse, timedelta(hours=1), RATES)
    print("[衰减 1h] dopamine=59.0 cortisol=54.0 oxytocin=11.0 affiliation=9.0 dominance=20.0")
    assert one_hour_pulse.dopamine == pytest.approx(59.0)
    assert one_hour_pulse.cortisol == pytest.approx(54.0)
    assert one_hour_pulse.oxytocin == pytest.approx(11.0)
    assert one_hour.ipc.affiliation == pytest.approx(9.0)
    # dominance 不在契约 4.1 衰减清单，透传。
    assert one_hour.ipc.dominance == pytest.approx(20.0)

    # delta_t = 0：返回与输入逐字段相等的深拷贝，不抛异常。
    zero, zero_pulse = apply_time_decay(snap, pulse, timedelta(0), RATES)
    print("[衰减 0h] dopamine=60.0 cortisol=55.0 oxytocin=10.0 affiliation=10.0（原样）")
    assert zero.model_dump() == snap.model_dump()
    assert zero_pulse.model_dump() == pulse.model_dump()
    assert zero is not snap
    assert zero_pulse is not pulse

    # 极大 delta_t：截断在基线，不穿过。
    huge, huge_pulse = apply_time_decay(
        make_snapshot(affiliation=-50.0),
        make_pulse(dopamine=45.5, oxytocin=100.0),
        timedelta(hours=1_000_000),
        RATES,
    )
    print("[衰减 10^6h] dopamine=50.0 oxytocin=50.0 affiliation=0.0（截断在基线，不穿过）")
    assert huge_pulse.dopamine == pytest.approx(50.0)
    assert huge_pulse.oxytocin == pytest.approx(50.0)
    assert huge.ipc.affiliation == pytest.approx(0.0)

    # 与 plan-dynamics-engine.md §2.1 手算段逐项对齐（速率 1.0/h）。
    _, up_pulse = apply_time_decay(
        make_snapshot(), make_pulse(dopamine=45.5), timedelta(hours=1), RATES
    )
    print("[手算对齐 §2.1] dopamine 45.5、1h → 46.5（向 50 上行）")
    assert up_pulse.dopamine == pytest.approx(46.5)

    _, down_pulse = apply_time_decay(
        make_snapshot(), make_pulse(dopamine=55.0), timedelta(hours=1), RATES
    )
    print("[手算对齐 §2.1] dopamine 55.0、1h → 54.0（向 50 下行）")
    assert down_pulse.dopamine == pytest.approx(54.0)

    aff_up, _ = apply_time_decay(
        make_snapshot(affiliation=-50.0), make_pulse(), timedelta(hours=1), RATES
    )
    print("[手算对齐 §2.1] affiliation -50.0、1h → -49.0（向 0 上行）")
    assert aff_up.ipc.affiliation == pytest.approx(-49.0)

    aff_huge, _ = apply_time_decay(
        make_snapshot(affiliation=60.0), make_pulse(), timedelta(hours=1_000_000), RATES
    )
    print("[手算对齐 §2.1] affiliation 60.0、10^6h → 0.0")
    assert aff_huge.ipc.affiliation == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 链路 2：三级限幅
# ---------------------------------------------------------------------------


def test_walkthrough_limit_cap() -> None:
    """单次 ±1 / 2 小时窗 ±3 / 单日 ±4 三层限幅，打印输入 → 各层剩余额度 → 最终返回值。"""
    print("\n===== 链路 2：三级限幅 apply_limit_cap =====")
    empty: list[StimulusRecord] = []
    zero = DayAccumulator(pos=0.0, neg=0.0)

    # 第 1 层：单次 ±1。
    print("[第 1 层 单次 ±1] 输入 +5.0 → 剩余额度 up=1.0 down=-1.0 → 返回 1.0")
    assert apply_limit_cap(5.0, empty, TS, zero) == pytest.approx(1.0)
    print("[第 1 层 单次 ±1] 输入 -5.0 → 剩余额度 up=1.0 down=-1.0 → 返回 -1.0")
    assert apply_limit_cap(-5.0, empty, TS, zero) == pytest.approx(-1.0)

    # 与 plan-dynamics-engine.md §2.3 限幅自测表四输入逐项对齐。
    print("[手算对齐 §2.3] 输入 +1.0 → 1.0（第 1 层原样通过）")
    assert apply_limit_cap(1.0, empty, TS, zero) == pytest.approx(1.0)
    print("[手算对齐 §2.3] 输入 -1.0 → -1.0（负向对称，额度不抬升）")
    assert apply_limit_cap(-1.0, empty, TS, zero) == pytest.approx(-1.0)

    # 第 2 层：滑动 2 小时窗 ±3（窗内已用 pos=2.0，正向剩余 1.0）。
    history = [
        StimulusRecord(at=TS - timedelta(minutes=30), delta=1.0),
        StimulusRecord(at=TS - timedelta(minutes=10), delta=1.0),
    ]
    acc = DayAccumulator(pos=2.0, neg=0.0)
    out = apply_limit_cap(1.5, history, TS, acc)
    print("[第 2 层 2h 窗 ±3] 窗内 pos=2.0 输入 +1.5 → 剩余额度 up=1.0 → 返回 1.0")
    assert out == pytest.approx(1.0)

    # 第 2 层额度耗尽：窗内 pos=3.0，再申请 +1.0 → 压到 0。
    exhausted = [
        StimulusRecord(at=TS - timedelta(minutes=30), delta=1.0),
        StimulusRecord(at=TS - timedelta(minutes=20), delta=1.0),
        StimulusRecord(at=TS - timedelta(minutes=10), delta=1.0),
    ]
    out_exhausted = apply_limit_cap(1.0, exhausted, TS, DayAccumulator(pos=3.0, neg=0.0))
    print("[第 2 层 2h 窗 ±3] 窗内 pos=3.0 输入 +1.0 → 剩余额度 up=0.0 → 返回 0.0")
    assert out_exhausted == pytest.approx(0.0)

    # 第 3 层：单日 ±4（当日已用 pos=3.5，剩余 0.5）。
    daily = apply_limit_cap(1.0, empty, TS, DayAccumulator(pos=3.5, neg=0.0))
    print("[第 3 层 单日 ±4] 当日 pos=3.5 输入 +1.0 → 剩余额度 up=0.5 → 返回 0.5")
    assert daily == pytest.approx(0.5)

    # 第 3 层负向耗尽：当日 neg=-4.0，再申请 -1.0 → 压到 0。
    daily_neg = apply_limit_cap(-1.0, empty, TS, DayAccumulator(pos=0.0, neg=-4.0))
    print("[第 3 层 单日 ±4] 当日 neg=-4.0 输入 -1.0 → 剩余额度 down=0.0 → 返回 0.0")
    assert daily_neg == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 链路 3：快照往返（S1Snapshot 构造 → 衰减 → 取激素值 → model_copy 回写）
# ---------------------------------------------------------------------------


def test_walkthrough_hormone_roundtrip() -> None:
    """快照 + 激素载体构造 → 衰减 → 取激素值 → 回写；两载体字段完整 + last_decay_at 推进。"""
    print("\n===== 链路 3：双载体往返 S1Snapshot + PulseState =====")

    snap = make_snapshot(affiliation=10.0)
    pulse = make_pulse(dopamine=60.0)
    print("[构造] version=1 scope_id=group_8888 is_conflict=False pulse.dopamine=60.0")
    # 快照字段完整性（P2.8 起九个字段不含 hormones）。
    assert snap.version == 1
    assert snap.scope_id == "group_8888"
    assert snap.timestamp == TS
    assert snap.last_decay_at == datetime(2026, 11, 4, 10, 0, 0)
    assert snap.is_conflict is False
    assert snap.compute_tier == "warm"
    assert snap.ipc.affiliation == pytest.approx(10.0)
    assert snap.traits["warmth"].score == 50
    assert snap.emotions["joy"].intensity == 20.0
    assert snap.emotions["joy"].ttl_expires_at == TTL
    assert isinstance(snap.emotions["joy"], EmotionValue)
    # 载体字段完整性。
    assert pulse.dopamine == pytest.approx(60.0)
    assert pulse.cortisol == pytest.approx(50.0)
    assert pulse.oxytocin == pytest.approx(50.0)
    assert pulse.delayed_impact == {}

    # 衰减：dopamine 60 → 59，affiliation 10 → 9，last_decay_at 推进到 timestamp。
    decayed, decayed_pulse = apply_time_decay(snap, pulse, timedelta(hours=1), RATES)
    print(
        "[衰减] pulse.dopamine=59.0 affiliation=9.0 "
        "last_decay_at=2026-11-04 12:00:00（推进到 timestamp）"
    )
    assert decayed_pulse.dopamine == pytest.approx(59.0)
    assert decayed.ipc.affiliation == pytest.approx(9.0)
    assert decayed.last_decay_at == TS

    # 取激素值 + 回写：模拟宿主取数后回写一帧新状态。
    dopamine = decayed_pulse.dopamine
    print(f"[取激素值] pulse.dopamine={dopamine}")
    written_back = decayed.model_copy(deep=True)
    written_back_pulse = decayed_pulse.model_copy(deep=True)
    print(f"[回写] pulse.dopamine={written_back_pulse.dopamine}（与取数值一致）")
    assert written_back_pulse.dopamine == pytest.approx(dopamine)
    assert written_back is not decayed
    assert written_back_pulse is not decayed_pulse
    assert written_back.model_dump() == decayed.model_dump()
    assert written_back_pulse.model_dump() == decayed_pulse.model_dump()
