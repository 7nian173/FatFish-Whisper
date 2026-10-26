"""撤回拦截器：按 Core 给的 probability 掷骰，撤回刚发出的那条消息。

规则（严格按规格）
------------------
1. 必须先拿到**新发出的 message_id**，才有资格撤回（撤回的是自己刚说的话）。
2. ``random.random() < probability`` 才撤回。
3. **撤回成功之后**才记录冷却时间；冷却期内该用户/群不再做任何撤回判定。
   —— 注意是"撤回后冷却"，不是"判定后冷却"，否则没撤回也会白白进冷却。
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

from .host import HostAPI
from .models import Target

logger = logging.getLogger("ffw.adapter.recall")


@dataclass
class RecallDecision:
    """一次撤回判定的结果（便于日志与测试断言）。

    Attributes:
        attempted: 是否真的做了判定（False 表示被冷却拦下或没有 message_id）。
        rolled: 掷出的随机数。
        probability: Core 给的概率。
        recalled: 是否真的撤回了。
        reason: 人类可读的原因。
    """

    attempted: bool = False
    rolled: float = 1.0
    probability: float = 0.0
    recalled: bool = False
    reason: str = ""


class RecallRoller:
    """撤回掷骰 + 冷却。

    Args:
        host: 宿主能力实现。
        cooldown: 冷却秒数（默认 180 = 3 分钟）。
        rng: 可注入随机源（测试里固定）。
        clock: 可注入时钟（测试里快进）。
    """

    def __init__(
        self,
        host: HostAPI,
        cooldown: float = 180.0,
        rng: Optional[random.Random] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self.host = host
        self.cooldown = max(0.0, float(cooldown))
        self._rng = rng or random.Random()
        self._clock = clock or time.time
        self._last_recall: dict[str, float] = {}

    # ---------------------------------------------------------- 冷却

    def in_cooldown(self, key: str) -> bool:
        last = self._last_recall.get(key)
        if last is None:
            return False
        return (self._clock() - last) < self.cooldown

    def remaining_cooldown(self, key: str) -> float:
        last = self._last_recall.get(key)
        if last is None:
            return 0.0
        return max(0.0, self.cooldown - (self._clock() - last))

    def mark_recalled(self, key: str) -> None:
        self._last_recall[key] = self._clock()

    # ---------------------------------------------------------- 判定

    async def maybe_recall(
        self,
        target: Target,
        message_id: Optional[str],
        probability: float,
        hint: Optional[dict[str, Any]] = None,
    ) -> RecallDecision:
        """按概率决定是否撤回 ``message_id``。永不抛异常。"""
        if not message_id:
            return RecallDecision(reason="no_message_id")
        if probability <= 0.0:
            return RecallDecision(reason="zero_probability", probability=probability)

        key = target.key
        if self.in_cooldown(key):
            return RecallDecision(
                reason="cooldown({0:.0f}s)".format(self.remaining_cooldown(key)),
                probability=probability,
            )

        rolled = self._rng.random()
        if rolled >= probability:
            return RecallDecision(
                attempted=True, rolled=rolled, probability=probability, reason="not_hit"
            )

        try:
            await self.host.recall(target, message_id)
        except Exception as exc:
            logger.warning("撤回失败：%s", exc)
            return RecallDecision(
                attempted=True,
                rolled=rolled,
                probability=probability,
                reason="recall_failed:" + str(exc),
            )

        self.mark_recalled(key)
        logger.info(
            "撤回 %s 的消息 %s（rolled=%.3f < p=%.2f, hint=%s）",
            key,
            message_id,
            rolled,
            probability,
            hint,
        )
        return RecallDecision(
            attempted=True,
            rolled=rolled,
            probability=probability,
            recalled=True,
            reason="recalled",
        )
