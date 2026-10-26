"""验收 2 · 半衰期衰减。

规格（v2）：每过 30 分钟向 50 靠近 10%，即 ``50 + (E-50) * 0.9 ** (秒/1800)``；
负面情绪（E < 50）衰减速度减半（``0.95 ** (秒/1800)``）。
为不让"平滑"掩盖衰减，用例里 target 与旧值同向（90 -> 90）。
"""
from __future__ import annotations

import asyncio

import pytest

import _helpers as H
from engine import level_of


def test_两小时衰减基准约76(engine_factory):
    """90 分放置 2 小时 -> 基准 50 + 40*0.9^4 ≈ 76.24。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-decay-2h", emotion=90, affinity=7, hours_ago=2.0)

    result = asyncio.run(engine.update_emotion("u-decay-2h", 90))

    assert result["decayed"] is True
    assert result["emotion_stored_prev"] == 90, "原始库值应原样回传"
    assert result["emotion_prev"] == pytest.approx(76.24, abs=1.0), (
        "衰减后基准应≈76.24，实际 {0}".format(result["emotion_prev"])
    )
    # 76.24 + 0.2*(90-76.24) ≈ 79
    assert result["emotion_current"] == pytest.approx(79, abs=1)
    assert result["emotion_current"] < 90, "衰减后必须明显低于旧值 90"
    assert result["delta"] == pytest.approx(2.75, abs=0.3)

    db = H.get_state(engine, "u-decay-2h")
    assert db["emotion"] == result["emotion_current"]
    assert db["level"] == result["emotion_level"]
    assert db["affinity"] == 7, "衰减/更新不得覆盖好感度"


def test_三十分钟衰减百分之十(engine_factory):
    """30 分钟：向 50 靠近 10%，90 -> 86。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-decay-30m", emotion=90, hours_ago=0.5)

    result = asyncio.run(engine.update_emotion("u-decay-30m", 90))

    assert result["decayed"] is True
    assert result["emotion_prev"] == pytest.approx(86.0, abs=0.2)
    # 86 + 0.2*(90-86) = 86.8 -> 87
    assert result["emotion_current"] == 87
    assert result["delta"] == pytest.approx(0.8, abs=0.1)


def test_负面情绪衰减更慢(engine_factory):
    """负面偏差：同样 30 分钟，E<50 只靠近 5%（0.95 而非 0.9）。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-decay-neg", emotion=10, hours_ago=0.5)

    result = asyncio.run(engine.update_emotion("u-decay-neg", 10))

    # 50 + (10-50)*0.95 = 12.0
    assert result["emotion_prev"] == pytest.approx(12.0, abs=0.3)


def test_一分钟内不衰减(engine_factory):
    """短于最小间隔不衰减，避免连续消息之间抖动。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-decay-fast", emotion=90, hours_ago=1 / 120.0)

    result = asyncio.run(engine.update_emotion("u-decay-fast", 90))

    assert result["decayed"] is False
    assert result["emotion_prev"] == pytest.approx(90.0, abs=0.01)


def test_二十四小时基准回到50(engine_factory):
    """24 小时：0.9^48 几乎归零，基准≈50。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-decay-24h", emotion=90, hours_ago=24.0)

    result = asyncio.run(engine.update_emotion("u-decay-24h", 90))

    assert result["decayed"] is True
    assert result["emotion_prev"] == pytest.approx(50.0, abs=0.5), (
        "24h 后基准应≈50，实际 {0}".format(result["emotion_prev"])
    )
    # 50 + clamp(0.2*(90-50)=8 -> 5) = 55
    assert result["emotion_current"] == pytest.approx(55, abs=1)
    assert result["emotion_level"] == "L0"


def test_衰减后等级与分数映射一致(engine_factory):
    """低分放置 24h 后衰减回 46 附近，再被 target=0 拉到 41 -> L1，映射必须一致。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-decay-low", emotion=0, hours_ago=24.0)

    result = asyncio.run(engine.update_emotion("u-decay-low", 0))

    assert result["decayed"] is True
    # 负面偏差：50 + (0-50)*0.95^48 ≈ 45.74
    assert result["emotion_prev"] == pytest.approx(45.74, abs=1.0)
    # 45.74 + clamp(0.2*(0-45.74)=-9.15 -> -5) = 40.74 -> 41
    assert result["emotion_current"] == 41
    assert result["emotion_level"] == "L1"
    assert result["emotion_level"] == level_of(result["emotion_current"]), (
        "返回的等级必须与 level_of(emotion_current) 一致"
    )
    assert H.get_state(engine, "u-decay-low")["level"] == result["emotion_level"]


def test_首次更新不衰减(engine_factory):
    """用户不存在时创建默认行（50），last_update=now，不应误判为衰减。"""
    engine = engine_factory({})

    result = asyncio.run(engine.update_emotion("u-decay-new", 80))

    assert result["decayed"] is False
    assert result["emotion_stored_prev"] == 50
    assert result["emotion_current"] == 55
