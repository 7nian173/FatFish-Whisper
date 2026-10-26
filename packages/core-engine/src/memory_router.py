"""插拔式记忆路由：宿主原生 > 外部插件 > 本地 SQLite 兜底。

三级降级
--------
1. :class:`HostNativeProvider`  —— 宿主（NapCat / NoneBot 插件）自带记忆 API，直接借用；
2. :class:`ExternalPluginProvider` —— Mem0 之类的 HTTP 记忆服务；
3. :class:`LocalSQLiteProvider` —— **永远可用**的兜底：SQLite + LRU + 7 天 TTL。

注入隔离（重要）
----------------
记忆**绝不能**拼进 System Prompt 的静态前缀（会毁掉前缀缓存、也会污染人设）。
它只能作为动态尾部的一个独立块：

    [相关回忆]：
    - 用户说他最喜欢吃白饭
    - 用户下周三要考试

由 :meth:`MemoryRouter.as_prompt_block` 产出，交给 PromptBuilder 追加。
"""
from __future__ import annotations

import abc
import asyncio
import datetime
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sanitize import clean_memory_item, clean_memory_items

logger = logging.getLogger("ffw.memory")

MEMORY_TTL_DAYS: float = 7.0      # 记忆存活 7 天
MEMORY_MAX_ITEMS: int = 200       # 单用户最多保留多少条（LRU 淘汰）
MEMORY_MAX_INJECT: int = 5        # 单次最多注入几条，防止 Prompt 膨胀

MEMORY_BLOCK_HEADER = "[相关回忆]："

#: 情绪显著性达到该值即 Pin（不参与 TTL / LRU 淘汰）。
#: 由 EmotionEngine 在 L2 / CRISIS 时传 1.0，普通情绪传 0.0。
PIN_SALIENCE_THRESHOLD: float = 0.8


def _utcnow() -> datetime.datetime:
    """naive UTC 当前时间（避开 3.12+ 已弃用的 ``datetime.utcnow()``）。"""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)



# ---------------------------------------------------------------- 接口

class IMemoryProvider(abc.ABC):
    """记忆提供者接口。所有实现都必须**永不抛异常**（内部降级）。"""

    #: 人类可读的名字，出现在日志/看板里
    name: str = "base"

    @abc.abstractmethod
    async def retrieve(self, user_id: str, query: str) -> list[str]:
        """按 query 取相关记忆，按相关度/时间倒序，最多几条由实现决定。"""

    @abc.abstractmethod
    async def store(self, user_id: str, fact: str, salience: float = 0.0) -> bool:
        """写入一条记忆；成功返回 True。"""

    @abc.abstractmethod
    def health(self) -> bool:
        """当前是否可用（配置齐全且连接正常）。"""


# ---------------------------------------------------------------- 本地兜底

@dataclass
class MemoryItem:
    """一条记忆（本地库的行对象）。"""

    id: int
    user_id: str
    fact: str
    created_at: datetime.datetime
    last_access: datetime.datetime
    hits: int
    #: 是否被 Pin（情绪显著时刻，不参与 TTL / LRU 淘汰）
    pinned: int = 0

    def is_expired(self, now: Optional[datetime.datetime] = None, ttl_days: float = MEMORY_TTL_DAYS) -> bool:
        if self.pinned:
            return False  # Pin 的条目永不过期
        moment = now or _utcnow()
        return (moment - self.created_at).total_seconds() > ttl_days * 86400


