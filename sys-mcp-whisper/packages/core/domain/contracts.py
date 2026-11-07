"""Request/response contracts between host and emotion engine (3.1, 3.3, 3.4).

The domain layer only defines structure and rejects out-of-range values.
It never reads the system clock and never touches IO.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .global_context import DELTA_ELIGIBLE_PATHS
from .state import S1Snapshot


class MCPRequestContext(BaseModel):
    """The `context` object of contract 3.1, feeding delta_t into time decay."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    last_interaction_at: datetime = Field(
        description="上一次交互发生的 UTC 时刻",
    )
    current_time: datetime = Field(
        description="本次请求到达的 UTC 时刻",
    )


class MCPRequest(BaseModel):
    """MCP Request, host to emotion engine (contract 3.1)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    request_id: str = Field(
        min_length=1,
        description="宿主生成的本次请求唯一标识",
    )
    user_id: str = Field(
        min_length=1,
        description="用户唯一标识，好感度 IPC 按 user_id 隔离",
    )
    scope_id: str = Field(
        min_length=1,
        description="会话域标识，区分私聊与群聊（双轨隔离键）",
    )
    message: str = Field(
        description="用户本次输入的原始消息文本",
    )
    persona_card: str = Field(
        description="宿主只读人设卡全文（L0 层，情绪机不修改）",
    )
    chat_mode: Literal["pure", "roleplay"] = Field(
        description="对话模式：pure 为裸聊模式，roleplay 为角色扮演模式",
    )
    context: MCPRequestContext = Field(
        description="时间上下文，供时间衰减计算 delta_t",
    )


class ActionIntent(BaseModel):
    """The `action_intent` object of contract 3.3."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    emotion: str = Field(
        min_length=1,
        description="动作意图情绪标签（如 annoyed）",
    )
    sticker_tag: str = Field(
        min_length=1,
        description="贴纸标签（如 angry/pout）",
    )


class MCPResponse(BaseModel):
    """MCP Response, emotion engine to host (contract 3.3)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    request_id: str = Field(
        min_length=1,
        description="回显宿主请求标识",
    )
    status: Literal["success"] = Field(
        description="处理状态",
    )
    # P3 (ADR-009 R4 + U6 终裁 2026-11-06): the single-string injection field
    # was removed.  Static content (original persona card + output-format seal)
    # and dynamic content (phenomenological injection) are now separate so the
    # host can keep the former byte-identical at the head of every request and
    # thereby preserve KV-cache prefix hits.
    system_head: str = Field(
        description="静态头部：原版人设卡 + 全局格式封印，每次请求逐字节相同",
    )
    tail_message: str = Field(
        description="动态尾部：现象学注入文本（躯体感知 + 潜意识），追加在对话流末尾",
    )
    state_snapshot: S1Snapshot = Field(
        description="结算后的 S1 状态快照（见契约 3.2）",
    )
    action_intent: ActionIntent = Field(
        description="可选动作意图",
    )
    # P2.5 (task-FFW-P25-T4): output-direction field, NOT a host input --
    # the engine computes it (calculate_typing_delay) and the host only reads
    # it to pace the typing indicator.  ``strict=True`` is deliberate: without
    # it pydantic's lax int mode coerces the string "100" into 100, which would
    # let a malformed host payload silently through (measured on 2.14.1).
    typing_delay_ms: int = Field(
        default=0,
        ge=0,
        le=5000,
        strict=True,
        description="拟人化输入延迟（毫秒），由引擎侧 calculate_typing_delay 计算后回填；上限 5000",
    )


class ProposalItem(BaseModel):
    """One item of the `proposals` list in contract 3.4."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    category: str = Field(
        min_length=1,
        description="提案类别（如 threshold）",
    )
    target: str = Field(
        pattern=r"^[a-z_]+(\.[a-z_]+)+$",
        description="提案目标参数的点分路径",
    )
    old_value: int | float | str = Field(
        description="当前值",
    )
    new_value: int | float | str = Field(
        description="建议新值",
    )
    reason: str = Field(
        min_length=1,
        description="提案依据的人类可读说明",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="置信度 0 到 1",
    )


