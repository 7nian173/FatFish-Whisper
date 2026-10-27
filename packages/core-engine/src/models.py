"""FatFish-Whisper 数据模型（SQLAlchemy 2.x declarative）。

设计说明
--------
1. 本模块是**模块文件** ``models.py``（不是包），全仓唯一的数据表定义入口。
   阶段一遗留的 ``src/models/`` 包已删除——包会遮蔽同名模块，必须清除。
2. 只依赖 sqlalchemy，**不依赖** engine / prompt_builder，保证依赖单向：
   ``engine -> models``、``engine -> prompt_builder``、``engine -> memory``。
3. 时间统一用 UTC 的 **naive** datetime（``utcnow()``），写入与读取全程一致，
   避免 SQLite 里混入带时区的 aware datetime 造成比较报错。
4. ``ensure_schema()`` 提供极轻量的 SQLite 列补齐迁移（只做 ADD COLUMN），
   用于兼容阶段一遗留的 ``app.db``（例如 token_logs 的旧列名 ``module_type``）。
5. ``migrate_chat_scope()`` 做两件 ``create_all`` / ``ADD COLUMN`` 都做不到的事：
   ``user_states`` 的主键改造（复合主键只能重建表）与 ``affinity_raw`` 拆表。

I-26 裁决（锚点全局、情绪分房、理由分房）
-----------------------------------------
- :class:`UserAffinity`（好感度）**全局**，只有 ``user_id`` 一个主键，没有 ``chat_scope``；
- :class:`BotMood`（它自己的心情）**全局**，单行；
- :class:`UserState`（对某个人的情绪）按 ``(user_id, chat_scope)`` **严格分房**。

``chat_scope`` 取值只有两种形状：``g:<group_id>``（群）与 ``p:<user_id>``（私聊），
由 ``main.py`` 在请求解析处**唯一一次**派生，下游模块禁止自拼。
"""
from __future__ import annotations

import datetime
from typing import Any, Final, Optional

from sqlalchemy import DateTime, Integer, String, inspect as sa_inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "UserState",
    "UserAffinity",
    "BotMood",
    "TokenLog",
    "MessageCount",
    "utcnow",
    "ensure_schema",
    "migrate_chat_scope",
    "DEFAULT_EMOTION",
    "DEFAULT_LEVEL",
    "DEFAULT_AFFINITY",
    "DEFAULT_CHAT_SCOPE",
    "SCOPE_GROUP_PREFIX",
    "SCOPE_PRIVATE_PREFIX",
]

# ---------------------------------------------------------------- 常量

DEFAULT_EMOTION: Final[int] = 50      # 平静基准值（0-100）
DEFAULT_LEVEL: Final[str] = "L0"      # 默认等级
DEFAULT_AFFINITY: Final[int] = 0      # 好感度默认值，无上限

#: 会话分区的**兼容期默认值**（空串）。
#:
#: 为什么是空串而不是 ``p:<user_id>``：老库里的行是在分房之前写下的，它们既可能来自
#: 群聊也可能来自私聊，**没有任何可靠依据可以推断**。把 ``""`` 静默当成
#: ``p:<user_id>`` 会让老数据被新逻辑误读（私聊分区凭空多出一份历史情绪，群聊分区
#: 又凭空丢掉它）。因此约定：**老数据保持 ``""``，新写入一律用真实 scope**，
#: ``""`` 只作为「分房之前的历史分区」存在，不参与任何跨房合并。
DEFAULT_CHAT_SCOPE: Final[str] = ""

#: ``chat_scope`` 的两种合法前缀（派生点唯一：``main.py`` 的请求解析处）。
SCOPE_GROUP_PREFIX: Final[str] = "g:"
SCOPE_PRIVATE_PREFIX: Final[str] = "p:"


