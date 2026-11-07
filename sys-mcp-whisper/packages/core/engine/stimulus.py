"""P3-E2: to_delta -- arbiter 姿态三分到激素点分增量的纯函数桥.

- 本模块是纯函数: 不读时钟, 不碰 IO, 不改入参.
- 输入是 arbiter 的三维姿态分 ``agreeableness`` / ``arousal``
  (``confidence`` 不参与换算, 不消费).
- 输出是可直接喂 ``HormoneDeltas(values=...)`` 的点分路径字典,
  键必须落在 ``DELTA_ELIGIBLE_PATHS`` 内.
"""

from __future__ import annotations

from typing import Final

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_STIMULUS_POS_DOPAMINE_RATE: Final[float] = 8.0
# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_STIMULUS_POS_OXYTOCIN_RATE: Final[float] = 6.0
# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_STIMULUS_POS_AFFILIATION_RATE: Final[float] = 6.0
# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_STIMULUS_NEG_CORTISOL_RATE: Final[float] = 8.0
# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_STIMULUS_AROUSAL_THRESHOLD: Final[float] = 0.3
# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_STIMULUS_AROUSAL_CORTISOL_RATE: Final[float] = 10.0


def to_delta(arb: dict[str, float]) -> dict[str, float]:
    """把 arbiter 姿态分换算成激素点分增量 (Demo 胶水的生产化).

    语义照抄 ``30_沙盒区/P1P2阶段damo/run_cli_demo.py`` 的 to_delta
    (P3 施工蓝图 plan-p3-orchestration.md §4 P3-E-2 的唯一权威实现):
    ``ag > 0`` 产三路正向增量; ``ag < 0`` 产皮质醇升 + 好感降;
    ``ag == 0`` 两边都不进; ``ar > 0.3`` (严格大于, 等于不触发)
    追加皮质醇; ``round(x, 3)`` 防浮点尾差. 缺键直接 ``KeyError``
    (键名拼错立刻暴露, 不吞). 零值剔除属编排层合成后的动作,
    不是本函数的职责 (蓝图 §4「合成顺序与冲突规则」).
    """
    ag = arb["agreeableness"]
    ar = arb["arousal"]
    out: dict[str, float] = {}
    if ag > 0:
        out["pulse.dopamine"] = round(ag * PLACEHOLDER_STIMULUS_POS_DOPAMINE_RATE, 3)
        out["pulse.oxytocin"] = round(ag * PLACEHOLDER_STIMULUS_POS_OXYTOCIN_RATE, 3)
        out["ipc.affiliation"] = round(ag * PLACEHOLDER_STIMULUS_POS_AFFILIATION_RATE, 3)
    elif ag < 0:
        out["pulse.cortisol"] = round(-ag * PLACEHOLDER_STIMULUS_NEG_CORTISOL_RATE, 3)
        out["ipc.affiliation"] = round(ag * PLACEHOLDER_STIMULUS_POS_AFFILIATION_RATE, 3)
    if ar > PLACEHOLDER_STIMULUS_AROUSAL_THRESHOLD:
        out["pulse.cortisol"] = round(
            out.get("pulse.cortisol", 0.0)
            + (ar - PLACEHOLDER_STIMULUS_AROUSAL_THRESHOLD)
            * PLACEHOLDER_STIMULUS_AROUSAL_CORTISOL_RATE,
            3,
        )
    return out


# ruff: noqa: RUF003
# ↑ RUF003 豁免(仅本文件, 仅此一条): 上方六个常量的法定注释句是人类裁决的逐字文案, 不得改写.
