"""事件幂等键（event_key）测试。

架构决策（用户拍板，勿回退）
----------------------------
- **15s 窗口保留**，但窗口只用于入口「幽灵消息」过滤，不再参与 key 生成。
- **event_key 由批次 ID 决定**：``md5(user_id:merged_text:batch_id)``。
  - 场景 A（网络重试）：同一条消息在防抖窗口内被重推 → 合并成一个 batch
    → 同一个 event_key → 只扣一次分。
  - 场景 B（用户连发）：跨过防抖窗口 → 两次 flush → 两个 batch_id
    → 两个 event_key → 各算各的，绝不吞消息。
- **去重落在数据库 `UserState.last_event_key`**，不依赖缓存淘汰。

核心断言：**引擎按「批次」去重，绝不按「文本内容」去重。**
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from engine import (
    Batch,
    EmotionEngine,
    IngressDeduper,
    make_event_key,
    new_batch_id,
)
from models import ensure_schema


def _run(coro):
    """在同步测试里跑协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------- key 生成


def test_同user同文本同批次生成相同key():
    a = make_event_key("u1", "嗯嗯", "1700000000000-1")
    b = make_event_key("u1", "嗯嗯", "1700000000000-1")
    assert a == b
    assert len(a) == 32
    assert all(ch in "0123456789abcdef" for ch in a)


def test_不同批次即使同文本也不同key():
    """场景 B 的核心：用户连发两次『嗯嗯』，必须算两次。"""
    a = make_event_key("u1", "嗯嗯", "1700000000000-1")
    b = make_event_key("u1", "嗯嗯", "1700000000000-2")
    assert a != b


def test_不同用户或不同文本key不同():
    base = make_event_key("u1", "嗯嗯", "b1")
    assert make_event_key("u2", "嗯嗯", "b1") != base
    assert make_event_key("u1", "哦哦", "b1") != base


def test_批次ID单调唯一():
    ids = [new_batch_id() for _ in range(50)]
    assert len(set(ids)) == 50


def test_Batch生成key与纯函数一致():
    batch = Batch(text="在吗", batch_id="b-42")
    assert batch.event_key("u1") == make_event_key("u1", "在吗", "b-42")


# ---------------------------------------------------------------- 引擎去重


def test_同一event_key重复调用不扣分(tmp_engine):
    first = _run(tmp_engine.update_emotion("u-dup", 80, event_key="evt-A"))
    second = _run(tmp_engine.update_emotion("u-dup", 80, event_key="evt-A"))

    assert first["applied"] is True
    assert second["applied"] is False
    assert second["emotion_current"] == first["emotion_current"]
    assert second["delta"] == 0.0


def test_不同event_key同文本照常累加(tmp_engine):
    """同一句话、不同批次 = 两次真实交互，必须各算一次。"""
    first = _run(tmp_engine.update_emotion("u-same", 80, event_key="evt-1"))
    second = _run(tmp_engine.update_emotion("u-same", 80, event_key="evt-2"))

    assert first["applied"] is True
    assert second["applied"] is True
    assert second["emotion_current"] > first["emotion_current"]


def test_last_event_key落库(tmp_engine):
    _run(tmp_engine.update_emotion("u-persist", 80, event_key="evt-xyz"))
    snapshot = tmp_engine._get_or_create("u-persist")
    assert snapshot["last_event_key"] == "evt-xyz"


def test_换新事件后last_event_key被覆盖(tmp_engine):
    _run(tmp_engine.update_emotion("u-over", 80, event_key="evt-1"))
    _run(tmp_engine.update_emotion("u-over", 80, event_key="evt-2"))
    assert tmp_engine._get_or_create("u-over")["last_event_key"] == "evt-2"


def test_不传event_key时不做幂等(tmp_engine):
    first = _run(tmp_engine.update_emotion("u-nokey", 80))
    second = _run(tmp_engine.update_emotion("u-nokey", 80))
    assert first["applied"] is True
    assert second["applied"] is True


# ---------------------------------------------------------------- 主链路


def test_防抖合并后共享同一批次与key(tmp_engine):
    """场景 A：同一条消息被重推 3 次，落在同一个防抖窗口。"""
    tmp_engine.debouncer.window = 0.05

    async def scenario():
        return await asyncio.gather(
            tmp_engine.handle_message("u-retry", "我在呢", debounce=True),
            tmp_engine.handle_message("u-retry", "我在呢", debounce=True),
            tmp_engine.handle_message("u-retry", "我在呢", debounce=True),
        )

    results = _run(scenario())
    keys = {item["debug"]["event_key"] for item in results}
    assert len(keys) == 1, "同一次防抖合并必须共享一个 event_key"
    assert sum(1 for item in results if item["debug"]["applied"]) == 1


