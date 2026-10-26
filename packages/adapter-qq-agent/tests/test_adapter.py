"""Adapter 单元测试：透传、执行、撤回、降级。

跑法（仓库根目录）：
    venv/Scripts/python.exe -m pytest packages/adapter-qq-agent/tests -q
"""
from __future__ import annotations

import asyncio
import os
import random
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER = os.path.dirname(_HERE)
if _ADAPTER not in sys.path:
    sys.path.insert(0, _ADAPTER)

import httpx  # noqa: E402

from fatfish_adapter import (  # noqa: E402
    ActionExecutor,
    AdapterConfig,
    CoreClient,
    CoreReply,
    FatFishAdapter,
    InboundEvent,
    MockHostAPI,
    RecallRoller,
    Target,
)


def _run(coro):
    return asyncio.run(coro)


def _noop_sleep(record: list[float] | None = None):
    async def _sleep(seconds: float) -> None:
        if record is not None:
            record.append(seconds)

    return _sleep


def _reply(**kwargs) -> CoreReply:
    data = {"status": "ok", "actions": [], "inner_os": "", "debug": {}}
    data.update(kwargs)
    return CoreReply.from_raw(data)


# ---------------------------------------------------------------- 1. 请求结构


def test_请求体透传平台消息ID与群号():
    event = InboundEvent(
        platform_message_id="napcat-123",
        user_id="10001",
        message="在吗",
        group_id="88888",
        is_at_bot=True,
    )
    payload = event.to_payload()
    assert payload["platform_message_id"] == "napcat-123"
    assert payload["user_id"] == "10001"
    assert payload["group_id"] == "88888"
    assert payload["is_at_bot"] is True
    assert payload["message"] == "在吗"
    assert payload["debounce"] is False, "Adapter 已做防抖，不应让 Core 再合并"


def test_私聊不带group_id():
    event = InboundEvent(platform_message_id="m1", user_id="10001", message="hi")
    assert "group_id" not in event.to_payload()
    assert event.target.is_group is False


# ---------------------------------------------------------------- 2. 动作执行


def test_文本动作捕获message_id():
    host = MockHostAPI()
    executor = ActionExecutor(host, typing_delay=(0, 0), sleep=_noop_sleep())
    result = _run(executor.run(_reply(actions=[{"type": "text", "content": "嗯。"}]), Target("1")))
    assert result.sent_texts == ["嗯。"]
    assert result.sent_message_ids == ["mid-10001"]
    assert result.last_message_id == "mid-10001"


def test_多文本动作之间有拟人延迟():
    host = MockHostAPI()
    delays: list[float] = []
    executor = ActionExecutor(
        host, typing_delay=(0.5, 1.5), sleep=_noop_sleep(delays), rng=random.Random(0)
    )
    result = _run(
        executor.run(
            _reply(actions=[{"type": "text", "content": "一"}, {"type": "text", "content": "二"}]),
            Target("1"),
        )
    )
    assert len(result.sent_texts) == 2
    assert len(delays) == 2, "两条文本应各有一次延迟"
    assert all(0.5 <= value <= 1.5 for value in delays)


def test_单文本不加延迟():
    host = MockHostAPI()
    delays: list[float] = []
    executor = ActionExecutor(host, typing_delay=(0.5, 1.5), sleep=_noop_sleep(delays))
    _run(executor.run(_reply(actions=[{"type": "text", "content": "就一句"}]), Target("1")))
    assert delays == [], "单条文本不该让用户多等"


def test_拍一拍与表情包():
    host = MockHostAPI()
    executor = ActionExecutor(host, sleep=_noop_sleep())
    result = _run(
        executor.run(
            _reply(
                actions=[
                    {"type": "poke", "target": "10001"},
                    {"type": "sticker", "tag": "委屈"},
                ]
            ),
            Target("10001", "88888"),
        )
    )
    assert result.pokes == 1
    assert result.stickers == ["委屈"]
    assert result.sent_message_ids == ["mid-10001"]


def test_未知动作被忽略但不影响其它动作():
    host = MockHostAPI()
    executor = ActionExecutor(host, sleep=_noop_sleep())
    result = _run(
        executor.run(
            _reply(
                actions=[
                    {"type": "hug", "content": "?"},
                    {"type": "text", "content": "还好吗"},
                ]
            ),
            Target("1"),
        )
    )
    assert result.sent_texts == ["还好吗"]
    assert any("unknown_action" in err for err in result.errors)


def test_单个动作发送失败不中断后续动作():
    host = MockHostAPI(fail_on_send=True)
    executor = ActionExecutor(host, sleep=_noop_sleep())
    result = _run(
        executor.run(
            _reply(actions=[{"type": "text", "content": "a"}, {"type": "poke", "target": "1"}]),
            Target("1"),
        )
    )
    assert result.sent_texts == []
    assert result.pokes == 1, "第一条失败不应影响第二条"
    assert result.errors


