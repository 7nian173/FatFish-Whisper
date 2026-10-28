"""P0-9 · Adapter 侧补偿（幽灵状态）验收。

要防的是什么
------------
``CoreClient`` 的铁律是「超时/5xx 立刻降级，绝不让用户干等」。这条铁律救的是
**用户等待**，但 Core 可能只是慢、并没有失败——那条消息的情绪账就这么凭空少
了一笔，而用户与 Bot 的对话已经推进了。两边账面从此错位，且再无修复时机。

所以降级之后要**异步补一笔账**：同一条原始消息、同一个 event_key 重放给 Core。
三条不可退让的性质：

1. **不拖慢回复**：补账必须在回复交付之后跑，绝不能 ``await`` 在关键路径上。
2. **只补该补的**：``bad_json`` / 4xx 说明 Core 已处理或请求本身不合法，补了是
   错账；只有超时、网络异常、5xx 才补。
3. **同一笔账不补两遍**：``original_event_key`` 必须就是原请求用过的幂等键，
   否则 Core 会把补偿当成一条全新消息，同一个情绪扣两遍——比不补更糟。
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER = os.path.dirname(_HERE)
if _ADAPTER not in sys.path:
    sys.path.insert(0, _ADAPTER)

from fatfish_adapter import AdapterConfig, CoreClient, FatFishAdapter, InboundEvent, MockHostAPI  # noqa: E402
from fatfish_adapter import core_client as cc  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_backlog():
    """每条用例前后清空全局待补队列，避免用例之间互相污染。"""
    cc.compensate_backlog.clear()
    yield
    cc.compensate_backlog.clear()


class Recorder:
    """记录打进来的请求，并按路径返回预设响应。"""

    def __init__(self, chat_response=None, compensate_response=None):
        self.requests: list[httpx.Request] = []
        self._chat = chat_response
        self._compensate = compensate_response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/compensate"):
            return self._respond(self._compensate, default_status=200, default_json={})
        return self._respond(self._chat, default_status=200, default_json={})

    @staticmethod
    def _respond(spec, *, default_status: int, default_json: dict) -> httpx.Response:
        if spec is None:
            return httpx.Response(default_status, json=default_json)
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, int):
            return httpx.Response(spec)
        return spec

    def compensate_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/compensate")]


def _adapter(recorder: Recorder) -> FatFishAdapter:
    client = CoreClient(AdapterConfig(), transport=httpx.MockTransport(recorder))
    return FatFishAdapter(host=MockHostAPI(), core=client)


# ---------------------------------------------------------------- 1. 该补的补


def test_超时降级后补一笔账():
    """超时 = Core 可能只是慢；账要补，且必须带原消息与原幂等键。"""
    recorder = Recorder(chat_response=httpx.ConnectTimeout("too slow"))
    adapter = _adapter(recorder)
    event = InboundEvent("mid-77", "10001", "我今天很难过", group_id="555")

    result = _run(adapter.on_event(event))

    # 用户先拿到兜底话术（这是铁律，不能被补账破坏）
    assert result.reply.is_fallback is True
    assert result.execution.sent_texts == ["（网卡了……等我一下）"]

    compensations = recorder.compensate_requests()
    assert len(compensations) == 1, "超时后必须补账一次"
    body = compensations[0].read().decode()
    assert "mid-77" in body, "original_event_key 必须是原请求用过的幂等键"
    assert "我今天很难过" in body
    assert "555" in body


def test_5xx降级后也补账():
    """5xx 是「Core 侧出错」，不是「请求不合法」——账同样漏了。"""
    recorder = Recorder(chat_response=503)
    result = _run(_adapter(recorder).on_event(InboundEvent("mid-5xx", "10001", "在吗")))

    assert result.degraded is True
    assert len(recorder.compensate_requests()) == 1


def test_补偿不改写原始消息():
    """补偿必须重放**原文**。改写过的文本判定结果会不同，等于补了一笔假账。"""
    recorder = Recorder(chat_response=httpx.ConnectTimeout("t"))
    _run(_adapter(recorder).on_event(InboundEvent("m1", "10001", "原话：我好累啊")))

    body = recorder.compensate_requests()[0].read().decode()
    assert "原话：我好累啊" in body


def test_补偿带trigger_type():
    """口径必须跟着走：拿 user_msg 去补一条 proactive 的账，扣分规则完全不同。"""
    recorder = Recorder(chat_response=httpx.ConnectTimeout("t"))
    event = InboundEvent("m1", "10001", "嗯", trigger_type="proactive")
    _run(_adapter(recorder).on_event(event))

    # httpx 序列化不带分隔空格，两种写法都容忍，只认键值对本身
    body = recorder.compensate_requests()[0].read().decode().replace(": ", ":")
    assert '"trigger_type":"proactive"' in body


# ---------------------------------------------------------------- 2. 不该补的不补


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"detail": "bad request"}),
        httpx.Response(422, json={"detail": "validation"}),
        httpx.Response(200, text="<html>not json</html>"),
    ],
    ids=["400-请求不合法", "422-请求不合法", "bc-bad_json"],
)
def test_不该补的场景一律不补(response):
    """400/422 说明请求本身有问题，补一次还是错的；坏 JSON 说明 Core 已处理。

    ``bad_json`` 特别容易误判成「Core 挂了要补」，其实它返回了 200——账已经记过了。
    """
    recorder = Recorder(chat_response=response)
    _run(_adapter(recorder).on_event(InboundEvent("mid-x", "10001", "在吗")))

    assert recorder.compensate_requests() == []
    assert not cc.compensate_backlog


# ---------------------------------------------------------------- 3. 补账失败不拖累


def test_补偿自己失败只进队列不抛异常():
    """补账是尽力而为：它自己挂了只记队列，绝不能让调用方的回合炸掉。"""
    recorder = Recorder(
        chat_response=httpx.ConnectTimeout("t"),
        compensate_response=httpx.ConnectTimeout("compensate down"),
    )
    adapter = _adapter(recorder)

    result = _run(adapter.on_event(InboundEvent("mid-f", "10001", "我很累")))

    assert result.reply.is_fallback is True, "补偿失败不能影响用户拿到回复"
    assert len(cc.compensate_backlog) == 1, "失败的补账要进待补队列供排查"
    entry = cc.compensate_backlog[0]
    assert entry["payload"]["original_event_key"] == "mid-f"
    assert entry["reason"].startswith("timeout")


def test_补偿队列上限有界():
    """待补队列必须有界——补账是尽力而为，不能反过来把 Adapter 内存吃光。"""
    assert cc.compensate_backlog.maxlen == cc.COMPENSATE_QUEUE_MAX
    for index in range(cc.COMPENSATE_QUEUE_MAX + 20):
        cc.compensate_backlog.append({"payload": {}, "reason": "timeout"})
    assert len(cc.compensate_backlog) == cc.COMPENSATE_QUEUE_MAX


def test_没有事件循环时不炸():
    """同步环境（无 running loop）里补账无处可排：只记队列，不能抛 RuntimeError。"""
    recorder = Recorder(chat_response=httpx.ConnectTimeout("t"))
    client = CoreClient(AdapterConfig(), transport=httpx.MockTransport(recorder))

    # 直接调 _fallback 而不经过 asyncio.run 的 on_event 外壳不可行（它本身是协程），
    # 因此这里验证的是「创建 task 失败」这条分支本身：模拟 create_task 抛 RuntimeError。
    original = asyncio.create_task

    def boom(coro):
        coro.close()
        raise RuntimeError("no running event loop")

    asyncio.create_task = boom
    try:
        _run(client._fallback(InboundEvent("mid-n", "10001", "在吗"), "timeout"))
    finally:
        asyncio.create_task = original

    assert len(cc.compensate_backlog) == 1
    assert cc.compensate_backlog[0]["payload"]["original_event_key"] == "mid-n"


# ---------------------------------------------------------------- 4. URL 派生


@pytest.mark.parametrize(
    "core_api, expected",
    [
        ("http://127.0.0.1:8000/api/v1/chat", "http://127.0.0.1:8000/api/v1/compensate"),
        # 带路径前缀：只换 /api/ 之后的部分，前缀必须保留
        ("https://gw.example.com/ffw/api/v1/chat", "https://gw.example.com/ffw/api/v1/compensate"),
        # 没有 /api/ 的异常配置：退化为同主机根路径追加，不能抛异常
        ("http://127.0.0.1:9000/chat", "http://127.0.0.1:9000/chat/api/v1/compensate"),
    ],
    ids=["默认", "带前缀", "无api段"],
)
def test_补偿URL从core_api派生(core_api, expected):
    """配置里只有 ``core_api`` 一个旋钮：再加一个 URL 就是新的故障面。"""
    client = CoreClient(AdapterConfig(core_api=core_api))
    assert client._compensate_url() == expected


# ---------------------------------------------------------------- 5. 不干扰主链路


def test_正常响应不触发补偿():
    """一切正常时绝不能凭空多打一个补偿请求（那会变成每次对话都重复扣分）。"""
    recorder = Recorder(chat_response=httpx.Response(200, json={
        "actions": [{"type": "text", "text": "在的"}],
        "inner_os": "",
        "final_prompt": "",
        "debug": {},
    }))
    result = _run(_adapter(recorder).on_event(InboundEvent("mid-ok", "10001", "在吗")))

    assert result.degraded is False
    assert recorder.compensate_requests() == []


def test_补偿不阻塞回复时序():
    """补账用 create_task 排后台：回复交付必须早于补偿请求发出。

    用「补偿 handler 里置标记」的方式验证：若实现改成 await 补偿再返回回复，
    sent_texts 与标记的相对顺序就会翻转。
    """
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/compensate"):
            order.append("compensate")
            return httpx.Response(200, json={})
        order.append("chat")
        raise httpx.ConnectTimeout("slow")

    client = CoreClient(AdapterConfig(), transport=httpx.MockTransport(handler))
    adapter = FatFishAdapter(host=MockHostAPI(), core=client)

    async def scenario():
        result = await adapter.on_event(InboundEvent("mid-o", "10001", "在吗"))
        order.append("replied")
        # 让出若干轮事件循环，把后台 task 跑完
        for _ in range(20):
            await asyncio.sleep(0)
        return result

    result = _run(scenario())
    assert result.execution.sent_texts, "回复必须已经发出"
    assert order.index("replied") < order.index("compensate"), (
        "补账必须晚于回复交付，否则就是把用户等待赌在补账上"
    )


# ---------------------------------------------------------------- 6. 日志措辞（判据 8 逐字）


def test_补偿成功日志措辞(caplog):
    """判据 8 要求日志里能找到 "compensate request sent"（逐字，供 grep 与告警规则）。"""
    import logging

    recorder = Recorder(chat_response=httpx.ConnectTimeout("t"))
    adapter = _adapter(recorder)

    async def scenario():
        await adapter.on_event(InboundEvent("mid-log", "10001", "在吗"))
        for _ in range(20):
            await asyncio.sleep(0)

    with caplog.at_level(logging.INFO, logger="ffw.adapter.core"):
        _run(scenario())

    assert "compensate request sent" in caplog.text
    assert len(recorder.compensate_requests()) == 1


def test_补偿失败日志措辞与队列长度(caplog):
    """判据 8：Core 不可达时日志含 "[WARN] compensate failed"，且队列长度恰为 1。"""
    import logging

    recorder = Recorder(
        chat_response=httpx.ConnectTimeout("t"),
        compensate_response=httpx.ConnectTimeout("down"),
    )
    adapter = _adapter(recorder)

    async def scenario():
        await adapter.on_event(InboundEvent("mid-log2", "10001", "在吗"))
        for _ in range(20):
            await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, logger="ffw.adapter.core"):
        _run(scenario())

    assert "[WARN] compensate failed" in caplog.text
    assert len(cc.compensate_backlog) == 1


def test_灌满队列后最旧一条被丢():
    """判据 8：灌 101 条后恒为 100，且丢掉的是最旧那条（不是最新的）。"""
    for index in range(cc.COMPENSATE_QUEUE_MAX + 1):
        cc.compensate_backlog.append(
            {"payload": {"original_event_key": "k{0}".format(index)}, "reason": "timeout"}
        )

    assert len(cc.compensate_backlog) == cc.COMPENSATE_QUEUE_MAX
    assert cc.compensate_backlog[0]["payload"]["original_event_key"] == "k1", "最旧的 k0 该被丢"
    assert cc.compensate_backlog[-1]["payload"]["original_event_key"] == "k100"
