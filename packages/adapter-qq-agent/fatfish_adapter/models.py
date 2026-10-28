"""Adapter 的领域模型：入站事件、动作、Core 响应。

设计原则
--------
Adapter 只做「透传 + 执行」，**不做任何情绪判定**。所有字段原样透传给 Core，
Core 返回什么动作就执行什么动作，返回重复就什么都不做。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: 触发口径四值（P0-5，与 Core 的 ``engine.TRIGGER_TYPES`` 同集合）。
#:
#: Adapter 只负责**原样声明**，绝不从 ``is_at_bot`` / 群号 / 文本内容推断——
#: 那属于死线 D9 禁止的行为：口径会改变 Core 的扣分与写库行为，猜错就是记错账。
TRIGGER_TYPES: tuple[str, ...] = (
    "user_msg",
    "proactive",
    "cron_task",
    "runtime_context",
)

#: 缺省口径：宿主看到一条消息时，它默认就是用户真实发言。
TRIGGER_TYPE_DEFAULT = "user_msg"


@dataclass(frozen=True)
class Target:
    """发送目标：群聊或私聊。

    Attributes:
        user_id: 用户 ID（私聊直接发给他；群聊里用于 @ / 拍一拍）。
        group_id: 群号；None 表示私聊。
    """

    user_id: str
    group_id: Optional[str] = None

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    @property
    def key(self) -> str:
        """用于冷却/日志的稳定标识 —— 同时也是 Core 的 ``chat_scope`` 形状。"""
        return "g:" + str(self.group_id) if self.is_group else "p:" + str(self.user_id)

    @property
    def chat_scope(self) -> str:
        """透传给 Core 的会话分区（``g:<群号>`` / ``p:<QQ号>``）。

        与 :attr:`key` 同一个值，但语义分开：``key`` 是 Adapter 内部标识，
        ``chat_scope`` 是 Core 的分房键。两者形状一致是刻意的——
        Core 侧只校验形状，不重新派生。
        """
        return self.key

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        return self.key


@dataclass
class InboundEvent:
    """宿主侧的一条消息事件（原样透传给 Core）。

    Attributes:
        platform_message_id: 宿主平台原生消息 ID（NapCat/OneBot 的 message_id）。
            **必须透传**：Core 的 IngressDeduper 靠它识别重推，比文本启发式准得多。
        user_id: 发送者 QQ 号。
        message: 纯文本消息内容。
        group_id: 群号；私聊为 None。
        is_at_bot: 是否 @ 了 Bot。
        self_id: Bot 自己的 QQ 号（宿主有就带上，便于后续过滤自身消息）。
        trigger_type: 触发口径（P0-5，四值见 :data:`TRIGGER_TYPES`）。默认
            ``user_msg``——宿主侧看到的就是「用户发了一条消息」。**不要按
            ``is_at_bot`` 或群号去推断**（死线 D9）。
    """

    platform_message_id: str
    user_id: str
    message: str
    group_id: Optional[str] = None
    is_at_bot: bool = False
    self_id: Optional[str] = None
    trigger_type: str = TRIGGER_TYPE_DEFAULT

    @property
    def target(self) -> Target:
        return Target(user_id=self.user_id, group_id=self.group_id)

    def to_payload(self) -> dict[str, Any]:
        """转成 Core 的 ``POST /api/v1/chat`` 请求体。"""
        payload: dict[str, Any] = {
            "platform_message_id": self.platform_message_id,
            "user_id": self.user_id,
            "message": self.message,
            "is_at_bot": self.is_at_bot,
            # 防抖归属：**Core 是唯一权威**（规格 P0-11）。Adapter 只声明「请合并」，
            # 不再自己开窗口，避免两侧各合并一次导致丢消息。
            # DebounceQueue 已按 F-19 裁决（2026-10-27 选项 1）封存于 _archive/debounce/，
            # 不要在 Adapter 侧重新合并；重启条件见该目录 README.md。
            "debounce": True,
            # 会话分区：Core 按它给情绪/记忆分房（I-26）
            "chat_scope": self.target.chat_scope,
            # P0-5 触发口径：显式声明，Core 不做任何推断（死线 D9）
            "trigger_type": self.trigger_type,
        }
        if self.group_id is not None:
            payload["group_id"] = str(self.group_id)
        return payload


@dataclass
class Action:
    """Core 返回的一个动作。

    支持的 type：
        - ``text``:    ``content`` 是要发的文本。
        - ``poke``:    ``target`` 是被拍的人（默认就是发消息的人）。
        - ``sticker``: ``tag`` 是表情包标签。
    """

    type: str
    content: str = ""
    target: str = ""
    tag: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Action":
        return cls(
            type=str(raw.get("type", "")),
            content=str(raw.get("content", "") or ""),
            target=str(raw.get("target", "") or ""),
            tag=str(raw.get("tag", "") or ""),
        )


@dataclass
class CoreReply:
    """Core 的一次响应。

    Attributes:
        status: ``ok`` / ``duplicate`` / ``fallback``。
        actions: 待执行动作（fallback 时是一个 text 动作）。
        inner_os: 内心 OS（调试用，绝不发给用户）。
        recall_hint: 撤回提示 ``{"probability": 0.15}``。
        debug: Core 的调试信息（情绪值、路径等）。
        raw: 原始 JSON，便于宿主排查。
    """

    status: str = "ok"
    actions: list[Action] = field(default_factory=list)
    inner_os: str = ""
    recall_hint: Optional[dict[str, Any]] = None
    debug: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def duplicate(self) -> bool:
        """True 表示这是被入口判重拦下的幽灵消息，不应执行任何动作。"""
        return self.status == "duplicate" or bool(self.raw.get("duplicate"))

    @property
    def is_fallback(self) -> bool:
        return self.status == "fallback"

    @property
    def recall_probability(self) -> float:
        if not self.recall_hint:
            return 0.0
        try:
            return max(0.0, min(1.0, float(self.recall_hint.get("probability", 0.0))))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "CoreReply":
        raw_actions = data.get("actions") or []
        actions = [
            Action.from_raw(item) for item in raw_actions if isinstance(item, dict)
        ]
        hint = data.get("recall_hint")
        return cls(
            status=str(data.get("status", "ok")),
            actions=actions,
            inner_os=str(data.get("inner_os", "") or ""),
            recall_hint=hint if isinstance(hint, dict) else None,
            debug=data.get("debug") if isinstance(data.get("debug"), dict) else {},
            raw=data,
        )

    @classmethod
    def fallback(cls, text: str, reason: str) -> "CoreReply":
        """构造一个降级回复（走和执行器相同的发送路径）。"""
        return cls(
            status="fallback",
            actions=[Action(type="text", content=text)],
            inner_os="",
            recall_hint=None,
            debug={"fallback_reason": reason},
            raw={"status": "fallback", "reason": reason},
        )