def test_duplicate响应不执行任何动作():
    host = MockHostAPI()
    executor = ActionExecutor(host, sleep=_noop_sleep())
    reply = CoreReply.from_raw(
        {"status": "duplicate", "duplicate": True, "actions": [{"type": "text", "content": "不该发"}]}
    )
    result = _run(executor.run(reply, Target("1")))
    assert result.sent_texts == []
    assert host.sends == []


# ---------------------------------------------------------------- 3. 撤回


def test_命中概率则撤回并记录冷却():
    host = MockHostAPI()
    roller = RecallRoller(host, cooldown=180, rng=random.Random(0))
    target = Target("10001", "88888")

    decision = _run(roller.maybe_recall(target, "mid-1", 1.0, {"probability": 1.0}))
    assert decision.recalled is True
    assert host.recalls == [{"target": "g:88888", "message_id": "mid-1"}]

    # 冷却期内不再判定
    second = _run(roller.maybe_recall(target, "mid-2", 1.0, {"probability": 1.0}))
    assert second.recalled is False
    assert "cooldown" in second.reason
    assert len(host.recalls) == 1


def test_没掷中就不撤回也不进冷却():
    host = MockHostAPI()
    roller = RecallRoller(host, cooldown=180, rng=random.Random(0))
    target = Target("10001")

    decision = _run(roller.maybe_recall(target, "mid-1", 0.0 + 1e-9))
    assert decision.recalled is False
    assert roller.in_cooldown(target.key) is False, "没撤回就不该进冷却"


def test_没有message_id不撤回():
    host = MockHostAPI()
    roller = RecallRoller(host, cooldown=180)
    decision = _run(roller.maybe_recall(Target("1"), None, 1.0))
    assert decision.recalled is False
    assert decision.reason == "no_message_id"


def test_冷却按群或用户区分():
    host = MockHostAPI()
    roller = RecallRoller(host, cooldown=180, rng=random.Random(0))
    _run(roller.maybe_recall(Target("1", "g1"), "m1", 1.0))
    other = _run(roller.maybe_recall(Target("2", "g2"), "m2", 1.0))
    assert other.recalled is True, "别的群不受影响"


# ---------------------------------------------------------------- 4. 降级


def _client_with(handler, config: AdapterConfig | None = None) -> CoreClient:
    transport = httpx.MockTransport(handler)
    return CoreClient(config or AdapterConfig(), transport=transport)


def test_core返回5xx立即降级():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "engine down"})

    adapter = FatFishAdapter(host=MockHostAPI(), core=_client_with(handler))
    result = _run(adapter.on_event(InboundEvent("m1", "10001", "在吗")))
    assert result.reply.is_fallback is True
    assert result.degraded is True
    assert "503" in result.reply.debug["fallback_reason"]
    assert result.execution.sent_texts, "降级也要真的回一句，不能让用户干等"


def test_超时立即降级():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    adapter = FatFishAdapter(host=MockHostAPI(), core=_client_with(handler))
    result = _run(adapter.on_event(InboundEvent("m1", "10001", "在吗")))
    assert result.reply.is_fallback is True
    assert result.reply.debug["fallback_reason"] == "timeout"
    assert result.execution.sent_texts == ["（网卡了……等我一下）"]


def test_坏JSON降级():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    adapter = FatFishAdapter(host=MockHostAPI(), core=_client_with(handler))
    result = _run(adapter.on_event(InboundEvent("m1", "10001", "在吗")))
    assert result.reply.debug["fallback_reason"] == "bad_json"


def test_原生LLM兜底优先于预设话术():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def native_llm(event: InboundEvent) -> str:
        return "原生模型回复：" + event.message

    adapter = FatFishAdapter(
        host=MockHostAPI(), core=_client_with(handler), native_llm=native_llm
    )
    result = _run(adapter.on_event(InboundEvent("m1", "10001", "在吗")))
    assert result.execution.sent_texts == ["原生模型回复：在吗"]


def test_原生LLM也挂了则用预设话术():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def native_llm(event: InboundEvent) -> str:
        raise RuntimeError("llm down")

    adapter = FatFishAdapter(
        host=MockHostAPI(), core=_client_with(handler), native_llm=native_llm
    )
    result = _run(adapter.on_event(InboundEvent("m1", "10001", "在吗")))
    assert result.execution.sent_texts == ["（网卡了……等我一下）"]


# ---------------------------------------------------------------- 5. 端到端


def test_重复消息只执行一次():
    """Core 判重后，第二次不产生任何发送。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "actions": [{"type": "text", "content": "嗯哼"}],
                    "debug": {"applied": True},
                },
            )
        return httpx.Response(
            200, json={"status": "duplicate", "duplicate": True, "actions": []}
        )

    host = MockHostAPI()
    adapter = FatFishAdapter(host=host, core=_client_with(handler))
    event = InboundEvent("same-id", "10001", "嗯嗯")

    first = _run(adapter.on_event(event))
    second = _run(adapter.on_event(event))

    assert first.skipped_duplicate is False
    assert second.skipped_duplicate is True
    assert len(host.sends) == 1
