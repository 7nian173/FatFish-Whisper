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
    "BotMood",
    "TokenLog",
    "MessageCount",
    "utcnow",
    "ensure_schema",
    "DEFAULT_EMOTION",
    "DEFAULT_LEVEL",
    "DEFAULT_AFFINITY",
]

# ---------------------------------------------------------------- 常量

DEFAULT_EMOTION: Final[int] = 50      # 平静基准值（0-100）
DEFAULT_LEVEL: Final[str] = "L0"      # 默认等级
DEFAULT_AFFINITY: Final[int] = 0      # 好感度默认值，无上限


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
    """单个用户的情绪 / 好感度状态。

    Attributes:
        user_id: 宿主侧稳定用户 ID（主键）。
        emotion_current: 平滑后的当前情绪值，0-100。
        emotion_level: 稳定等级，L0 / L1 / L2 / CRISIS。
        affinity_raw: 好感度原始值，无上限（可以为负）。
        last_update: 最近一次写入时间（UTC naive），用于半衰期衰减。
        last_event_key: 最近一次生效的 event_key（幂等去重用）。
    """

    __tablename__ = "user_states"

    user_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    emotion_current: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_EMOTION, nullable=False
    )
    emotion_level: Mapped[str] = mapped_column(
        String, default=DEFAULT_LEVEL, nullable=False
    )
    affinity_raw: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_AFFINITY, nullable=False
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
            "emotion_current": int(self.emotion_current),
            "emotion_level": self.emotion_level,
            "affinity_raw": int(self.affinity_raw),
            "last_update": self.last_update,
            "last_event_key": self.last_event_key,
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            "<UserState user_id={0!r} emotion={1} level={2!r} affinity={3}>".format(
                self.user_id, self.emotion_current, self.emotion_level, self.affinity_raw
            )
        )


class BotMood(Base):
    """Bot **自己的**情绪（全局单行，不随用户变化）。

    与 :class:`UserState` 的区别
    ----------------------------
    - ``UserState`` 是「它对**某个人**的情绪」，按 ``user_id`` 隔离；
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