class LocalSQLiteProvider(IMemoryProvider):
    """本地兜底：SQLite + TTL(7 天) + LRU 淘汰。

    Args:
        db_path: SQLite 文件路径。**建议与 app.db 分开**（``memory.db``），
            避免和 SQLAlchemy 的连接抢写锁。
        ttl_days: 存活天数，默认 7。
        max_items: 单用户上限，超出按 ``last_access`` 淘汰最久未用的。
        clock: 可注入时钟（测试里快进）。
    """

    name = "local_sqlite"

    def __init__(
        self,
        db_path: str,
        ttl_days: float = MEMORY_TTL_DAYS,
        max_items: int = MEMORY_MAX_ITEMS,
        clock: Optional[Any] = None,
    ) -> None:
        self.db_path = db_path
        self.ttl_days = float(ttl_days)
        self.max_items = int(max_items)
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_schema()

    # ---------------------------------------------------------- 底层

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id TEXT NOT NULL,"
                " fact TEXT NOT NULL,"
                " created_at TEXT NOT NULL,"
                " last_access TEXT NOT NULL,"
                " hits INTEGER NOT NULL DEFAULT 0,"
                " pinned INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id, created_at)"
            )
            # 老库迁移：补 pinned 列（SQLite 没有 IF NOT EXISTS for column）
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            if "pinned" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
                logger.info("记忆库已迁移：新增 pinned 列")

    # ---------------------------------------------------------- 接口实现

    def health(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def store(self, user_id: str, fact: str, salience: float = 0.0) -> bool:
        fact = (fact or "").strip()
        if not fact or not user_id:
            return False
        return await asyncio.to_thread(self._store_sync, str(user_id), fact, float(salience or 0.0))

    def _store_sync(self, user_id: str, fact: str, salience: float = 0.0) -> bool:
        now = self._clock().isoformat(sep=" ")
        pinned = 1 if salience >= PIN_SALIENCE_THRESHOLD else 0
        try:
            with self._lock, self._connect() as conn:
                # 同一句话不重复存（幂等）；已 Pin 的条目保持 Pin
                exists = conn.execute(
                    "SELECT id, pinned FROM memories WHERE user_id=? AND fact=? LIMIT 1",
                    (user_id, fact),
                ).fetchone()
                if exists:
                    conn.execute(
                        "UPDATE memories SET last_access=?, pinned=MAX(pinned, ?) WHERE id=?",
                        (now, pinned, exists["id"]),
                    )
                    return True
                conn.execute(
                    "INSERT INTO memories(user_id, fact, created_at, last_access, hits, pinned)"
                    " VALUES (?,?,?,?,0,?)",
                    (user_id, fact, now, now, pinned),
                )
                self._evict_lru(conn, user_id)
            return True
        except Exception:
            logger.warning("本地记忆写入失败", exc_info=True)
            return False

    def _evict_lru(self, conn: sqlite3.Connection, user_id: str) -> None:
        # Pin 的条目（情绪显著时刻）不参与淘汰
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE user_id=? AND pinned=0", (user_id,)
        ).fetchone()["n"]
        overflow = int(total) - self.max_items
        if overflow <= 0:
            return
        conn.execute(
            "DELETE FROM memories WHERE id IN ("
            " SELECT id FROM memories WHERE user_id=? AND pinned=0"
            " ORDER BY last_access ASC LIMIT ?)",
            (user_id, overflow),
        )

    async def retrieve(self, user_id: str, query: str) -> list[str]:
        return await asyncio.to_thread(self._retrieve_sync, str(user_id), query or "")

    def _retrieve_sync(self, user_id: str, query: str) -> list[str]:
        try:
            now = self._clock()
            cutoff = (now - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
            with self._lock, self._connect() as conn:
                # TTL：过期即清（顺手做，省得单独跑定时任务）；**Pin 的条目永不过期**
                conn.execute(
                    "DELETE FROM memories WHERE user_id=? AND created_at < ? AND pinned=0",
                    (user_id, cutoff),
                )
                rows = conn.execute(
                    "SELECT * FROM memories WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, max(self.max_items, MEMORY_MAX_INJECT)),
                ).fetchall()

            items = [
                MemoryItem(
                    id=row["id"],
                    user_id=row["user_id"],
                    fact=row["fact"],
                    created_at=datetime.datetime.fromisoformat(row["created_at"]),
                    last_access=datetime.datetime.fromisoformat(row["last_access"]),
                    hits=int(row["hits"]),
                    pinned=int(row["pinned"] or 0) if "pinned" in row.keys() else 0,
                )
                for row in rows
            ]
            ranked = self._rank(items, query)
            selected = ranked[:MEMORY_MAX_INJECT]

            if selected:
                with self._lock, self._connect() as conn:
                    for item in selected:
                        conn.execute(
                            "UPDATE memories SET last_access=?, hits=hits+1 WHERE id=?",
                            (now.isoformat(sep=" "), item.id),
                        )
            return [item.fact for item in selected]
        except Exception:
            logger.warning("本地记忆检索失败", exc_info=True)
            return []

    @staticmethod
    def _rank(items: list[MemoryItem], query: str) -> list[MemoryItem]:
        """极简相关度：字面重叠 + 命中次数 + 新鲜度。

        没上 embedding —— 本地兜底要的是「零依赖、够快、够稳」，
        真要好效果请接 ExternalPluginProvider（Mem0 之类）。
        """
        tokens = {ch for ch in (query or "") if ch.strip()}
        now = _utcnow()

        def score(item: MemoryItem) -> float:
            overlap = len(tokens & set(item.fact)) / (len(tokens) or 1)
            freshness = 1.0 / (1.0 + (now - item.created_at).total_seconds() / 86400.0)
            return overlap * 2.0 + min(item.hits, 5) * 0.1 + freshness

        return sorted(items, key=score, reverse=True)

    # ---------------------------------------------------------- 运维

    def count(self, user_id: str) -> int:
        """当前有效记忆条数（测试/看板用）。"""
        with self._connect() as conn:
            cutoff = (self._clock() - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE user_id=? AND created_at >= ?",
                (user_id, cutoff),
            ).fetchone()
        return int(row["n"])

    def purge_expired(self) -> int:
        """清掉所有用户的过期记忆，返回删除条数。"""
        cutoff = (self._clock() - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def digest(self, user_id: str, limit: int = 20) -> str:
        """给演化系统用的记忆摘要（纯文本，按时间正序）。"""
        cutoff = (self._clock() - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fact FROM memories WHERE user_id=? AND created_at >= ?"
                " ORDER BY created_at ASC LIMIT ?",
                (user_id, cutoff, limit),
            ).fetchall()
        return "\n".join("- " + row["fact"] for row in rows)


# ---------------------------------------------------------------- 宿主原生

class HostNativeProvider(IMemoryProvider):
    """借用宿主（NapCat / NoneBot 插件）自带的记忆 API。

    宿主只需提供一个 ``retrieve`` / ``store`` 两个可 await 的方法。
    """

    name = "host_native"

    def __init__(self, client: Optional[Any] = None) -> None:
        self.client = client

    def health(self) -> bool:
        return self.client is not None

    async def retrieve(self, user_id: str, query: str) -> list[str]:
        if self.client is None:
            return []
        try:
            result = await self.client.retrieve(user_id=user_id, query=query)
            return [str(item) for item in (result or [])]
        except Exception:
            logger.warning("宿主记忆检索失败", exc_info=True)
            return []

    async def store(self, user_id: str, fact: str, salience: float = 0.0) -> bool:
        if self.client is None:
            return False
        try:
            await self.client.store(user_id=user_id, fact=fact)
            return True
        except Exception:
            logger.warning("宿主记忆写入失败", exc_info=True)
            return False


# ---------------------------------------------------------------- 外部插件

class ExternalPluginProvider(IMemoryProvider):
    """Mem0 等外部 HTTP 记忆服务（OpenAI 兼容风格的极简封装）。

    Args:
        base_url: 服务地址，如 ``http://127.0.0.1:8080``。
        api_key: 可选 Bearer。
        timeout: 请求超时（秒）。
        client: 可注入的 httpx.AsyncClient（测试用）。
    """

    name = "external_plugin"

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 2.0,
        client: Optional[Any] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        self._client = client

    def health(self) -> bool:
        return bool(self.base_url)

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    async def retrieve(self, user_id: str, query: str) -> list[str]:
        if not self.health():
            return []
        try:
            client = await self._http()
            response = await client.post(
                self.base_url + "/search",
                json={"user_id": user_id, "query": query, "limit": MEMORY_MAX_INJECT},
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            logger.warning("外部记忆检索失败，降级到下一级", exc_info=True)
            return []

        items = data.get("results") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        facts: list[str] = []
        for item in items:
            if isinstance(item, str):
                facts.append(item)
            elif isinstance(item, dict):
                text = item.get("memory") or item.get("text") or item.get("content")
                if text:
                    facts.append(str(text))
        return facts

    async def store(self, user_id: str, fact: str, salience: float = 0.0) -> bool:
        if not self.health():
            return False
        try:
            client = await self._http()
            response = await client.post(
                self.base_url + "/add",
                json={"user_id": user_id, "text": fact},
                headers=self._headers(),
            )
            response.raise_for_status()
            return True
        except Exception:
            logger.warning("外部记忆写入失败", exc_info=True)
            return False


# ---------------------------------------------------------------- 路由

class MemoryRouter:
    """按优先级降级的记忆路由。

    Args:
        providers: 提供者列表，按优先级从高到低。
        local: 兜底提供者（写死为 LocalSQLiteProvider，保证「永远能记住」）。
    """

    def __init__(
        self,
        providers: Optional[Iterable[IMemoryProvider]] = None,
        local: Optional[LocalSQLiteProvider] = None,
    ) -> None:
        self.providers: list[IMemoryProvider] = [
            provider for provider in (providers or []) if provider is not None
        ]
        self.local = local
        if self.local is not None:
            self.providers.append(self.local)

    # ---------------------------------------------------------- 构造

    @classmethod
    def build(
        cls,
        db_path: str,
        host_client: Optional[Any] = None,
        external_base_url: str = "",
        external_api_key: str = "",
        ttl_days: float = MEMORY_TTL_DAYS,
    ) -> "MemoryRouter":
        """按配置组装一条降级链：宿主 > 外部 > 本地。"""
        chain: list[IMemoryProvider] = []
        if host_client is not None:
            chain.append(HostNativeProvider(host_client))
        if external_base_url:
            chain.append(ExternalPluginProvider(external_base_url, external_api_key))
        return cls(chain, LocalSQLiteProvider(db_path, ttl_days=ttl_days))

    # ---------------------------------------------------------- 读写

    async def retrieve(self, user_id: str, query: str = "") -> list[str]:
        """按优先级取记忆；**第一个返回非空结果的提供者胜出**。"""
        for provider in self.providers:
            if not provider.health():
                continue
            facts = await provider.retrieve(user_id, query)
            if facts:
                logger.debug("记忆命中：%s（%d 条）", provider.name, len(facts))
                return facts[:MEMORY_MAX_INJECT]
        return []

    async def store(self, user_id: str, fact: str, salience: float = 0.0) -> bool:
        """写记忆：**写所有可用提供者**（外部挂了也不影响本地留存）。

        写入前统一清洗：去控制字符、压平换行、降级段标记、截断超长内容。
        这是**第一道**防线；渲染侧 ``prompt_builder`` 还有第二道。
        """
        cleaned = clean_memory_item(fact)
        if not cleaned:
            return False
        stored = False
        for provider in self.providers:
            if not provider.health():
                continue
            try:
                if await provider.store(user_id, cleaned, salience):
                    stored = True
            except Exception:
                logger.warning("记忆写入异常：%s", provider.name, exc_info=True)
        return stored

    # ---------------------------------------------------------- 注入

    @staticmethod
    def as_prompt_block(facts: list[str]) -> str:
        """把记忆拼成**独立的一段**，绝不混进人设前缀。"""
        cleaned = clean_memory_items(facts)
        if not cleaned:
            return ""
        lines = "\n".join("- " + fact for fact in cleaned[:MEMORY_MAX_INJECT])
        return MEMORY_BLOCK_HEADER + "\n" + lines

    async def prompt_block(self, user_id: str, query: str = "") -> str:
        return self.as_prompt_block(await self.retrieve(user_id, query))

    def digest(self, user_id: str, limit: int = 20) -> str:
        """演化系统用的记忆摘要（只取本地兜底，外部服务的摘要由它自己出）。"""
        if self.local is None:
            return ""
        return self.local.digest(user_id, limit=limit)

    def health(self) -> dict[str, bool]:
        return {provider.name: provider.health() for provider in self.providers}

    # ---------------------------------------------------------- 兼容旧接口

    def remember(self, user_id: str, user_message: str, bot_reply: str = "") -> None:
        """兼容阶段一的同步写法：尽力而为地异步写入（不阻塞调用方）。"""
        fact = (user_message or "").strip()
        if not fact:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.store(user_id, fact))
            return
        loop.create_task(self.store(user_id, fact))

    def as_text(self, user_id: str) -> str:
        """兼容阶段一的同步摘要接口。"""
        return self.digest(user_id)
