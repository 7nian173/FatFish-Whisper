"""SQLAlchemy 数据表定义（SQLite 落地，无 Redis）。"""
from __future__ import annotations

import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class UserState(Base):
    """单个用户的情绪 / 好感度状态。"""

    __tablename__ = "user_states"

    user_id = Column(String, primary_key=True, index=True)
    affinity_raw = Column(Integer, default=50)      # 好感度，可突破 100
    emotion_current = Column(Integer, default=50)   # 当前情绪值 0-100
    emotion_level = Column(String, default="L0")    # L0 / L1 / L2 / CRISIS
    last_update = Column(DateTime, default=datetime.datetime.utcnow)


class TokenLog(Base):
    """Track B 的 token 账本，方便看花了多少钱。"""

    __tablename__ = "token_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    module_type = Column(String)          # MOD_TRACK_B / MOD_EVOLVE
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
