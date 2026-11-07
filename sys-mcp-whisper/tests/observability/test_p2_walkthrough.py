# ruff: noqa: RUF001, RUF002, RUF003
"""P2 端到端可观测脚本（判定层 + 现象学组装层的人类视角可观测脚本）。

本脚本是 P1 `test_p1_walkthrough.py` 的 P2 续篇，端到端走一遍第二阶段两条主链路：
  1. 冲突判定（`is_conflict` 数据驱动，命中 / 未命中两条路径，打印 deltas → 返回值 → matched_rules）
  2. 现象学组装（`assemble_prompt`，打印完整字符串 + 行数 + 封印次数，覆盖纯/非纯 × 冲突/非冲突）

每一步都用 `print()` 打印 `[步骤名] 字段=值`，让人肉眼能看懂数值流转；
同时每一步都必须 `assert`（不允许只 print 不断言），断言可真实失败（非恒真）。

运行方式（抓取真实 stdout）：
    python -m pytest tests/observability/test_p2_walkthrough.py -s -v
"""

from __future__ import annotations

from datetime import UTC, datetime

from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.assembler import assemble_prompt
from packages.core.engine.judge import HormoneDeltas, is_conflict

TS = datetime(2026, 11, 4, 12, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)
EMOTIONS = {"joy": EmotionValue(intensity=20.0, ttl_expires_at=TTL)}


def make_snapshot(
    *,
    dopamine: float = 50.0,
    cortisol: float = 50.0,
    oxytocin: float = 50.0,
    affiliation: float = 0.0,
    dominance: float = 0.0,
    is_conflict: bool = False,
) -> S1Snapshot:
    """构造一个合法的 S1Snapshot；九字段完整（见 S1Snapshot 契约）。"""
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
        traits={
            "tsundere": TraitState(score=85, visibility="unlocked"),
            "shyness": TraitState(score=30, visibility="dormant"),
            "pride": TraitState(score=60, visibility="unlocked"),
        },
        emotions=EMOTIONS,
    )


# ---------------------------------------------------------------------------
# 链路 1：冲突判定 —— 命中路径
# ---------------------------------------------------------------------------


def test_p2_walkthrough_conflict_hit() -> None:
    """dopamine↑ + cortisol↑ 同向命中规则 1（dopamine_up_and_cortisol_up）。"""
    print("\n===== P2 链路 1a：冲突判定 is_conflict（命中）=====")

    snap = make_snapshot()
    deltas = HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": 1.0})
    print("[输入 deltas] hormones.dopamine=1.0 hormones.cortisol=1.0")

    verdict = is_conflict(snap, deltas)
    print(f"[is_conflict 返回] conflicted={verdict.conflicted}")
    print(f"[matched_rules] {verdict.matched_rules}")

    assert verdict.conflicted is True
    assert verdict.matched_rules == ("dopamine_up_and_cortisol_up",)

    # 与 plan-judge.md §2.1 / AC-J2 判据对齐：同向命中规则 1。
    print("[手算对齐 AC-J2] dopamine +1.0 & cortisol +1.0 → conflicted=True 命中规则 1")
    assert is_conflict(
        snap,
        HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": 1.0}),
    ).matched_rules == ("dopamine_up_and_cortisol_up",)


# ---------------------------------------------------------------------------
# 链路 1：冲突判定 —— 未命中路径
# ---------------------------------------------------------------------------


def test_p2_walkthrough_conflict_miss() -> None:
    """dopamine↑ + cortisol↓ 反向，不命中任何规则。"""
    print("\n===== P2 链路 1b：冲突判定 is_conflict（未命中）=====")

    snap = make_snapshot()
    deltas = HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": -1.0})
    print("[输入 deltas] hormones.dopamine=1.0 hormones.cortisol=-1.0")

    verdict = is_conflict(snap, deltas)
    print(f"[is_conflict 返回] conflicted={verdict.conflicted}")
    print(f"[matched_rules] {verdict.matched_rules}")

    assert verdict.conflicted is False
    assert verdict.matched_rules == ()

    # 与 plan-judge.md §2.1 / AC-J3 判据对齐：一升一降不构成同向，不命中。
    print("[手算对齐 AC-J3] dopamine↑ + cortisol↓ → conflicted=False matched_rules=()")
    assert is_conflict(
        snap,
        HormoneDeltas(values={"pulse.dopamine": 1.0, "pulse.cortisol": -1.0}),
    ).matched_rules == ()


