"""宿主 API 抽象层。

Adapter 不直接依赖 NapCat / OneBot / NoneBot 的具体 SDK，只依赖这里的
:class:`HostAPI` 协议。接入真实宿主时写一个薄适配类即可；测试用
:class:`MockHostAPI` 记录所有发送与撤回。

关键约定
--------
``send_text`` / ``send_sticker`` **必须返回平台的新消息 ID**，撤回功能依赖它。
"""
from __future__ import annotations

import itertools
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from .models import Target


@runtime_checkable
class HostAPI(Protocol):
    """宿主能力协议（结构类型，鸭子类型即可满足）。"""

    async def send_text(self, target: Target, text: str) -> str:
        """发文本，返回新消息 ID。"""
        ...

    async def send_poke(self, target: Target, user_id: str) -> None:
        """拍一拍 ``user_id``。"""
        ...

    async def send_sticker(self, target: Target, tag: str) -> Optional[str]:
        """发表情包，返回新消息 ID（宿主不支持则返回 None）。"""
        ...

    async def recall(self, target: Target, message_id: str) -> None:
        """撤回指定消息。"""
        ...


class MockHostAPI:
    """测试/演示用宿主：只记录日志，不发真实消息。

    Attributes:
        sends: 每次发送的记录 ``{"kind", "target", "content", "message_id"}``。
        recalls: 每次撤回的记录 ``{"target", "message_id"}``。
    """

    def __init__(self, fail_on_send: bool = False) -> None:
        self.sends: list[dict[str, Any]] = []
        self.recalls: list[dict[str, Any]] = []
        self.fail_on_send = fail_on_send
        self._seq = itertools.count(10001)

    def _next_id(self) -> str:
        return "mid-" + str(next(self._seq))

    async def send_text(self, target: Target, text: str) -> str:
        if self.fail_on_send:
            raise RuntimeError("宿主发送失败（模拟）")
        message_id = self._next_id()
        self.sends.append(
            {
                "kind": "text",
                "target": str(target),
                "content": text,
                "message_id": message_id,
            }
        )
        return message_id

    async def send_poke(self, target: Target, user_id: str) -> None:
        self.sends.append(
            {"kind": "poke", "target": str(target), "content": str(user_id), "message_id": None}
        )

    async def send_sticker(self, target: Target, tag: str) -> Optional[str]:
        message_id = self._next_id()
        self.sends.append(
            {"kind": "sticker", "target": str(target), "content": tag, "message_id": message_id}
        )
        return message_id

    async def recall(self, target: Target, message_id: str) -> None:
        self.recalls.append({"target": str(target), "message_id": message_id})


class CallbackHost:
    """用三个回调函数拼出来的宿主（兼容阶段一 Adapter 的写法）。

    Args:
        send_text: ``async def (user_id, text) -> message_id | None``。
        send_poke: ``async def (target_user_id, suffix) -> None``。
        recall_message: ``async def (message_id_or_user_id) -> None``。

    Note:
        旧回调签名只有 ``user_id``，没有群号，所以群聊场景建议直接用
        :class:`MockHostAPI` 的模式自己实现 :class:`HostAPI`。
    """

    def __init__(
        self,
        send_text: Optional[Callable[[str, str], Awaitable[Any]]] = None,
        send_poke: Optional[Callable[[str, str], Awaitable[Any]]] = None,
        recall_message: Optional[Callable[[Any], Awaitable[Any]]] = None,
    ) -> None:
        self._send_text = send_text
        self._send_poke = send_poke
        self._recall = recall_message

    async def send_text(self, target: Target, text: str) -> str:
        if self._send_text is None:
            raise RuntimeError("宿主未提供 send_text 能力")
        result = await self._send_text(target.user_id, text)
        return "" if result is None else str(result)

    async def send_poke(self, target: Target, user_id: str) -> None:
        if self._send_poke is None:
            raise RuntimeError("宿主未提供 send_poke 能力")
        await self._send_poke(user_id, "")

    async def send_sticker(self, target: Target, tag: str) -> Optional[str]:
        # 旧回调没有表情包能力，退化为发一个表情文字，避免整条链路断掉
        if self._send_text is None:
            return None
        result = await self._send_text(target.user_id, "[" + tag + "]")
        return "" if result is None else str(result)

    async def recall(self, target: Target, message_id: str) -> None:
        if self._recall is None:
            raise RuntimeError("宿主未提供 recall 能力")
        await self._recall(message_id)