class CandidateProposal(BaseModel):
    """Evolution candidate proposal (contract 3.4)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    candidate_id: str = Field(
        min_length=1,
        description="候选提案唯一标识",
    )
    baseline_hash: str = Field(
        min_length=1,
        description="提案所基于的 L0 基线哈希，写入前比对防漂移",
    )
    proposals: list[ProposalItem] = Field(
        min_length=1,
        description="本候选包含的具体提案列表",
    )
    status: Literal["pending_approval"] = Field(
        description="候选状态：待人类审批",
    )


class UserProfileRef(BaseModel):
    """Fallback tag cache for the Context Router (plan section 4, G4).

    The normal routing path never reads ``cached_tags``; it is only used when
    LLM routing degrades (Track B ``is_degraded``) and a degraded route still
    needs a short, pre-fetched tag list.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    user_id: str = Field(
        min_length=1,
        description="用户唯一标识，缓存按 user_id 隔离",
    )
    cached_tags: tuple[Annotated[str, Field(min_length=1)], ...] = Field(
        default=(),
        max_length=8,
        description=(
            "降级路由兜底短标签缓存：每项非空，最多 8 个；"
            "用 tuple 而非 list，list 是可变引用会绕过快照隔离，"
            "tuple 值语义与 domain 不可变口径一致"
        ),
    )
    tags_fetched_at: datetime | None = Field(
        default=None,
        description="缓存抓取时刻，由调用方注入，domain 不读系统时钟",
    )

class EmotionalEvent(BaseModel):
    """Append-only record of an emotion that already happened (plan section 5, G5).

    The nine fields below are reverse-engineered from the four P3-04 consumption
    requirements, so none of them is decorative: without ``scope_id`` the
    private/group filter degrades into a blanket rule by type, and without
    ``created_at`` the "newest three" truncation stops being reproducible.

    ``frozen=True`` is deliberate and differs from every other domain model: an
    event is a fact that already happened, so editing it tampers with history
    rather than performing a state transition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        min_length=1,
        description="事件唯一标识，由生产方生成（uuid 或 hash），domain 只锁非空",
    )
    emotion_key: str = Field(
        min_length=1,
        description="情绪键（如 anger），词表归 P3 阶段，本层只锁非空",
    )
    intensity: float = Field(
        ge=0,
        le=100,
        description="情绪强度 0 到 100；severity 是分档，intensity 是档内排序键",
    )
    severity: Literal["high", "medium", "low"] = Field(
        description="严重度分档：high 高档，medium 中档，low 低档（检索只取前两档）",
    )
    summary: str = Field(
        min_length=1,
        max_length=200,
        description="事件摘要 1 到 200 个字符（按 Unicode 码点计，不做 strip 校验）",
    )
    visibility_scope: Literal["private", "group"] = Field(
        description="可见范围：private 仅本会话可见，group 群内可见；不引入 public",
    )
    scope_id: str = Field(
        min_length=1,
        description="事件所属会话标识，过滤 private 事件时与查看方 scope 比对",
    )
    created_at: datetime = Field(
        description="事件发生时刻，由调用方注入（domain 不读系统时钟）",
    )
    raw_retained: bool = Field(
        default=False,
        description="原文是否留存；本模型无原文体字段，默认 False 即结构自证只存摘要",
    )

    @field_validator("created_at")
    @classmethod
    def _created_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps; any aware offset is accepted.

        Same rule as ``state.EmotionValue._timestamps_must_be_utc_aware``: the
        domain never reads the clock, so a timestamp without a zone cannot be
        compared against caller-injected times.  Deliberately NOT extracted into
        a shared helper - that would require a new module, which is a layering
        change (plan section 5.3).
        """
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value