# ---------------------------------------------------------------------------
# 链路 2：现象学组装 assemble_prompt
# ---------------------------------------------------------------------------


def test_p2_walkthrough_assemble() -> None:
    """组装四种形态（纯/非纯 × 冲突/非冲突），打印完整字符串 + 行数 + 封印出现次数。"""
    print("\n===== P2 链路 2：现象学组装 assemble_prompt =====")

    # 形态 A：非冲突 + 非纯（3 行：标签 + 主场 + 旁边杵着）。
    snap_false = make_snapshot(is_conflict=False)
    prompt = assemble_prompt(snap_false, pure=False)
    print("[组装 A 非冲突+非纯] 完整输出 ↓")
    print(prompt)
    print(f"[组装 A] 行数={len(prompt.splitlines())}")
    print(f"[组装 A] 封印出现次数={prompt.count('绝对不要使用任何括号或星号描写动作。')}")
    assert len(prompt.splitlines()) == 3
    assert "绝对不要使用任何括号或星号描写动作。" not in prompt

    # 形态 B：非冲突 + 纯（4 行：多加封印行，且封印只在最后一行出现一次）。
    prompt = assemble_prompt(snap_false, pure=True)
    print("\n[组装 B 非冲突+纯] 完整输出 ↓")
    print(prompt)
    print(f"[组装 B] 行数={len(prompt.splitlines())}")
    print(f"[组装 B] 封印出现次数={prompt.count('绝对不要使用任何括号或星号描写动作。')}")
    lines = prompt.splitlines()
    assert len(lines) == 4
    assert lines[-1] == "【输出格式封印】：绝对不要使用任何括号或星号描写动作。"
    assert prompt.count("绝对不要使用任何括号或星号描写动作。") == 1

    # 形态 C：冲突 + 非纯。
    # 应主机要求，非纯渲染会把正文截断到
    # MAX_INJECTION_BODY_CHARS；冲突态正文截断前 59 码点，所以从尾部
    # 丢掉 [旁边杵着]，保留主场 > 拉扯。完整三段式见形态 D（纯渲染）。
    snap_true = make_snapshot(is_conflict=True)
    prompt = assemble_prompt(snap_true, pure=False)
    print("\n[组装 C 冲突+非纯] 完整输出 ↓")
    print(prompt)
    print(f"[组装 C] 行数={len(prompt.splitlines())}")
    print(f"[组装 C] 封印出现次数={prompt.count('绝对不要使用任何括号或星号描写动作。')}")
    lines = prompt.splitlines()
    assert len(lines) == 3
    assert lines[0] == "【潜意识感知】："
    assert lines[1].startswith("[主场]：")
    assert lines[2].startswith("[拉扯]：")
    body_chars = sum(len(line) for line in lines[1:])
    assert body_chars <= 50

    # 形态 D：冲突 + 纯（5 行：标签 + 主场 + 拉扯 + 旁边 + 封印）。
    prompt = assemble_prompt(snap_true, pure=True)
    print("\n[组装 D 冲突+纯] 完整输出 ↓")
    print(prompt)
    print(f"[组装 D] 行数={len(prompt.splitlines())}")
    print(f"[组装 D] 封印出现次数={prompt.count('绝对不要使用任何括号或星号描写动作。')}")
    lines = prompt.splitlines()
    assert len(lines) == 5
    assert lines[-1] == "【输出格式封印】：绝对不要使用任何括号或星号描写动作。"
    assert prompt.count("绝对不要使用任何括号或星号描写动作。") == 1

    # 段名与主场句来自 config.assembly / 首个 unlocked 词条（tsundere score=85 → high）。
    print("\n[手算对齐 §2.3/§2.4] 主场句=tsundere 高光档（score 85 ≥ 50）")
    assert "[主场]：被叫笨蛋时，你感到一阵烦躁，指尖发烫。" in prompt
