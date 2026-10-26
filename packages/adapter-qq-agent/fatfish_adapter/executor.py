"""动作执行器：按顺序执行 Core 返回的 actions 数组。

支持的三种动作
--------------
- ``text``    发文本，**捕获返回的新 message_id**（撤回要用）。
- ``poke``    拍一拍（默认拍发消息的人）。
- ``sticker`` 发表情包。

拟人化
------
有多条 text 动作时，逐条发送前随机等 500-1500ms，避免"刷屏式"连发暴露机器人身份。
单条 text 不加延迟（用户等不起）。
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Optional

from .host import HostAPI
from .models import Action, CoreReply, Target

logger = logging.getLogger("ffw.adapter.executor")


@dataclass
class ExecutionResult:
    """一次执行的战果。

    Attributes:
        sent_message_ids: 本次新发出的消息 ID（按发送顺序），撤回取最后一个。
        sent_texts: **确认送达**的文本（宿主返回了 message_id 才计入）。
        failed_texts: 发送失败/未拿到回执的文本（A-A6：不产生假回执）。
        pokes: 拍一拍次数。
        stickers: **确认送达**的表情包 tag。
        failed_stickers: 发送失败/未拿到回执的表情包 tag。
        errors: 单个动作失败的信息（不影响其它动作继续执行）。
    """

    sent_message_ids: list[str] = field(default_factory=list)
    sent_texts: list[str] = field(default_factory=list)
    failed_texts: list[str] = field(default_factory=list)
    pokes: int = 0
    stickers: list[str] = field(default_factory=list)
    failed_stickers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def last_message_id(self) -> Optional[str]:
        return self.sent_message_ids[-1] if self.sent_message_ids else None


class ActionExecutor:
    """把 ``actions`` 数组翻译成宿主 API 调用。

    Args:
        host: 宿主能力实现。
        typing_delay: 多条文本时的延迟区间（秒）。
        sleep: 可注入的 sleep（测试里换成零延迟）。
        rng: 可注入的随机源（测试里固定）。
    """

    def __init__(
        self,
        host: HostAPI,
        typing_delay: tuple[float, float] = (0.5, 1.5),
        sleep: Optional[Any] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.host = host
        self.typing_delay = typing_delay
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()

    async def run(self, reply: CoreReply, target: Target) -> ExecutionResult:
        """按顺序执行全部动作；单个动作失败不中断后续动作。"""
        result = ExecutionResult()
        if reply.duplicate:
            # 幽灵消息：一个动作都不许执行
            return result

        actions = reply.actions
        text_total = sum(1 for action in actions if action.type == "text")
        text_index = 0

        for action in actions:
            try:
                if action.type == "text":
                    text_index += 1
                    if text_total > 1:
                        await self._typing_pause()
                    message_id = await self.host.send_text(target, action.content)
                    if message_id:
                        result.sent_message_ids.append(str(message_id))
                        result.sent_texts.append(action.content)
                    else:
                        # A-A6：拿不到回执 = 未确认送达，绝不记成已发
                        result.failed_texts.append(action.content)
                        result.errors.append("text:no_receipt")
                        logger.warning(
                            "send_text 未返回 message_id，按未送达记账：%s",
                            action.content[:20],
                        )

                elif action.type == "poke":
                    await self.host.send_poke(target, action.target or target.user_id)
                    result.pokes += 1

                elif action.type == "sticker":
                    message_id = await self.host.send_sticker(target, action.tag)
                    if message_id:
                        result.sent_message_ids.append(str(message_id))
                        result.stickers.append(action.tag)
                    else:
                        # 与 text 同理：无回执不进战果（宿主不支持时返回 None）
                        result.failed_stickers.append(action.tag)
                        result.errors.append("sticker:no_receipt")
                        logger.warning(
                            "send_sticker 未返回 message_id，按未送达记账：%s", action.tag
                        )

                else:
                    logger.warning("未知动作类型，跳过：%s", action.type)
                    result.errors.append("unknown_action:" + action.type)
            except Exception as exc:  # 单个动作失败不能拖垮整条回复
                logger.exception("动作执行失败：%s", action.type)
                result.errors.append(action.type + ":" + str(exc))

        return result

    async def _typing_pause(self) -> None:
        low, high = self.typing_delay
        low = max(0.0, float(low))
        high = max(low, float(high))
        await self._sleep(self._rng.uniform(low, high))