def test_跨两次冲刷的同文本消息都要算(tmp_engine):
    """场景 B：用户发『嗯嗯』，Bot 回复，用户再发『嗯嗯』——不能被吞。"""
    tmp_engine.debouncer.window = 0.02

    first = _run(tmp_engine.handle_message("u-twice", "我在呢", debounce=True))
    second = _run(tmp_engine.handle_message("u-twice", "我在呢", debounce=True))

    assert first["debug"]["event_key"] != second["debug"]["event_key"]
    assert first["debug"]["applied"] is True
    assert second["debug"]["applied"] is True


def test_不防抖时同文本连发也算两次(tmp_engine):
    """引擎不按文本内容去重；幽灵消息由入口 IngressDeduper 负责。"""
    first = _run(tmp_engine.handle_message("u-nod", "我在呢"))
    second = _run(tmp_engine.handle_message("u-nod", "我在呢"))
    assert first["debug"]["event_key"] != second["debug"]["event_key"]
    assert first["debug"]["applied"] is True
    assert second["debug"]["applied"] is True


def test_显式event_key优先且能去重(tmp_engine):
    first = _run(tmp_engine.handle_message("u-manual", "随便说说", event_key="msg-42"))
    second = _run(tmp_engine.handle_message("u-manual", "随便说说", event_key="msg-42"))

    assert first["debug"]["event_key"] == "msg-42"
    assert second["debug"]["applied"] is False


def test_debug里带event_key(tmp_engine):
    payload = _run(tmp_engine.handle_message("u-debug", "在吗"))
    assert payload["debug"]["event_key"]
    assert len(payload["debug"]["event_key"]) == 32


# ---------------------------------------------------------------- 入口过滤


def test_按message_id过滤幽灵消息(tmp_engine):
    deduper = IngressDeduper(tmp_engine.cache, ttl=3.0)
    assert deduper.is_duplicate("u1", "嗯嗯", message_id="m-1") is False
    assert deduper.is_duplicate("u1", "嗯嗯", message_id="m-1") is True
    # 不同消息 ID 是不同消息，即使文本一样
    assert deduper.is_duplicate("u1", "嗯嗯", message_id="m-2") is False


def test_无message_id时按短窗口文本过滤(tmp_engine):
    deduper = IngressDeduper(tmp_engine.cache, ttl=3.0)
    assert deduper.is_duplicate("u1", "嗯嗯") is False
    assert deduper.is_duplicate("u1", "嗯嗯") is True
    assert deduper.is_duplicate("u1", "哦哦") is False
    assert deduper.is_duplicate("u2", "嗯嗯") is False


def test_入口过滤器过期后放行(tmp_engine):
    deduper = IngressDeduper(tmp_engine.cache, ttl=0.2)
    assert deduper.is_duplicate("u1", "嗯嗯") is False
    assert deduper.is_duplicate("u1", "嗯嗯") is True
    import time

    time.sleep(0.35)
    assert deduper.is_duplicate("u1", "嗯嗯") is False


# ---------------------------------------------------------------- 老库迁移


def test_老库自动补last_event_key列(tmp_path):
    """阶段二之前的库没有 last_event_key，ensure_schema 要能补上。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE user_states ("
        "user_id VARCHAR PRIMARY KEY, emotion_current INTEGER, "
        "emotion_level VARCHAR, affinity_raw INTEGER, last_update DATETIME)"
    )
    conn.execute(
        "INSERT INTO user_states VALUES ('legacy-user', 66, 'L0', 12, '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    engine = EmotionEngine(db_path=str(db_path), cache_dir=str(tmp_path / "cache_legacy"))
    try:
        assert any("last_event_key" in ddl for ddl in engine.migrations), engine.migrations
        state = _run(engine.update_emotion("legacy-user", 80, event_key="evt-legacy"))
        assert state["applied"] is True
        # 旧数据没被冲掉
        assert state["affinity_raw"] == 12
        assert engine._get_or_create("legacy-user")["last_event_key"] == "evt-legacy"
    finally:
        engine.cache.close()
        engine.db_engine.dispose()