#: Inline copy of the scenario vocabulary owned by ``engine/routing.py``.
#:
#: The engine layer holds the real ``SCENARIO_TAGS`` constant, but the domain may
#: not import engine (layering), so this model validates against its own copy.
#: This is the card's only duplicated source of truth; it is kept honest by
#: ``tests/engine/test_routing.py::test_scenario_tag_vocabulary_matches_the_engine_constant``,
#: which compares the two tuples element by element.  ``"degraded"`` is
#: deliberately NOT a member: it is the F-ruling literal marking a degraded
#: Track B result, not an emotion posture, and the validator admits it separately.
_SCENARIO_TAGS: tuple[str, ...] = (
    "neutral",
    "shy_hesitant",
    "defensive_cold",
    "warm_open",
    "teasing_playful",
    "distressed_fragile",
)


class RoutingResult(BaseModel):
    """One routing verdict, produced by Track A or by the Track B fallback (G9).

    A cross-layer DTO: the legacy adapter produces it in P3-02 and the engine
    consumes it, so it lives in the contract layer rather than in engine.
    ``frozen=True`` because the engine may read the verdict but must never edit
    it in place -- a second reason for freezing, distinct from
    ``EmotionalEvent``'s append-only history.

    It carries suggestions only: no S1 field lives here and there is no
    apply/copy/patch method, so "never mutates S1 directly" is enforced by the
    type rather than by convention.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stimulus_delta: dict[str, Annotated[float, Field(ge=-100, le=100)]] = Field(
        default={},
        description="本句话对数值状态的增量建议；空 dict 表示本句零刺激",
    )
    risk_flag: Literal["none", "boundary", "conflict"] = Field(
        default="none",
        description=(
            "语义层风险提示：none 普通，boundary 触发关系上限探测，"
            "conflict 判定为拉扯；它不改数值，数值全部走 stimulus_delta"
        ),
    )
    scenario_tag: str = Field(
        min_length=1,
        description="情境分类：六项词表之一，或 F 裁决的 degraded 降级字面值",
    )
    is_degraded: bool = Field(
        default=False,
        description="True 表示 Track B 兜底结果，引擎收到 True 只做数值回退",
    )
    source: Literal["track_a", "track_b"] = Field(
        description="判定来源，防降级路径被伪装成正常路径",
    )
    route_id: str = Field(
        min_length=1,
        description="本次路由的幂等键，由调用方生成，domain 只锁非空",
    )
    matched_scope: Literal["private", "group"] = Field(
        description="与 S1Snapshot.scope_id 的过滤口径对齐：群聊 group，私聊 private",
    )

    @field_validator("stimulus_delta")
    @classmethod
    def _delta_keys_must_be_delta_eligible_paths(cls, value: dict[str, float]) -> dict[str, float]:
        """Reject misspelled delta keys instead of silently reading them as 0.0.

        A typo such as ``"hormon.dopamine"`` would otherwise be accepted and
        understood as "no change" -- the same silent-negative failure mode the
        ``HormoneDeltas`` validator in engine/judge.py exists to prevent.

        The whitelist is ``global_context.DELTA_ELIGIBLE_PATHS``, the same
        constant ``HormoneDeltas`` validates against (ADR-008 section 1.8).
        ``S1Snapshot.valid_paths()`` is a structural inventory, not this list:
        it contains ``timestamp`` / ``scope_id`` and would admit non-numeric
        deltas.
        """
        valid = DELTA_ELIGIBLE_PATHS
        for key in value:
            if key not in valid:
                raise ValueError(f"unknown delta key: {key}")
        return value

    @field_validator("scenario_tag")
    @classmethod
    def _scenario_tag_must_be_known(cls, value: str) -> str:
        """Admit the six posture tags and the degraded literal, nothing else."""
        if value not in _SCENARIO_TAGS and value != "degraded":
            raise ValueError(f"unknown scenario_tag: {value}")
        return value


# Field descriptions above are copied verbatim from contracts 3.1 / 3.3 / 3.4, so
# they keep the original full-width CJK punctuation that RUF001 flags.
# ruff: noqa: RUF001
