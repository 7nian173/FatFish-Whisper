"""Token 监控切面：把每次 LLM 调用的 usage 记进 ``token_logs``。

设计
----
- :class:`TokenMonitor` 只管「记账 + 聚合」，不碰业务；
- :func:`track_tokens` 是异步装饰器，套在任何「返回 (结果, usage)」的协程上；
- 记账失败**永不**影响主流程（token 账本丢了也不能让对话挂掉）；
- 模块标签统一用 ``MOD_*`` 常量，看板按标签分组。

为什么不用中间件拦截 httpx
--------------------------
httpx 的 ``response`` 钩子拿不到"这次调用属于哪个模块"，而 token 账本最有价值的
恰恰是模块维度。所以切面放在**调用点**（TrackB / Evolve），标签显式传。
"""
from __future__ import annotations

import asyncio
import datetime
import functools
import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar

from sqlalchemy import func, select

from models import TokenLog, utcnow

logger = logging.getLogger("ffw.monitor")

MOD_TRACK_B = "MOD_TRACK_B"      # 语义判定轨
MOD_EVOLVE = "MOD_EVOLVE"        # 7 日演化（生成追加词）
MOD_OOC = "MOD_OOC"              # OOC 探针
MOD_COMPRESS = "MOD_COMPRESS"    # 人设卡压缩

T = TypeVar("T")

Usage = tuple[int, int]  # (prompt_tokens, completion_tokens)


def parse_usage(payload: Any) -> Usage:
    """从 OpenAI 兼容响应里安全提取 usage。"""
    if not isinstance(payload, dict):
        return (0, 0)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (0, 0)

    def _int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    return (_int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens")))


class TokenMonitor:
    """Token 账本。

    Args:
        session_factory: SQLAlchemy ``sessionmaker``；为 None 时只做内存记账
            （纯单元测试用）。
    """

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        self.Session = session_factory
        self._memory: list[dict[str, Any]] = []

    # ---------------------------------------------------------- 记账

    def record(
        self,
        module: str,
        usage: Usage,
        user_id: str = "",
        created_at: Optional[datetime.datetime] = None,
    ) -> None:
        """同步记账（内部已吞掉所有异常）。"""
        prompt_tokens, completion_tokens = usage
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        row = {
            "user_id": str(user_id or ""),
            "module": str(module or "MOD_UNKNOWN"),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "created_at": created_at or utcnow(),
        }
        self._memory.append(row)
        if self.Session is None:
            return
        try:
            with self.Session() as session:
                session.add(TokenLog(**row))
                session.commit()
        except Exception:
            logger.warning("token 记账失败（不影响主流程）", exc_info=True)

    async def arecord(
        self,
        module: str,
        usage: Usage,
        user_id: str = "",
        created_at: Optional[datetime.datetime] = None,
    ) -> None:
        """异步记账：把阻塞的 SQLite 写入丢到线程池。"""
        await asyncio.to_thread(self.record, module, usage, user_id, created_at)

    # ---------------------------------------------------------- 聚合

    def stats_today(
        self,
        day: Optional[datetime.date] = None,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> dict[str, dict[str, int]]:
        """返回指定日期（默认今天，UTC）各模块的消耗。

        Returns:
            ``{module: {"prompt_tokens", "completion_tokens", "total_tokens", "calls"}}``
        """
        target_day = day or utcnow().date()
        factory = session_factory or self.Session
        if factory is None:
            return self._stats_from_memory(target_day)

        start = datetime.datetime.combine(target_day, datetime.time.min)
        end = start + datetime.timedelta(days=1)
        try:
            with factory() as session:
                rows = session.execute(
                    select(
                        TokenLog.module,
                        func.sum(TokenLog.prompt_tokens),
                        func.sum(TokenLog.completion_tokens),
                        func.count(TokenLog.id),
                    )
                    .where(TokenLog.created_at >= start, TokenLog.created_at < end)
                    .group_by(TokenLog.module)
                ).all()
        except Exception:
            logger.warning("token 聚合失败，回退到内存账本", exc_info=True)
            return self._stats_from_memory(target_day)

        result: dict[str, dict[str, int]] = {}
        for module, prompt_tokens, completion_tokens, calls in rows:
            prompt_value = int(prompt_tokens or 0)
            completion_value = int(completion_tokens or 0)
            result[str(module or "MOD_UNKNOWN")] = {
                "prompt_tokens": prompt_value,
                "completion_tokens": completion_value,
                "total_tokens": prompt_value + completion_value,
                "calls": int(calls or 0),
            }
        return result

    def _stats_from_memory(self, day: datetime.date) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for row in self._memory:
            moment = row["created_at"]
            if isinstance(moment, datetime.datetime) and moment.date() != day:
                continue
            bucket = result.setdefault(
                row["module"],
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
            )
            bucket["prompt_tokens"] += row["prompt_tokens"]
            bucket["completion_tokens"] += row["completion_tokens"]
            bucket["total_tokens"] += row["prompt_tokens"] + row["completion_tokens"]
            bucket["calls"] += 1
        return result

    @staticmethod
    def summary_dict(stats: dict[str, dict[str, int]]) -> dict[str, Any]:
        """把分模块统计包成看板友好的结构（含总计）。"""
        total_prompt = sum(item["prompt_tokens"] for item in stats.values())
        total_completion = sum(item["completion_tokens"] for item in stats.values())
        return {
            "day": utcnow().date().isoformat(),
            "modules": stats,
            "total": {
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
                "calls": sum(item["calls"] for item in stats.values()),
            },
        }


def track_tokens(
    monitor: TokenMonitor,
    module: str,
    user_id: str = "",
) -> Callable[[Callable[..., Awaitable[tuple[T, Usage]]]], Callable[..., Awaitable[T]]]:
    """异步装饰器：套在返回 ``(结果, usage)`` 的协程上，自动记账。

    用法::

        @track_tokens(monitor, MOD_EVOLVE, user_id="10001")
        async def call_llm(...) -> tuple[str, Usage]:
            ...
    """

    def decorator(
        func: Callable[..., Awaitable[tuple[T, Usage]]]
    ) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            result, usage = await func(*args, **kwargs)
            await monitor.arecord(module, usage, user_id)
            return result

        return wrapper

    return decorator
