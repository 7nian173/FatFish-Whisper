"""Adapter 的领域模型：入站事件、动作、Core 响应。

设计原则
--------
Adapter 只做「透传 + 执行」，**不做任何情绪判定**。所有字段原样透传给 Core，
Core 返回什么动作就执行什么动作，返回重复就什么都不做。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


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
        """用于冷却/日志的稳定标识。"""
        return "g:" + str(self.group_id) if self.is_group else "p:" + str(self.user_id)

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
    """

    platform_message_id: str
    user_id: str
    message: str
    group_id: Optional[str] = None
    is_at_bot: bool = False
    self_id: Optional[str] = None

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
            # Adapter 侧已做防抖（合并窗口），Core 不需要再合并
            "debounce": False,
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
