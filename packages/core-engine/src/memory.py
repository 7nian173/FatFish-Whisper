"""记忆路由。

优先级：宿主原生记忆（由 adapter 注入）> 外部记忆插件（Mem0 等）> 本地兜底。

本地兜底用 diskcache 存最近若干轮对话，进程重启不丢，也不占内存。
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from diskcache import Cache

DEFAULT_MAX_TURNS = 8


class LocalMemory:
    """基于 diskcache 的本地轻量记忆。"""

    def __init__(self, cache_dir: str, max_turns: int = DEFAULT_MAX_TURNS):
        os.makedirs(cache_dir, exist_ok=True)
        self._cache = Cache(os.path.join(cache_dir, "memory"))
        self.max_turns = max_turns

    def _key(self, user_id: str) -> str:
        return "turns:" + user_id

    def append(self, user_id: str, role: str, content: str) -> None:
        turns = self._cache.get(self._key(user_id)) or []
        turns.append({"role": role, "content": content})
        if len(turns) > self.max_turns:
            turns = turns[-self.max_turns:]
        self._cache.set(self._key(user_id), turns)

    def recent(self, user_id: str, limit: int = 6) -> list[dict[str, Any]]:
        turns = self._cache.get(self._key(user_id)) or []
        return turns[-limit:]

    def summary(self, user_id: str) -> str:
        turns = self.recent(user_id)
        if not turns:
            return "（还没有可用的历史记忆）"
        return "\n".join(t["role"] + ": " + t["content"] for t in turns)

    def clear(self, user_id: str) -> None:
        self._cache.delete(self._key(user_id))


class MemoryRouter:
    """统一入口：优先外部记忆，失败自动降级到本地。"""

    def __init__(self, cache_dir: str, external: Optional[Any] = None,
                 max_turns: int = DEFAULT_MAX_TURNS):
        # external 需要实现 search(user_id, query, limit) / add(user_id, text) 两个方法
        self.external = external
        self.local = LocalMemory(cache_dir, max_turns=max_turns)

    def remember(self, user_id: str, user_message: str, bot_reply: str) -> None:
        self.local.append(user_id, "user", user_message)
        self.local.append(user_id, "assistant", bot_reply)
        if self.external is not None:
            try:
                self.external.add(user_id, user_message)
            except Exception:
                pass  # 外部插件挂了不影响主流程

    def recall(self, user_id: str, query: str = "", limit: int = 6) -> list[dict[str, Any]]:
        if self.external is not None:
            try:
                hits = self.external.search(user_id, query, limit)
                if hits:
                    return hits
            except Exception:
                pass
        return self.local.recent(user_id, limit)

    def as_text(self, user_id: str) -> str:
        return self.local.summary(user_id)
