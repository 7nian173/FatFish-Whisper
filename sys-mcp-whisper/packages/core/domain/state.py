"""S1 state snapshot and its nested state models (contract 3.2).

The domain layer only defines structure and rejects out-of-range values.
It never reads the system clock and never touches IO: every timestamp is
supplied by the caller as a field value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IPCState(BaseModel):
    """Per-user individual psychology coordinates (contract 3.2 ipc)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    affiliation: float = Field(
        ge=-100,
        le=100,
        description="好感度：-100 到 100，向 0 线性衰减，按 user_id 隔离",
    )
    dominance: float = Field(
        ge=-100,
        le=100,
        description="支配感坐标值",
    )
    relationship_stage: str = Field(
        min_length=1,
        description="当前关系阶段（如 friend）",
    )
    relationship_ceiling: str = Field(
        min_length=1,
        description="关系阶段天花板（如 lover），演化候选只能在此上限内提案",
    )


class TraitState(BaseModel):
    """One trait axis; the dict value of traits in contract 3.2."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    score: int = Field(
        ge=0,
        le=100,
        description="特质轴分数",
    )
    visibility: Literal["unlocked", "flickering", "dormant"] = Field(
        description=(
            "可见性：unlocked 已解锁，flickering 闪现（当前未解锁但出现过前台信号），"
            "dormant 休眠；flickering 只进拉扯位、不进主场（P0 裁决书 A 条）"
        ),
    )


class EmotionValue(BaseModel):
    """One surface emotion entry; the dict value of emotions in contract 3.2.

    Constraint style, chosen per field shape (do not "unify" these -- each
    shape has its own natural slot):
      * scalar ``intensity`` -> ``Field(ge=..., le=...)`` works directly;
      * ``dict[str, float]`` (``HormoneState.delayed_impact``) -> a
        ``field_validator``.  NOT because ``Annotated`` cannot do it (it
        can, on both sides: ``dict[Annotated[str, ...], Annotated[float,
        Field(ge=0, le=100)]]``, and ``Literal[...]`` keys are rejected too --
        verified on pydantic 2.14), but because one validator names the
        offending key and checks key + range in a single pass with an
        explicit message.  An earlier revision of this docstring claimed
        ``Annotated`` "would silently stop validating"; that was false and is
        recorded here so it is not re-derived;
      * ``tuple[str, ...]`` elements (``UserProfileRef.cached_tags``) ->
        ``Annotated[str, Field(min_length=1)]`` is enough, declared inline.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    intensity: float = Field(
        ge=0,
        le=100,
        description="情绪强度：0 到 100",
    )
    ttl_expires_at: datetime = Field(
        description="情绪衰减到期时刻，由调用方注入（domain 不读时钟）",
    )
    frozen_until: datetime | None = Field(
        default=None,
        description="冻结截止时刻，None 表示未冻结",
    )

    @field_validator("ttl_expires_at", "frozen_until")
    @classmethod
    def _timestamps_must_be_utc_aware(cls, v: datetime | None) -> datetime | None:
        # Naive datetimes are a real incident source: every timestamp in this
        # layer is caller-injected, so a naive value would silently shift by
        # the host's local offset.  None stays legal (frozen_until default).
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return v


def _field_paths(model: type) -> set[str]:
    """Recursively collect dotted field paths of a pydantic model.

    Module-private so that only ``S1Snapshot.valid_paths()`` exposes it; the
    recursion still needs to reach nested models such as ``IPCState`` without
    giving every domain model a public method of its own.
    """
    paths: set[str] = set()
    for name, info in model.model_fields.items():
        annotation = info.annotation
        nested: set[str] = set()
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            nested = _field_paths(annotation)
        if nested:
            paths.update(f"{name}.{sub}" for sub in nested)
        else:
            paths.add(name)
    return paths


class S1Snapshot(BaseModel):
    """Top-level S1 state snapshot (contract 3.2)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    version: int = Field(
        ge=1,
        description="快照结构版本号",
    )
    scope_id: str = Field(
        min_length=1,
        description="快照所属会话域，与 MCPRequest.scope_id 同键",
    )
    timestamp: datetime = Field(
        description="快照生成时刻",
    )
    last_active_at: datetime | None = Field(
        default=None,
        description="最后有效活跃时刻, 由调用方注入; None 表示尚未记录活跃 (驻留 0 天)",
    )
    last_decay_at: datetime = Field(
        description="上次时间衰减结算时刻，delta_t = timestamp − last_decay_at 的锚点",
    )
    is_conflict: bool = Field(
        description="冲突判定结果（契约 4.2），domain 层只存储不计算",
    )
    compute_tier: Literal["cold", "warm", "hot"] = Field(
        default="warm",
        description="当前算力档位（ADR-006）；由编排层每次请求现算派生，domain 只存储不计算",
    )
    ipc: IPCState = Field(
        description="个体心理坐标（per-user 隔离）",
    )
    traits: dict[str, TraitState] = Field(
        description="特质轴表：键为特质名，值为 TraitState",
    )
    emotions: dict[str, EmotionValue] = Field(
        description=(
            "表层情绪强度表：键为情绪名（如 anger / joy），值为 EmotionValue；"
            "键名白名单归 P3 词表阶段，本字段当前只约束值结构"
        ),
    )

    @classmethod
    def valid_paths(cls) -> set[str]:
        """Export every dotted field path of this model as a set of strings.

        This is a *structural* inventory of the snapshot, not the delta
        whitelist.  The delta whitelist is
        ``global_context.DELTA_ELIGIBLE_PATHS``, not this method -- see ADR-008
        section 1.8.  The two used to be the same thing (both callers
        validated against ``valid_paths()``, a single implementation replacing
        two hand-copied recursions, see P2.5 Bug 2), but the whitelist is now a
        contract constant that admits only additive numeric paths.  Changing
        this method does NOT change which delta keys are accepted.

        A nested pydantic model contributes ``parent.child`` paths; everything
        else (scalars, dicts, tuples) contributes the bare field name.  Note
        that bare top-level names such as ``is_conflict`` and ``timestamp``
        appear here, which is exactly why this set is too wide to serve as the
        delta whitelist.
        """
        return _field_paths(cls)


# Field descriptions above are copied verbatim from contract 3.2, so they keep
# the original full-width CJK punctuation that RUF001 flags as ambiguous.
# ruff: noqa: RUF001
