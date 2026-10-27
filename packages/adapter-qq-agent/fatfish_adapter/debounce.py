"""消息防抖合并队列：把同一用户的连发消息合并成一次 Core 请求。

为什么需要它
------------
Core-Engine 里有 ``Debouncer``，但 Adapter 从来不给它机会：
``InboundEvent.to_payload()`` 一直传 ``debounce: False``（因为 Adapter 号称
"已经合并过了"，其实没有）。结果是宿主连发 5 条 → 5 次 HTTP 请求，
每次都占满 1.2s 的合并窗口，Core 侧的防抖形同死代码。

这里补上真正的那一半：**Adapter 侧按 (会话, 用户) 维度做固定窗口合并**。

窗口语义（有意选择，不是随手写的）
----------------------------------
- **从第一条消息开始计时，不因后续消息延长**（固定窗口）。
  尾部滑动窗口在"用户持续打字"时会一直等下去，延迟无上界；
  固定窗口的延迟上界就是 ``window``，对聊天场景更可接受。
- 窗口内到达的消息**合并成一条**，用 ``\\n`` 连接；
- 批内出现**相同 platform_message_id** 的消息直接丢弃（宿主重推）；
- 批大小达到 ``max_batch`` 立刻冲刷，防止刷屏用户把延迟堆到窗口末尾；
- 同一个 key 上的所有调用方**共享同一个 future**，拿到同一份 TurnResult。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .models import InboundEvent

logger = logging.getLogger("ffw.adapter.debounce")

#: 合并后的处理器：``async def (merged_event, merged_count) -> Any``
Handler = Callable[[InboundEvent, int], Awaitable[Any]]


def merge_events(events: list[InboundEvent]) -> InboundEvent:
    """把同一批事件合并成一个。

    Args:
        events: 非空事件列表，顺序即到达顺序。

    Returns:
        合并后的事件。``platform_message_id`` 取**第一条**——它是这批消息的
        身份，也是 Core 幂等键的来源；后续消息的 id 不单独上报。
    """
    if not events:
        raise ValueError("merge_events 需要至少一条事件")
    first = events[0]
    messages = [event.message for event in events if event.message]
    return InboundEvent(
        platform_message_id=first.platform_message_id,
        user_id=first.user_id,
        message="\n".join(messages),
        group_id=first.group_id,
        is_at_bot=any(event.is_at_bot for event in events),
        self_id=first.self_id,
    )


@dataclass
class _Pending:
    """一个正在等待冲刷的批次。"""

    events: list[InboundEvent] = field(default_factory=list)
    future: Optional[asyncio.Future] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


class DebounceQueue:
    """按 (group_id, user_id) 合并连发消息的队列。

    Args:
        handler: 冲刷时调用的处理器，接收合并后的事件与条数。
        window: 合并窗口秒数（默认 0.3s，落在 200-500ms 的建议区间内）。
        max_batch: 批内消息数上限，达到即立刻冲刷。
        clock_sleep: 可注入的 sleep（测试里用，避免真的等）。
    """

    def __init__(
        self,
        handler: Handler,
        window: float = 0.3,
        max_batch: int = 8,
        clock_sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self.handler = handler
        self.window = max(0.0, float(window))
        self.max_batch = max(1, int(max_batch))
        self._sleep = clock_sleep or asyncio.sleep
        self._pending: dict[str, _Pending] = {}

    # ---------------------------------------------------------- 对外

    async def submit(self, event: InboundEvent) -> Any:
        """把一条事件塞进队列，返回**整批**的处理结果。

        Returns:
            处理器返回值。队列被关闭 / 冲刷被取消时返回 ``None``
            （调用方应视为"这次什么都没做"）。
        """
        key = self.key_of(event)
        pending = self._pending.get(key)

        if pending is None:
            loop = asyncio.get_running_loop()
            pending = _Pending(future=loop.create_future())
            self._pending[key] = pending
            pending.task = loop.create_task(self._flush_later(key, pending))
        elif self._already_seen(pending, event):
            # 宿主重推的同一条消息：不重复入批，但仍等同一份结果
            logger.info("批内判重命中，丢弃重推消息（id=%s）", event.platform_message_id)
            return await asyncio.shield(pending.future)

        pending.events.append(event)
        if len(pending.events) >= self.max_batch:
            pending.ready.set()
        return await asyncio.shield(pending.future)

    def pending_count(self) -> int:
        """当前还有多少批在等冲刷（诊断用）。"""
        return len(self._pending)

    async def close(self) -> None:
        """关闭队列：取消所有待冲刷批次，并让等待方拿到 None。"""
        tasks = []
        for pending in list(self._pending.values()):
            if pending.task is not None:
                pending.task.cancel()
                tasks.append(pending.task)
            self._settle(pending, None)
        self._pending.clear()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ---------------------------------------------------------- 内部

    @staticmethod
    def key_of(event: InboundEvent) -> str:
        """合并维度：同一会话里的同一个用户。

        不能只用 ``Target.key``——群里它是 ``g:<group>``，会把**全群所有人**
        的消息合并成一条，等于把 A 的话算到 B 头上。
        """
        return "{0}:{1}".format(event.group_id or "-", event.user_id)

    @staticmethod
    def _already_seen(pending: _Pending, event: InboundEvent) -> bool:
        if not event.platform_message_id:
            return False
        return any(
            item.platform_message_id == event.platform_message_id
            for item in pending.events
        )

    async def _flush_later(self, key: str, pending: _Pending) -> None:
        """等窗口结束（或批满）后冲刷。任何路径都必须给 future 一个结果。"""
        try:
            try:
                await asyncio.wait_for(pending.ready.wait(), timeout=self.window)
            except asyncio.TimeoutError:
                pass  # 正常路径：窗口到期
            self._pending.pop(key, None)
            if not pending.events:
                self._settle(pending, None)
                return
            merged = merge_events(pending.events)
            try:
                result = await self.handler(merged, len(pending.events))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 处理器不许把等待方拖死
                logger.exception("防抖冲刷处理器异常")
                self._fail(pending, exc)
                return
            self._settle(pending, result)
        except asyncio.CancelledError:
            self._settle(pending, None)
            raise
        finally:
            if not pending.future.done():
                pending.future.set_result(None)

    @staticmethod
    def _settle(pending: _Pending, result: Any) -> None:
        if pending.future is not None and not pending.future.done():
            pending.future.set_result(result)

    @staticmethod
    def _fail(pending: _Pending, exc: BaseException) -> None:
        if pending.future is not None and not pending.future.done():
            pending.future.set_exception(exc)
