"""Adapter 主服务：把「透传 -> 执行 -> 撤回」串成一条流水线。

```
宿主事件
   │  InboundEvent(platform_message_id, ...)
   ▼
CoreClient.ask ──超时/5xx──> CoreReply.fallback（预设话术或原生 LLM）
   │
   ├─ duplicate? ──是──> 直接返回，一个动作都不执行
   ▼
ActionExecutor.run ──> text / poke / sticker（捕获 message_id）
   │
   ▼
RecallRoller.maybe_recall（有 message_id + 有 recall_hint 才判定）
```
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .config import AdapterConfig
from .core_client import CoreClient, NativeLLM
from .executor import ActionExecutor, ExecutionResult
from .host import HostAPI
from .models import CoreReply, InboundEvent
from .recall import RecallDecision, RecallRoller
from .sanitize import clean_cq

logger = logging.getLogger("ffw.adapter")


@dataclass
class TurnResult:
    """一次消息处理的完整结果（给宿主/测试用）。

    Attributes:
        event: 原始入站事件。
        reply: Core 的响应（可能是 duplicate / fallback）。
        execution: 动作执行战果。
        recall: 撤回判定结果。
    """

    event: InboundEvent
    reply: CoreReply
    execution: ExecutionResult
    recall: Optional[RecallDecision] = None

    @property
    def skipped_duplicate(self) -> bool:
        return self.reply.duplicate

    @property
    def degraded(self) -> bool:
        return self.reply.is_fallback

    def summary(self) -> dict[str, Any]:
        return {
            "platform_message_id": self.event.platform_message_id,
            "status": self.reply.status,
            "duplicate": self.reply.duplicate,
            "degraded": self.degraded,
            "sent": list(self.execution.sent_texts),
            "sent_message_ids": list(self.execution.sent_message_ids),
            "failed_texts": list(self.execution.failed_texts),
            "pokes": self.execution.pokes,
            "stickers": list(self.execution.stickers),
            "failed_stickers": list(self.execution.failed_stickers),
            "errors": list(self.execution.errors),
            "recall": self.recall.reason if self.recall else None,
            "recalled": bool(self.recall and self.recall.recalled),
            "emotion": self.reply.debug.get("emotion"),
            "level": self.reply.debug.get("level"),
            "inner_os": self.reply.inner_os,
        }


class FatFishAdapter:
    """Adapter 门面：宿主只需要调 :meth:`on_event`。

    Args:
        host: 宿主能力实现（``MockHostAPI`` 或你自己对接 NapCat 的类）。
        config: 运行参数。
        native_llm: 降级用的宿主原生 LLM 回调。
        core: 可注入的 CoreClient（测试里注入 ASGITransport）。
    """

    def __init__(
        self,
        host: HostAPI,
        config: Optional[AdapterConfig] = None,
        native_llm: Optional[NativeLLM] = None,
        core: Optional[CoreClient] = None,
        executor: Optional[ActionExecutor] = None,
        roller: Optional[RecallRoller] = None,
    ) -> None:
        self.config = config or AdapterConfig()
        self.host = host
        if core is not None:
            # 注入 core 时也别丢掉 native_llm（否则降级永远走预设话术，很难察觉）
            if native_llm is not None:
                core.native_llm = native_llm
            self.core = core
        else:
            self.core = CoreClient(self.config, native_llm=native_llm)
        self.executor = executor or ActionExecutor(
            host, typing_delay=self.config.typing_delay
        )
        self.roller = roller or RecallRoller(host, cooldown=self.config.recall_cooldown)

    async def close(self) -> None:
        await self.core.close()

    # ---------------------------------------------------------- 主流程

    async def on_event(self, event: InboundEvent) -> TurnResult:
        """处理一条宿主消息事件。**永不抛异常**。"""
        prepared = self._prepare(event)
        if prepared is None:
            # 清洗后无可判定内容（纯 @ / 纯表情）：不发消息、不请求 Core、不计 Token
            return self._skipped(event)
        event = prepared
        target = event.target

        reply = await self.core.ask(event)

        if reply.duplicate:
            logger.info(
                "入口判重命中，跳过执行（platform_message_id=%s）",
                event.platform_message_id,
            )
            return TurnResult(event, reply, ExecutionResult())

        execution = await self.executor.run(reply, target)

        decision: Optional[RecallDecision] = None
        probability = reply.recall_probability
        if probability > 0.0 and execution.last_message_id:
            decision = await self.roller.maybe_recall(
                target, execution.last_message_id, probability, reply.recall_hint
            )

        return TurnResult(event, reply, execution, decision)

    # ---------------------------------------------------------- 清洗

    def _prepare(self, event: InboundEvent) -> Optional[InboundEvent]:
        """剥离 CQ 码。清洗后没内容就返回 ``None``（调用方跳过本次判定）。

        为什么在 Adapter 做而不是让宿主做：宿主往往直接把 ``raw_message``
        透传，而 ``[CQ:at,qq=123]`` 会让 Track A 词典误命中、让 Track B 把
        ``qq=123`` 当成用户说的话（47 功能点第 1 条 / 裁决 A-10 拆件吸收）。
        """
        cleaned = clean_cq(event.message)
        if not cleaned:
            logger.info(
                "清洗后无可判定内容，跳过（id=%s, user=%s）",
                event.platform_message_id,
                event.user_id,
            )
            return None
        if cleaned == event.message:
            return event
        return InboundEvent(
            platform_message_id=event.platform_message_id,
            user_id=event.user_id,
            message=cleaned,
            group_id=event.group_id,
            is_at_bot=event.is_at_bot,
            self_id=event.self_id,
        )

    @staticmethod
    def _skipped(event: InboundEvent) -> TurnResult:
        """构造「跳过」结果：不发消息、不请求 Core、不计 Token。"""
        reply = CoreReply(
            status="skipped",
            actions=[],
            inner_os="",
            recall_hint=None,
            debug={"reason": "empty_after_sanitize"},
            raw={"status": "skipped", "reason": "empty_after_sanitize"},
        )
        return TurnResult(event, reply, ExecutionResult())

    async def on_message(
        self,
        user_id: str,
        message: str,
        platform_message_id: Optional[str] = None,
        group_id: Optional[str] = None,
        is_at_bot: bool = False,
    ) -> TurnResult:
        """便捷入口：宿主只有一个 ``(user_id, message)`` 回调时用这个。

        ``platform_message_id`` 强烈建议传 —— 不传就只能靠文本窗口判重。
        """
        event = InboundEvent(
            platform_message_id=str(platform_message_id or ""),
            user_id=str(user_id),
            message=message,
            group_id=None if group_id is None else str(group_id),
            is_at_bot=is_at_bot,
        )
        return await self.on_event(event)
