"""Pure-constant engine configuration (plan-config.md section 2).

This module is the single source of truth for every tunable value of the
emotion engine: conflict rules, dynamics rates and assembly templates.
Everything here is a frozen dataclass instance built from literals - the
file never reads the clock, never opens a file, and never parses JSON input
(``json.dumps`` only, for the configuration fingerprint).  Future host-side
overrides construct new ``EngineConfig`` instances in the apps layer; there
is no ``load_config`` channel in core (plan-config.md section 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

#: One condition of a conflict rule: (field_path, direction).
Condition = tuple[str, Literal["up", "down"]]


@dataclass(frozen=True)
class ConflictRule:
    """One row of the conflict rule table (contract 4.2 as data).

    ``name`` is only an identifier for tests and logs; ``judge`` iterates the
    rule table and never hardcodes any rule name.
    """

    name: str
    conditions: tuple[Condition, ...]
    priority: int


@dataclass(frozen=True)
class ConflictConfig:
    """The conflict rule table plus the up/down tolerance (plan-config 2.1/2.3)."""

    rules: tuple[ConflictRule, ...]
    epsilon: float

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError("ConflictConfig.rules must not be empty")
        if self.epsilon < 0:
            raise ValueError(f"ConflictConfig.epsilon must be >= 0, got {self.epsilon}")


@dataclass(frozen=True)
class DynamicsConfig:
    """Decay speeds and cap mode (plan-config 2.4, contract 4.1)."""

    hormone_per_hour: dict[str, float]
    affiliation_per_hour: float
    daily_cap_mode: str
    # HPA-axis delayed-impact window (G2 route 2).  Deliberately an
    # independent field: engine/dynamics.py's TWO_HOUR_WINDOW caps the
    # 2h stimulus aggregation for apply_limit_cap -- a different mechanism
    # that merely happens to share the number 2.  Reusing that constant
    # would couple the two mechanisms, so the default repeats the value
    # explicitly (changing one must not silently change the other).
    delayed_impact_window_hours: float = 2.0


@dataclass(frozen=True)
class AssemblyConfig:
    """Assembly template values (plan-config 2.5, contract 4.3 / 3.3)."""

    segment_names: dict[str, str]
    seal_text: str
    lexicon_fallback: str


@dataclass(frozen=True)
class EngineConfig:
    """Top-level aggregate of the three sub-configs."""

    conflict: ConflictConfig
    dynamics: DynamicsConfig
    assembly: AssemblyConfig


#: Two built-in rules from contract 4.2, priorities 10 and 20 (plan-config 2.1).
DEFAULT_RULES: tuple[ConflictRule, ...] = (
    ConflictRule(
        name="dopamine_up_and_cortisol_up",
        conditions=(
            ("pulse.dopamine", "up"),
            ("pulse.cortisol", "up"),
        ),
        priority=10,
    ),
    ConflictRule(
        name="affiliation_up_and_dominance_down",
        conditions=(
            ("ipc.affiliation", "up"),
            ("ipc.dominance", "down"),
        ),
        priority=20,
    ),
)


def _default_hormone_rates() -> dict[str, float]:
    """Placeholder 1.0 unit/hour per hormone, decided in DEFAULT_ENGINE_CONFIG."""
    return {"dopamine": 1.0, "cortisol": 1.0, "oxytocin": 1.0}


DEFAULT_ENGINE_CONFIG = EngineConfig(
    conflict=ConflictConfig(rules=DEFAULT_RULES, epsilon=0.05),
    dynamics=DynamicsConfig(
        hormone_per_hour=_default_hormone_rates(),
        affiliation_per_hour=0.5,
        daily_cap_mode="natural_day",
        delayed_impact_window_hours=2.0,
    ),
    assembly=AssemblyConfig(
        segment_names={
            "main": "[主场]",
            "tension": "[拉扯]",
            "bystander": "[旁边杵着]",
        },
        seal_text="绝对不要使用任何括号或星号描写动作。",
        lexicon_fallback="熟悉的熟悉感漫上来。",
    ),
)


def config_fingerprint(cfg: EngineConfig) -> str:
    """Return a stable JSON fingerprint of ``cfg`` (dumps-only, never loads).

    Used for structure self-checks and drift detection: the same config value
    always produces the same string, and any field change changes the string.
    """
    return json.dumps(
        {
            "conflict": {
                "rules": [
                    {
                        "name": rule.name,
                        "conditions": [list(cond) for cond in rule.conditions],
                        "priority": rule.priority,
                    }
                    for rule in cfg.conflict.rules
                ],
                "epsilon": cfg.conflict.epsilon,
            },
            "dynamics": {
                "hormone_per_hour": dict(cfg.dynamics.hormone_per_hour),
                "affiliation_per_hour": cfg.dynamics.affiliation_per_hour,
                "daily_cap_mode": cfg.dynamics.daily_cap_mode,
            },
            "assembly": {
                "segment_names": dict(cfg.assembly.segment_names),
                "seal_text": cfg.assembly.seal_text,
                "lexicon_fallback": cfg.assembly.lexicon_fallback,
            },
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# P2.8 structural placeholders (ADR-008 section 5)
#
# Every value below is a *shape*, not a tuned number.  P4 re-derives all six
# after load-testing against real dialogue data; until then the numbers exist so
# the downstream signatures can be written and the wiring can be tested.
# The mandated sentence appears verbatim above each constant because the human
# ruling of 2026-11-06 requires it -- do not paraphrase or drop it.
# ---------------------------------------------------------------------------

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_RESIDENT_BONUS_RATE: Final[float] = 0.1
# 每连续驻留 1 天的热度加成

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_IPC_BONUS_RATE: Final[float] = 0.1
# ipc.affiliation 每单位映射到热度的加成

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_TENSION_THRESHOLD: Final[float] = 80.0
# TensionState.value 超过此值时强制提为 hot

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_PERSONA_HEAT_MODIFIER: Final[MappingProxyType] = MappingProxyType(
    {"cold": 0.4, "warm": 1.0, "hot": 1.5}
)
# 人设热度系数：冷酷 0.4 / 常温 1.0 / 开朗 1.5
# cold=0.4（原 0.5）：动态满值 100.0 × 0.5 = 50.0 恰好落在 hot 侧，
# 「削半反而提档」与 ADR-02「50 归 Hot」语义冲突。改 0.4 后 100.0 × 0.4 = 40.0
# 落 warm，同时不动 evaluate_compute_tier 的 < 50 判据（人类 2026-11-06 裁决）。

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_MAX_QUEUE_RETRIES: Final[int] = 3
# 异步对账队列消费失败重试上限

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_STIMULUS_UNIT_POINTS: Final[float] = 8.0
# 「1 个刺激单位 = 8 个激素点数」的换算桥梁（人类 m16532 终裁选 A）。
# 依据当前词典最大单轴增量（8.0 = 「滚」的 cortisol 8.4 收整）校准，
# 待 P4 真实数据压测后裁决。它不改变 apply_limit_cap 的任何阈值，
# 只回答「多少点数算一个刺激单位」。

# 此为结构占位值，实际业务数值待 P4 阶段基于真实对话数据压测后裁决。
PLACEHOLDER_RESIDENT_RESET_DAYS: Final[int] = 3
# 驻留中断超过此天数则驻留加成清零

# The six placeholder comments above are the human ruling's mandated sentence,
# copied verbatim -- it contains a full-width comma that RUF003 flags. Rewriting
# it to ASCII would break the ruling ("逐字，不许改写"), so the suppression is
# scoped to RUF003 only and to this file: no other rule and no other module is
# covered by it. Every other comment here stays ASCII.
# ruff: noqa: RUF003
