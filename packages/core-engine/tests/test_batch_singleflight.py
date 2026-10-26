"""批次单飞：防抖合并批内只打一次 LLM（Track B）。

背景（QA 红黑榜 2026-10-25，scenario-1b-trace.log）：合并批内多个等待者并发走
``process()``，后到者的关系快照读到前者刚落库的新情绪（50/L0 → 45/L1）→ Track B
缓存键不同 → 缓存 MISS → 一次合并打两次 LLM（token 双倍）。三轮实测调用次数
``[2,2]`` / ``[2,2,2,2,2]`` / ``[1,1,1,2,2]``，竞态非确定。

修复：``EmotionEngine._track_b_once`` 按 event_key 做 in-flight 单飞。
架构约束（test_event_key.py 头部「用户拍板，勿回退」）：引擎按批次去重、
去重落库不依赖缓存、3 个等待者必须恰好 1 个 ``applied=True``——本修复不动这些。
"""
from __future__ import annotations

import asyncio

from engine import EmotionEngine, TrackB


def _llm_engine(engine_factory) -> EmotionEngine:
    """带 LLM 配置的引擎（Track B enabled），请求用替身，不触外网。"""
    return engine_factory(
        {
            "LLM_API_BASE": "https://api.deepseek.com/v1",
            "LLM_API_KEY": "sk-mock-key",
            "LLM_MODEL": "deepseek-chat",
        }
    )


def _patch_track_b(calls: dict, delay: float = 0.0, target: int = 22):
    """把 TrackB._request 换成计数替身，返回（原函数，恢复函数）。"""
    original = TrackB._request

    async def fake_request(self, body, headers):
        calls["n"] += 1
        if delay:
            await asyncio.sleep(delay)  # 模拟真实网络 RTT
        return {
            "choices": [{"message": {"content": '{"emotion_target": %d}' % target}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    TrackB._request = fake_request

    def restore():
        TrackB._request = original

    return restore


def test_防抖合并只打一次LLM(engine_factory):
    """两条不同文本并发进同一批次：Track B 只能被调一次，分数共享。

    mock 延迟取 150ms（真实 LLM RTT 量级）：合并批内多个等待者到达 in-flight
    查询点的时刻会被各自的 DB 活拉开几十毫秒（实测 11~46ms，随负载变化），
    延迟必须大于这个散布；零延迟/20ms mock 会偶发脱窗，那是测试 artifact 而非
    生产问题（生产 RTT 远大于散布）。
    """
    engine = _llm_engine(engine_factory)
    engine.debouncer.window = 0.2
    calls = {"n": 0}
    restore = _patch_track_b(calls, delay=0.15)
    try:
        async def scenario():
            return await asyncio.gather(
                engine.handle_message("u-sf", "qwxz", debounce=True),
                engine.handle_message("u-sf", "plkm", debounce=True),
            )

        results = asyncio.run(scenario())
    finally:
        restore()

    assert calls["n"] == 1, "同一合并批次只应打一次 LLM"
    keys = {item["debug"]["event_key"] for item in results}
    assert len(keys) == 1, "同一次防抖合并必须共享一个 event_key"
    assert sum(1 for item in results if item["debug"]["applied"]) == 1
    assert all(item["debug"]["track"] == "B" for item in results)
    assert all(item["debug"]["target"] == 22 for item in results)


def test_慢速LLM下也只打一次(engine_factory):
    """真实 RTT 下跟随者的缓存 GET 必然早于领头者的 SET——仍应只打一次。"""
    engine = _llm_engine(engine_factory)
    engine.debouncer.window = 0.2
    calls = {"n": 0}
    restore = _patch_track_b(calls, delay=0.15, target=30)
    try:
        async def scenario():
            return await asyncio.gather(
                engine.handle_message("u-sf2", "zzxx", debounce=True),
                engine.handle_message("u-sf2", "ccvv", debounce=True),
            )

        results = asyncio.run(scenario())
    finally:
        restore()

    assert calls["n"] == 1
    assert [item["debug"]["target"] for item in results] == [30, 30]


def test_三个等待者共享一次判定(engine_factory):
    """复刻 test_event_key 的三连推场景：恰好 1 个 applied、1 次 LLM。"""
    engine = _llm_engine(engine_factory)
    engine.debouncer.window = 0.2
    calls = {"n": 0}
    restore = _patch_track_b(calls, delay=0.15)
    try:
        async def scenario():
            return await asyncio.gather(
                engine.handle_message("u-sf3", "aaa", debounce=True),
                engine.handle_message("u-sf3", "bbb", debounce=True),
                engine.handle_message("u-sf3", "ccc", debounce=True),
            )

        results = asyncio.run(scenario())
    finally:
        restore()

    assert calls["n"] == 1
    assert sum(1 for item in results if item["debug"]["applied"]) == 1
    assert engine._track_b_inflight == {}, "in-flight 表必须被清空"


def test_无event_key时退回直接调用(engine_factory):
    """直接调 process() 不带 event_key：不走单飞，行为与旧版一致。"""
    engine = _llm_engine(engine_factory)
    calls = {"n": 0}
    restore = _patch_track_b(calls, target=40)
    try:
        payload = asyncio.run(engine.process("u-nokey", "今天星期三"))
    finally:
        restore()

    assert calls["n"] == 1
    assert payload["debug"]["track"] == "B"
    assert payload["debug"]["target"] == 40
    assert engine._track_b_inflight == {}


def test_串行调用不互相搭车(engine_factory):
    """两次独立冲刷（不同 event_key）各判各的，in-flight 不跨调用共享。"""
    engine = _llm_engine(engine_factory)
    engine.debouncer.window = 0.02
    calls = {"n": 0}
    restore = _patch_track_b(calls)
    try:
        first = asyncio.run(engine.handle_message("u-seq", "我在呢", debounce=True))
        second = asyncio.run(engine.handle_message("u-seq", "我在呢", debounce=True))
    finally:
        restore()

    assert first["debug"]["event_key"] != second["debug"]["event_key"]
    assert first["debug"]["applied"] is True
    assert second["debug"]["applied"] is True
    assert engine._track_b_inflight == {}
