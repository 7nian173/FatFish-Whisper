"""验收 4 · diskcache 锁与并发去重。

规格要点（与实现一致，勿按"教科书锁"误判）：
- diskcache 5.6.3 **没有** ``Cache.lock``，实现用 ``Cache.add(key, None, expire=..)``
  原子占位 + 自旋 + 超时。**不要 mock Cache.lock**，只测行为。
- ``event_key`` 5 秒窗口内只生效一次：10 并发同一事件只加一次 delta（50 -> 55）。
- 无 ``event_key`` 时靠锁串行，不能出现"10 次都基于 50 写回 55"的丢失更新。
- 拿不到锁（超时）必须降级返回当前状态，``applied is False``，**不抛异常、不 hang**。
"""
from __future__ import annotations

import asyncio
import time

import pytest

import _helpers as H

#: 并发用例把锁超时调大，避免慢机器上偶发超时导致误报（不影响被测逻辑）
CONCURRENCY_ENV = {"STATE_LOCK_TIMEOUT": "20"}


def test_同event_key并发只生效一次(engine_factory):
    """10 个并发同一 event_key：只有 1 个 applied，最终 55，且只建 1 行。"""
    engine = engine_factory(CONCURRENCY_ENV)
    H.seed_state(engine, "u-dedup", emotion=50)

    async def run_concurrently():
        return await asyncio.gather(
            *[engine.update_emotion("u-dedup", 80, event_key="evt-1") for _ in range(10)]
        )

    results = asyncio.run(run_concurrently())

    applied = sum(1 for item in results if item["applied"])
    assert applied == 1, "同一 event_key 并发 10 次只应生效 1 次，实际 {0}".format(applied)

    db = H.get_state(engine, "u-dedup")
    assert db["emotion"] == 55, "只应加一次 delta 上限（50+5），实际 {0}".format(
        db["emotion"]
    )
    assert H.count_users(engine, "u-dedup") == 1, "并发不应建出多行"
    # 所有返回快照都应是最终一致值 55
    assert all(item["emotion_current"] == 55 for item in results), [
        item["emotion_current"] for item in results
    ]


def test_不同event_key都会生效(engine_factory):
    """不同 event_key 不应互相去重（避免幂等实现把窗口做太宽）。"""
    engine = engine_factory(CONCURRENCY_ENV)
    H.seed_state(engine, "u-dedup-2", emotion=50)

    first = asyncio.run(engine.update_emotion("u-dedup-2", 80, event_key="evt-a"))
    second = asyncio.run(engine.update_emotion("u-dedup-2", 80, event_key="evt-b"))

    assert first["applied"] is True
    assert second["applied"] is True
    assert first["emotion_current"] == 55
    assert second["emotion_current"] == 60


def test_无event_key并发不丢失更新(engine_factory):
    """10 个并发无 event_key：必须等价于串行 10 次，不能丢更新。"""
    engine = engine_factory(CONCURRENCY_ENV)
    H.seed_state(engine, "u-serial", emotion=50)

    async def run_concurrently():
        return await asyncio.gather(
            *[engine.update_emotion("u-serial", 80) for _ in range(10)]
        )

    results = asyncio.run(run_concurrently())

    expected = H.simulate_serial(start=50, target=80, steps=10, habituation=True)
    db = H.get_state(engine, "u-serial")

    assert sum(1 for item in results if item["applied"]) == 10, "无 event_key 每次都应生效"
    assert db["emotion"] == expected, "期望串行 {0}，实际 {1}（出现丢失更新）".format(
        expected, db["emotion"]
    )
    assert db["emotion"] > 55, "若为 55 说明 10 次都基于旧值写回，属丢失更新"
    assert 0 <= db["emotion"] <= 100
    assert H.count_users(engine, "u-serial") == 1


def test_锁被占用时降级返回当前状态(engine_factory):
    """手工占住锁 -> 超时降级：返回当前状态、applied False、不抛错、不 hang。"""
    engine = engine_factory({"STATE_LOCK_TIMEOUT": "0.05"})
    H.seed_state(engine, "u-lock", emotion=73, affinity=9)

    # 用与实现完全相同的原子占位方式抢锁
    acquired = engine.cache.add("lock:state:u-lock", None, expire=10)
    assert acquired is True, "测试自身抢锁失败"

    started = time.monotonic()
    result = asyncio.run(engine.update_emotion("u-lock", 90))
    elapsed = time.monotonic() - started

    assert result["applied"] is False, "拿不到锁时不应标记为已应用"
    assert result["emotion_current"] == 73, "应原样返回当前状态"
    assert result["emotion_level"] == "L0"
    assert result["delta"] == pytest.approx(0.0, abs=0.01)
    assert elapsed < 3.0, "超时降级必须快速返回，实际耗时 {0:.2f}s".format(elapsed)
    # 状态未被改动
    assert H.get_state(engine, "u-lock")["emotion"] == 73
    assert H.get_state(engine, "u-lock")["affinity"] == 9


def test_锁释放后可以继续更新(engine_factory):
    """正常路径：前一次释放锁后，后一次必须能正常拿到锁并生效。"""
    engine = engine_factory({"STATE_LOCK_TIMEOUT": "0.05"})
    H.seed_state(engine, "u-release", emotion=50)

    first = asyncio.run(engine.update_emotion("u-release", 80))
    second = asyncio.run(engine.update_emotion("u-release", 80))

    assert first["applied"] is True and first["emotion_current"] == 55
    assert second["applied"] is True and second["emotion_current"] == 60
    # 锁 key 必须已被释放
    assert engine.cache.get("lock:state:u-release") is None
