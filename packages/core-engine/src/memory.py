"""兼容层：阶段一的 ``memory.py`` 已被 ``memory_router.py`` 取代（阶段五）。

保留这个文件只为两件事：
1. 老的 ``import memory`` 不会炸（CI 的 import 检查、第三方脚本可能还在用）；
2. 旧的 ``MemoryRouter(cache_dir)`` 调用方式仍能工作 —— 内部转接到新的
   :class:`memory_router.MemoryRouter` + :class:`LocalSQLiteProvider`。

新代码请直接用 ``from memory_router import MemoryRouter, LocalSQLiteProvider``。
"""
from __future__ import annotations

import os
from typing import Any, Optional

from memory_router import (  # noqa: F401  (重新导出，保持旧引用可用)
    MEMORY_BLOCK_HEADER,
    MEMORY_MAX_INJECT,
    MEMORY_TTL_DAYS,
    ExternalPluginProvider,
    HostNativeProvider,
    IMemoryProvider,
    LocalSQLiteProvider,
    MemoryItem,
    MemoryRouter as _Router,
)

__all__ = [
    "MemoryRouter",
    "LocalMemory",
    "LocalSQLiteProvider",
    "HostNativeProvider",
    "ExternalPluginProvider",
    "IMemoryProvider",
    "MEMORY_BLOCK_HEADER",
    "MEMORY_TTL_DAYS",
    "MEMORY_MAX_INJECT",
    "MemoryItem",
]


class LocalMemory:
    """旧接口适配：``LocalMemory(cache_dir)`` -> 本地 SQLite 兜底。

    注意：阶段一的 diskcache 版本已废弃，现在统一走 SQLite（可 TTL、可统计）。
    """

    def __init__(self, cache_dir: str, max_turns: int = 8) -> None:
        db_path = os.path.join(cache_dir, "memory.db")
        self.provider = LocalSQLiteProvider(db_path)
        self.max_turns = max_turns

    def append(self, user_id: str, role: str, content: str) -> None:
        import asyncio

        asyncio.run(self.provider.store(user_id, str(content)))

    def recent(self, user_id: str, limit: int = 6) -> list[dict[str, Any]]:
        import asyncio

        facts = asyncio.run(self.provider.retrieve(user_id, ""))
        return [{"role": "user", "content": fact} for fact in facts[-limit:]]

    def summary(self, user_id: str) -> str:
        return self.provider.digest(user_id, limit=self.max_turns)

    def clear(self, user_id: str) -> None:
        """兼容占位：SQLite 版本请用 ``purge_expired()`` 或直接删库。"""


class MemoryRouter(_Router):
    """旧签名 ``MemoryRouter(cache_dir)`` 的适配。

    新写法请用 :meth:`memory_router.MemoryRouter.build`。
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        external: Optional[Any] = None,
        max_turns: int = 8,
    ) -> None:
        directory = cache_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
        local = LocalSQLiteProvider(os.path.join(directory, "memory.db"))
        providers: list[Any] = []
        if external is not None:
            providers.append(HostNativeProvider(external))
        super().__init__(providers, local)
        self.local_memory = LocalMemory(directory, max_turns=max_turns)
