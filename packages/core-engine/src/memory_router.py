"""插拔式记忆路由：宿主原生 > 外部插件 > 本地 SQLite 兜底。

三级降级
--------
1. :class:`HostNativeProvider`  —— 宿主（NapCat / NoneBot 插件）自带记忆 API，直接借用；
2. :class:`ExternalPluginProvider` —— Mem0 之类的 HTTP 记忆服务；
3. :class:`LocalSQLiteProvider` —— **永远可用**的兜底：SQLite + LRU + 7 天 TTL。

记忆分房（I-26：理由分房）
--------------------------
记忆条目按 ``(user_id, chat_scope)`` **物理隔离**：A 群的聊天内容绝不能在 B 群
被检索注入。``chat_scope`` 取值 ``g:<group_id>`` / ``p:<user_id>``，由入口
（``main.py`` 的请求解析处）唯一一次派生后透传，本模块**只存不拼**。
兼容期老数据的 ``chat_scope`` 是空串 ``""``（分房之前的历史分区），
不会与任何真实分区混读。

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
import hashlib
import hmac
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


# ---------------------------------------------------------------- 外发脱敏（F-10）

#: 脱敏后标识符的截断长度。16 位十六进制 = 64 bit，碰撞概率可忽略，
#: 同时足够短，不改变外部服务对 key 长度的预期。
REDACT_HEX_LEN: int = 16

#: 盐钥来源（按优先级）：环境变量 > 本地密钥文件 > 进程内随机（仅告警一次）。
ENV_REDACT_SALT = "MEMORY_EXTERNAL_SALT"
REDACT_SALT_FILENAME = "memory_salt.key"

_salt_lock = threading.Lock()
_salt_cache: dict[str, bytes] = {}
_salt_warned = False


def _load_or_create_salt(directory: str = "") -> bytes:
    """取脱敏盐钥；缺失时在 ``directory`` 生成 32 字节随机钥并持久化。

    为什么必须持久化：脱敏后的 ``user_id`` 是外部服务的检索主键。若每次进程
    启动都换钥，历史记忆永远检索不回来（等于每次重启清空外部记忆）。因此盐钥
    必须跨重启稳定；同时它**不能**是硬编码常量，否则等于无盐哈希。

    优先级：``MEMORY_EXTERNAL_SALT`` 环境变量 > ``directory`` 下的密钥文件 >
    进程内随机钥（告警，重启即失效）。
    """
    global _salt_warned
    env_salt = (os.environ.get(ENV_REDACT_SALT) or "").strip()
    if env_salt:
        return env_salt.encode("utf-8")

    base = directory or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    base = os.path.abspath(base)
    with _salt_lock:
        cached = _salt_cache.get(base)
        if cached is not None:
            return cached
        path = os.path.join(base, REDACT_SALT_FILENAME)
        try:
            if os.path.exists(path):
                with open(path, "rb") as handle:
                    key = handle.read().strip()
                if key:
                    _salt_cache[base] = key
                    return key
            key = os.urandom(32).hex().encode("ascii")
            with open(path, "wb") as handle:
                handle.write(key)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            _salt_cache[base] = key
            return key
        except OSError:
            # 目录不可写（只读容器等）：退化为进程内随机钥，明确告警。
            if not _salt_warned:
                _salt_warned = True
                logger.warning(
                    "无法持久化脱敏盐钥（%s），本次使用进程内随机钥；"
                    "重启后外部记忆将无法按原键检索，请设置 %s",
                    path,
                    ENV_REDACT_SALT,
                )
            key = os.urandom(32).hex().encode("ascii")
            _salt_cache[base] = key
            return key


def redact_identifier(value: str, salt: Optional[bytes] = None) -> str:
    """把标识符（QQ 号 / 群号）做 HMAC-SHA256 截断，返回不可逆短串。

    用 HMAC 而不是裸 SHA-256：QQ 号段空间只有约 10⁹，裸哈希可被预计算表
    几小时内反查；带密钥的 HMAC 让预计算不可行。
    """
    key = salt if salt is not None else _load_or_create_salt()
    digest = hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:REDACT_HEX_LEN]


def redact_chat_scope(chat_scope: str, salt: Optional[bytes] = None) -> str:
    """脱敏 ``chat_scope``，**保留分区前缀**以免破坏分房语义。

    ``p:987654321`` → ``p:<hmac16>``；``g:123456789`` → ``g:<hmac16>``。
    前缀必须保留：它编码「私聊 / 群聊」这一分区类别，且外部服务靠它做隔离。
    只脱敏后半段的明文 id——那正是原 F-10 条目漏掉的第二处泄露。
    """
    scope = str(chat_scope or "")
    if not scope:
        return ""
    head, sep, tail = scope.partition(":")
    if not sep:
        return redact_identifier(scope, salt)
    return head + ":" + redact_identifier(tail, salt)


def redact_outbound(
    user_id: str, chat_scope: str = "", salt_dir: str = ""
) -> dict[str, str]:
    """一次性产出对外请求要用的脱敏标识符（同一把盐，保证可检索）。"""
    salt = _load_or_create_salt(salt_dir)
    return {
        "user_id": redact_identifier(user_id, salt),
        "chat_scope": redact_chat_scope(chat_scope, salt),
    }



# ---------------------------------------------------------------- 接口

class IMemoryProvider(abc.ABC):
    """记忆提供者接口。所有实现都必须**永不抛异常**（内部降级）。"""

    #: 人类可读的名字，出现在日志/看板里
    name: str = "base"

    @abc.abstractmethod
    async def retrieve(
        self, user_id: str, query: str, chat_scope: str = ""
    ) -> list[str]:
        """按 query 取**本会话分区**的相关记忆，按相关度/时间倒序。"""

    @abc.abstractmethod
    async def store(
        self, user_id: str, fact: str, salience: float = 0.0, chat_scope: str = ""
    ) -> bool:
        """往**本会话分区**写入一条记忆；成功返回 True。"""

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
    #: 会话分区（``g:<群号>`` / ``p:<QQ号>`` / 老库 ``""``）。
    #: 放在最后是因为 dataclass 要求带默认值的字段排在无默认值字段之后。
    chat_scope: str = ""

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
        # PRAGMA 是**按连接**生效的（journal_mode 按库文件写进头，其余按连接），
        # 所以裸 sqlite3 必须每开一条连接都设一次，不能只设一次全局。
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id TEXT NOT NULL,"
                " chat_scope TEXT NOT NULL DEFAULT '',"
                " fact TEXT NOT NULL,"
                " created_at TEXT NOT NULL,"
                " last_access TEXT NOT NULL,"
                " hits INTEGER NOT NULL DEFAULT 0,"
                " pinned INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id, created_at)"
            )
            # 老库迁移：补列（SQLite 没有 IF NOT EXISTS for column）。
            # CREATE TABLE IF NOT EXISTS **不会**给已存在的表加列，必须 ALTER TABLE；
            # 且必须先查 PRAGMA table_info 判断列是否已存在（幂等，重复启动不报错）。
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            if "pinned" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
                logger.info("记忆库已迁移：新增 pinned 列")
            if "chat_scope" not in columns:
                # 老数据填 ''（兼容期历史分区），**不猜**成 p:<user_id>：
                # 分房之前的行既可能来自群聊也可能来自私聊，猜错会让老数据被误读。
                conn.execute(
                    "ALTER TABLE memories ADD COLUMN chat_scope TEXT NOT NULL DEFAULT ''"
                )
                logger.info("记忆库已迁移：新增 chat_scope 列（老数据填空串）")
            # 分房索引：检索 / 淘汰 / TTL 全部走 (user_id, chat_scope) 前缀
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_user_scope"
                " ON memories(user_id, chat_scope, created_at)"
            )

    # ---------------------------------------------------------- 接口实现

    def health(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def store(
        self, user_id: str, fact: str, salience: float = 0.0, chat_scope: str = ""
    ) -> bool:
        fact = (fact or "").strip()
        if not fact or not user_id:
            return False
        return await asyncio.to_thread(
            self._store_sync, str(user_id), fact, float(salience or 0.0), str(chat_scope)
        )

    def _store_sync(
        self, user_id: str, fact: str, salience: float = 0.0, chat_scope: str = ""
    ) -> bool:
        now = self._clock().isoformat(sep=" ")
        pinned = 1 if salience >= PIN_SALIENCE_THRESHOLD else 0
        scope = str(chat_scope)
        try:
            with self._lock, self._connect() as conn:
                # 同一句话不重复存（幂等）；已 Pin 的条目保持 Pin。
                # 幂等判定**必须带 chat_scope**：同一句话在 A 群和 B 群各说一次是两条
                # 独立的记忆，合并会让 B 群读到 A 群的上下文。
                exists = conn.execute(
                    "SELECT id, pinned FROM memories"
                    " WHERE user_id=? AND chat_scope=? AND fact=? LIMIT 1",
                    (user_id, scope, fact),
                ).fetchone()
                if exists:
                    conn.execute(
                        "UPDATE memories SET last_access=?, pinned=MAX(pinned, ?) WHERE id=?",
                        (now, pinned, exists["id"]),
                    )
                    return True
                conn.execute(
                    "INSERT INTO memories(user_id, chat_scope, fact, created_at,"
                    " last_access, hits, pinned) VALUES (?,?,?,?,?,0,?)",
                    (user_id, scope, fact, now, now, pinned),
                )
                self._evict_lru(conn, user_id, scope)
            return True
        except Exception:
            logger.warning("本地记忆写入失败", exc_info=True)
            return False

    def _evict_lru(
        self, conn: sqlite3.Connection, user_id: str, chat_scope: str = ""
    ) -> None:
        # Pin 的条目（情绪显著时刻）不参与淘汰；上限按**分区**算
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM memories"
            " WHERE user_id=? AND chat_scope=? AND pinned=0",
            (user_id, str(chat_scope)),
        ).fetchone()["n"]
        overflow = int(total) - self.max_items
        if overflow <= 0:
            return
        conn.execute(
            "DELETE FROM memories WHERE id IN ("
            " SELECT id FROM memories WHERE user_id=? AND chat_scope=? AND pinned=0"
            " ORDER BY last_access ASC LIMIT ?)",
            (user_id, str(chat_scope), overflow),
        )

    async def retrieve(
        self, user_id: str, query: str, chat_scope: str = ""
    ) -> list[str]:
        return await asyncio.to_thread(
            self._retrieve_sync, str(user_id), query or "", str(chat_scope)
        )

    def _retrieve_sync(self, user_id: str, query: str, chat_scope: str = "") -> list[str]:
        scope = str(chat_scope)
        try:
            now = self._clock()
            cutoff = (now - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
            with self._lock, self._connect() as conn:
                # TTL：过期即清（顺手做，省得单独跑定时任务）；**Pin 的条目永不过期**
                conn.execute(
                    "DELETE FROM memories"
                    " WHERE user_id=? AND chat_scope=? AND created_at < ? AND pinned=0",
                    (user_id, scope, cutoff),
                )
                # 只读**本分区**：这就是跨群隐私泄漏的修复点
                rows = conn.execute(
                    "SELECT * FROM memories WHERE user_id=? AND chat_scope=?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (user_id, scope, max(self.max_items, MEMORY_MAX_INJECT)),
                ).fetchall()

            items = [
                MemoryItem(
                    id=row["id"],
                    user_id=row["user_id"],
                    chat_scope=row["chat_scope"] if "chat_scope" in row.keys() else "",
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

    def count(self, user_id: str, chat_scope: Optional[str] = None) -> int:
        """当前有效记忆条数（测试/看板用）。

        ``chat_scope=None`` 统计该用户**所有分区**（看板/运维视角）；
        传具体分区则只数那个分区。
        """
        cutoff = (self._clock() - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
        with self._connect() as conn:
            if chat_scope is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE user_id=? AND created_at >= ?",
                    (user_id, cutoff),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memories"
                    " WHERE user_id=? AND chat_scope=? AND created_at >= ?",
                    (user_id, str(chat_scope), cutoff),
                ).fetchone()
        return int(row["n"])

    def purge_expired(self) -> int:
        """清掉所有用户的过期记忆，返回删除条数。"""
        cutoff = (self._clock() - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def digest(self, user_id: str, limit: int = 20, chat_scope: Optional[str] = None) -> str:
        """给演化系统用的记忆摘要（纯文本，按时间正序）。

        ``chat_scope=None`` 时跨分区汇总（演化看的是「这个人整体」）；
        传具体分区则只取那个分区。演化产物是人设卡，不含分区敏感内容。
        """
        cutoff = (self._clock() - datetime.timedelta(days=self.ttl_days)).isoformat(sep=" ")
        with self._connect() as conn:
            if chat_scope is None:
                rows = conn.execute(
                    "SELECT fact FROM memories WHERE user_id=? AND created_at >= ?"
                    " ORDER BY created_at ASC LIMIT ?",
                    (user_id, cutoff, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT fact FROM memories WHERE user_id=? AND chat_scope=?"
                    " AND created_at >= ? ORDER BY created_at ASC LIMIT ?",
                    (user_id, str(chat_scope), cutoff, limit),
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

    async def retrieve(
        self, user_id: str, query: str, chat_scope: str = ""
    ) -> list[str]:
        if self.client is None:
            return []
        try:
            # 宿主若支持 chat_scope 就传（多数宿主 API 不支持，故按签名探测后降级）
            result = await self.client.retrieve(
                user_id=user_id, query=query, chat_scope=str(chat_scope)
            )
        except TypeError:
            try:
                result = await self.client.retrieve(user_id=user_id, query=query)
            except Exception:
                logger.warning("宿主记忆检索失败", exc_info=True)
                return []
        except Exception:
            logger.warning("宿主记忆检索失败", exc_info=True)
            return []
        return [str(item) for item in (result or [])]

    async def store(
        self, user_id: str, fact: str, salience: float = 0.0, chat_scope: str = ""
    ) -> bool:
        if self.client is None:
            return False
        try:
            await self.client.store(user_id=user_id, fact=fact, chat_scope=str(chat_scope))
            return True
        except TypeError:
            try:
                await self.client.store(user_id=user_id, fact=fact)
                return True
            except Exception:
                logger.warning("宿主记忆写入失败", exc_info=True)
                return False
        except Exception:
            logger.warning("宿主记忆写入失败", exc_info=True)
            return False


# ---------------------------------------------------------------- 外部插件

class ExternalPluginProvider(IMemoryProvider):
    """Mem0 等外部 HTTP 记忆服务（OpenAI 兼容风格的极简封装）。

    外发脱敏（F-10 / 决策 #9）
    --------------------------
    ``user_id`` 与 ``chat_scope`` **一律以 HMAC 脱敏形式外发**，明文 QQ 号 /
    群号不出本机。两处都必须改：私聊的 ``chat_scope`` 就是 ``p:<QQ号>``，
    只哈希 ``user_id`` 等于没脱敏。

    Args:
        base_url: 服务地址，如 ``http://127.0.0.1:8080``。
        api_key: 可选 Bearer。
        timeout: 请求超时（秒）。
        client: 可注入的 httpx.AsyncClient（测试用）。
        redact: 是否外发脱敏，默认 ``True``；仅排障时可关。
        salt_dir: 盐钥文件所在目录，默认与记忆库同目录（保证跨重启稳定）。
    """

    name = "external_plugin"

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 2.0,
        client: Optional[Any] = None,
        redact: bool = True,
        salt_dir: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        self._client = client
        self.redact = bool(redact)
        self.salt_dir = salt_dir or ""

    def health(self) -> bool:
        return bool(self.base_url)

    def _outbound_ids(self, user_id: str, chat_scope: str) -> dict[str, str]:
        """外发标识符：脱敏开关关闭时退回明文（仅排障用途）。"""
        if not self.redact:
            return {"user_id": str(user_id), "chat_scope": str(chat_scope)}
        return redact_outbound(user_id, chat_scope, self.salt_dir)


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

    async def retrieve(
        self, user_id: str, query: str, chat_scope: str = ""
    ) -> list[str]:
        if not self.health():
            return []
        ids = self._outbound_ids(user_id, chat_scope)
        try:
            client = await self._http()
            response = await client.post(
                self.base_url + "/search",
                json={
                    "user_id": ids["user_id"],
                    "query": query,
                    # 分区一并上报：外部服务若忽略它，最多退化到「跨区召回」，
                    # 不会比改造前更糟；本地兜底始终是分房的。
                    "chat_scope": ids["chat_scope"],
                    "limit": MEMORY_MAX_INJECT,
                },
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

    async def store(
        self, user_id: str, fact: str, salience: float = 0.0, chat_scope: str = ""
    ) -> bool:
        if not self.health():
            return False
        ids = self._outbound_ids(user_id, chat_scope)
        try:
            client = await self._http()
            response = await client.post(
                self.base_url + "/add",
                json={
                    "user_id": ids["user_id"],
                    "text": fact,
                    "chat_scope": ids["chat_scope"],
                },
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
            # 盐钥与记忆库同目录：跟着部署走，且跨重启稳定（否则外部记忆检索不到）
            salt_dir = os.path.dirname(os.path.abspath(db_path)) if db_path else ""
            chain.append(
                ExternalPluginProvider(
                    external_base_url, external_api_key, salt_dir=salt_dir
                )
            )
        return cls(chain, LocalSQLiteProvider(db_path, ttl_days=ttl_days))

    # ---------------------------------------------------------- 读写

    async def retrieve(
        self, user_id: str, query: str = "", chat_scope: str = ""
    ) -> list[str]:
        """按优先级取**本会话分区**的记忆；**第一个返回非空结果的提供者胜出**。"""
        for provider in self.providers:
            if not provider.health():
                continue
            facts = await provider.retrieve(user_id, query, chat_scope=chat_scope)
            if facts:
                logger.debug("记忆命中：%s（%d 条）", provider.name, len(facts))
                return facts[:MEMORY_MAX_INJECT]
        return []

    async def store(
        self, user_id: str, fact: str, salience: float = 0.0, chat_scope: str = ""
    ) -> bool:
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
                if await provider.store(
                    user_id, cleaned, salience, chat_scope=chat_scope
                ):
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

    async def prompt_block(
        self, user_id: str, query: str = "", chat_scope: str = ""
    ) -> str:
        return self.as_prompt_block(
            await self.retrieve(user_id, query, chat_scope=chat_scope)
        )

    def digest(
        self, user_id: str, limit: int = 20, chat_scope: Optional[str] = None
    ) -> str:
        """演化系统用的记忆摘要（只取本地兜底，外部服务的摘要由它自己出）。"""
        if self.local is None:
            return ""
        return self.local.digest(user_id, limit=limit, chat_scope=chat_scope)

    def health(self) -> dict[str, bool]:
        return {provider.name: provider.health() for provider in self.providers}

    # ---------------------------------------------------------- 兼容旧接口

    def remember(
        self, user_id: str, user_message: str, bot_reply: str = "", chat_scope: str = ""
    ) -> None:
        """兼容阶段一的同步写法：尽力而为地异步写入（不阻塞调用方）。"""
        fact = (user_message or "").strip()
        if not fact:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.store(user_id, fact, chat_scope=chat_scope))
            return
        loop.create_task(self.store(user_id, fact, chat_scope=chat_scope))

    def as_text(self, user_id: str) -> str:
        """兼容阶段一的同步摘要接口。"""
        return self.digest(user_id)