def utcnow() -> datetime.datetime:
    """返回当前 UTC 时间（naive，无 tzinfo）。

    统一使用 ``datetime.now(timezone.utc)`` 后去掉 tzinfo，
    而不是已废弃的 ``datetime.utcnow()``（Python 3.12+ 会告警）。
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- 表定义

class Base(DeclarativeBase):
    """全局 declarative base。"""


class UserState(Base):
    """某个用户在**某个会话分区**里的情绪状态（I-26：情绪分房）。

    Attributes:
        user_id: 宿主侧稳定用户 ID（复合主键第一列）。
        chat_scope: 会话分区，``g:<group_id>`` 或 ``p:<user_id>``；
            兼容期老数据为 ``""``（见 :data:`DEFAULT_CHAT_SCOPE`）。
        emotion_current: 平滑后的当前情绪值，0-100。
        emotion_level: 稳定等级，L0 / L1 / L2 / CRISIS。
        last_update: 最近一次写入时间（UTC naive），用于半衰期衰减。
        last_event_key: 最近一次生效的 event_key（幂等去重用）。

    Note:
        **好感度不在这里**。``affinity_raw`` 曾经是本表的一列，改成复合主键后
        同一个用户会有多行，好感度就会被意外分房（违反 I-26），因此已拆到全局表
        :class:`UserAffinity`。
    """

    __tablename__ = "user_states"

    user_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    chat_scope: Mapped[str] = mapped_column(
        String, primary_key=True, default=DEFAULT_CHAT_SCOPE, nullable=False
    )
    emotion_current: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_EMOTION, nullable=False
    )
    emotion_level: Mapped[str] = mapped_column(
        String, default=DEFAULT_LEVEL, nullable=False
    )
    last_update: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    last_event_key: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default=None
    )

    def as_dict(self) -> dict[str, Any]:
        """转成纯字典（方便调试 / 日志，不参与业务逻辑）。"""
        return {
            "user_id": self.user_id,
            "chat_scope": self.chat_scope,
            "emotion_current": int(self.emotion_current),
            "emotion_level": self.emotion_level,
            "last_update": self.last_update,
            "last_event_key": self.last_event_key,
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            "<UserState user_id={0!r} scope={1!r} emotion={2} level={3!r}>".format(
                self.user_id, self.chat_scope, self.emotion_current, self.emotion_level
            )
        )


class UserAffinity(Base):
    """好感度（I-26 裁决：**锚点全局，不分房**）。

    为什么单独一张表：``user_states`` 改成 ``(user_id, chat_scope)`` 复合主键后，
    同一用户在每个群都会有一行；好感度若还留在那一行里，就会变成「A 群 20、B 群 80」，
    这是把锚点当成情绪分房，违反 I-26。因此好感度独立成表，只有 ``user_id`` 一个键。

    ``engine._read_affinity`` / ``_write_affinity`` 的签名里**没有** ``chat_scope``
    参数——在签名层面就杜绝好感度被分房。
    """

    __tablename__ = "user_affinity"

    user_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    affinity_raw: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_AFFINITY, nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "affinity_raw": int(self.affinity_raw),
            "updated_at": self.updated_at,
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<UserAffinity user_id={0!r} affinity={1}>".format(
            self.user_id, self.affinity_raw
        )


class BotMood(Base):
    """Bot **自己的**情绪（全局单行，不随用户变化，也不随会话分区变化）。

    与 :class:`UserState` 的区别
    ----------------------------
    - ``UserState`` 是「它对**某个人在某个群里**的情绪」，按 ``(user_id, chat_scope)`` 隔离；
    - ``BotMood`` 是「它**自己**的心情」，全局一份，被所有交互缓慢影响。

    典型表现：被 A 骂了会闷，转头跟 B 说话时语气还没缓过来。
    它变化得比 UserState 慢（阻尼系数更小、衰减更慢），因为「整体心情」是慢变量。
    """

    __tablename__ = "bot_mood"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    emotion_current: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_EMOTION, nullable=False
    )
    emotion_level: Mapped[str] = mapped_column(
        String, default=DEFAULT_LEVEL, nullable=False
    )
    last_update: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "emotion_current": int(self.emotion_current),
            "emotion_level": self.emotion_level,
            "last_update": self.last_update,
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<BotMood emotion={0} level={1!r}>".format(
            self.emotion_current, self.emotion_level
        )


class TokenLog(Base):
    """Track B 的 token 账本，方便估算花费。

    Attributes:
        id: 自增主键。
        user_id: 触发本次调用的用户。
        module: 模块标识（如 ``MOD_TRACK_B``）。
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
        created_at: 写入时间（UTC naive）。
    """

    __tablename__ = "token_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    module: Mapped[str] = mapped_column(String)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            "<TokenLog user_id={0!r} module={1!r} in={2} out={3}>".format(
                self.user_id, self.module, self.prompt_tokens, self.completion_tokens
            )
        )


class MessageCount(Base):
    """按天累计的消息条数（演化系统判断「今天这个用户活不活跃」）。

    保持**全局按用户**计数（不分房）：活跃度闸门问的是「这个人今天还来不来」，
    与他在哪个群说话无关。

    Attributes:
        user_id: 用户 ID。
        day: ``YYYY-MM-DD``（UTC）。
        count: 当日消息条数。
        updated_at: 最后一次累加时间。
    """

    __tablename__ = "message_counts"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<MessageCount {0!r} {1!r} = {2}>".format(
            self.user_id, self.day, self.count
        )


# ---------------------------------------------------------------- 轻量迁移

def ensure_schema(bind: Engine) -> list[str]:
    """补齐已存在表缺失的列（仅 SQLite 的 ADD COLUMN）。

    阶段一的 ``token_logs`` 用的是旧列名 ``module_type``，而本阶段规格要求
    ``module``。``create_all`` 不会改动已存在的表，因此这里做一次列补齐，
    让老库也能继续写入；旧列保留不删（无破坏性）。

    Args:
        bind: SQLAlchemy Engine。

    Returns:
        实际执行的 DDL 语句列表（供调用方打日志）。
    """
    applied: list[str] = []
    try:
        inspector = sa_inspect(bind)
        existing_tables = set(inspector.get_table_names())
    except Exception:  # 极端情况下无法反射，直接放弃迁移
        return applied

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        try:
            columns = {col["name"] for col in inspector.get_columns(table.name)}
        except Exception:
            continue
        for column in table.columns:
            if column.name in columns:
                continue
            # 只补可空列：SQLite 的 ADD COLUMN 无法安全加 NOT NULL 无默认列
            col_type = column.type.compile(dialect=bind.dialect)
            ddl = "ALTER TABLE {0} ADD COLUMN {1} {2}".format(
                table.name, column.name, col_type
            )
            try:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
                applied.append(ddl)
            except Exception:
                # 迁移失败不应阻止服务启动
                continue
    return applied


#: 重建后的 ``user_states`` 结构（SQLite 专用：改主键只能重建表）。
_USER_STATES_NEW_DDL: Final[str] = (
    "CREATE TABLE user_states__new ("
    " user_id VARCHAR NOT NULL,"
    " chat_scope VARCHAR NOT NULL DEFAULT '',"
    " emotion_current INTEGER NOT NULL DEFAULT 50,"
    " emotion_level VARCHAR NOT NULL DEFAULT 'L0',"
    " last_update DATETIME NOT NULL,"
    " last_event_key VARCHAR,"
    " PRIMARY KEY (user_id, chat_scope))"
)


def migrate_chat_scope(bind: Engine) -> list[str]:
    """``chat_scope`` 分房迁移：``user_states`` 复合主键 + 好感度拆表。

    为什么必须自己动手：``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列，
    更不会改主键；SQLite 也不支持 ``ALTER TABLE ... ADD PRIMARY KEY``。所以主键
    改造只能「建新表 → 搬数据 → 删旧表 → 改名」。

    幂等性：每一步都先 ``PRAGMA table_info`` 判断当前结构，已是目标结构就直接返回
    （连续跑两次，第二次返回空列表、不写任何字节）。

    Args:
        bind: SQLAlchemy Engine。

    Returns:
        实际执行的 DDL / DML 语句列表（供调用方打日志）。
    """
    applied: list[str] = []
    try:
        inspector = sa_inspect(bind)
        if "user_states" not in set(inspector.get_table_names()):
            return applied  # 新库：create_all 已经建出目标结构
        columns = {col["name"] for col in inspector.get_columns("user_states")}
    except Exception:  # 反射失败时宁可不动库
        return applied

    has_affinity = "affinity_raw" in columns
    has_scope = "chat_scope" in columns
    if not has_affinity and has_scope:
        # 已是目标结构：只补索引（老库可能没建过）
        try:
            with bind.begin() as conn:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_user_states_scope"
                        " ON user_states(chat_scope)"
                    )
                )
        except Exception:
            pass
        return applied

    # ---- ① 好感度先搬走：拆表之前它还在 user_states 里 ----
    if has_affinity:
        copy_affinity = (
            "INSERT OR IGNORE INTO user_affinity (user_id, affinity_raw, updated_at)"
            " SELECT user_id, COALESCE(affinity_raw, 0),"
            " COALESCE(last_update, CURRENT_TIMESTAMP) FROM user_states"
        )
        try:
            with bind.begin() as conn:
                conn.execute(text(copy_affinity))
            applied.append(copy_affinity)
        except Exception:
            # 搬不动就不能继续拆表，否则好感度数据会丢
            return applied

    # ---- ② 重建 user_states：加 chat_scope 复合主键、去掉 affinity_raw ----
    # 老库可能连 last_event_key / last_update 都没有（阶段一），按实际列拼 SELECT。
    select_scope = "COALESCE(chat_scope, '')" if has_scope else "''"
    select_event_key = "last_event_key" if "last_event_key" in columns else "NULL"
    select_last_update = (
        "COALESCE(last_update, CURRENT_TIMESTAMP)"
        if "last_update" in columns
        else "CURRENT_TIMESTAMP"
    )
    copy_rows = (
        "INSERT INTO user_states__new"
        " (user_id, chat_scope, emotion_current, emotion_level, last_update, last_event_key)"
        " SELECT user_id, {0}, COALESCE(emotion_current, 50),"
        " COALESCE(emotion_level, 'L0'), {1}, {2} FROM user_states".format(
            select_scope, select_last_update, select_event_key
        )
    )
    steps = [
        _USER_STATES_NEW_DDL,
        copy_rows,
        "DROP TABLE user_states",
        "ALTER TABLE user_states__new RENAME TO user_states",
        "CREATE INDEX IF NOT EXISTS ix_user_states_user_id ON user_states(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_user_states_scope ON user_states(chat_scope)",
    ]
    try:
        with bind.begin() as conn:
            for ddl in steps:
                conn.execute(text(ddl))
        applied.extend(steps)
    except Exception:
        # 重建失败：清掉半成品新表，旧表原样保留（服务继续用旧结构启动）
        try:
            with bind.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS user_states__new"))
        except Exception:
            pass
    return applied
