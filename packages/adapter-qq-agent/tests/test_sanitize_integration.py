"""CQ 码清洗的入口集成验收（裁决 A-10 拆件吸收）。

单元测试在 `test_sanitize.py`（来自 B 线，13 条）。本文件只守**接缝**：
``FatFishAdapter.on_event`` 是否真的在请求 Core 之前把消息洗过。

为什么这条接缝必须单独测
------------------------
`sanitize.clean_cq` 写对了但没接进 `on_event`，测试照样全绿——这正是
「模块已实现、链路未启用」的经典假绿（B 线的 debounce 就吃过这个亏）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER = os.path.dirname(_HERE)
if _ADAPTER not in sys.path:
    sys.path.insert(0, _ADAPTER)

from fatfish_adapter import AdapterConfig, CoreClient, FatFishAdapter, InboundEvent  # noqa: E402
from fatfish_adapter.host import MockHostAPI  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _capture_client(sink: list[dict]):
    """返回一个会把请求体记进 sink 的 CoreClient（并回一个合法响应）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "actions": [{"type": "text", "content": "嗯。"}],
                "inner_os": "（他来了。）",
                "final_prompt": "[用户消息]\n…",
                "debug": {},
            },
        )

    return CoreClient(AdapterConfig(), transport=httpx.MockTransport(handler))


def test_CQ码在请求Core前被剥离():
    sink: list[dict] = []
    adapter = FatFishAdapter(host=MockHostAPI(), core=_capture_client(sink))

    _run(adapter.on_event(InboundEvent("m1", "10001", "[CQ:at,qq=999] 在吗")))

    assert len(sink) == 1, "应恰好请求一次 Core"
    assert sink[0]["message"] == "在吗", "CQ 码必须在入口剥离，不能透传给 Core"


def test_纯图片消息保留占位标记():
    sink: list[dict] = []
    adapter = FatFishAdapter(host=MockHostAPI(), core=_capture_client(sink))

    _run(adapter.on_event(InboundEvent("m1", "10001", "[CQ:image,file=abc.jpg]")))

    assert len(sink) == 1
    assert sink[0]["message"] == "[图片]", "纯媒体消息应保留类型占位，参数不得外泄"
    assert "abc.jpg" not in sink[0]["message"], "文件参数绝不能喂给模型"


def test_纯at消息不请求Core():
    """纯 @ / 纯表情清洗后为空 → 直接跳过，不发消息、不计 Token。"""
    sink: list[dict] = []
    host = MockHostAPI()
    adapter = FatFishAdapter(host=host, core=_capture_client(sink))

    result = _run(adapter.on_event(InboundEvent("m1", "10001", "[CQ:at,qq=999]")))

    assert sink == [], "清洗后为空不得请求 Core"
    assert result.reply.status == "skipped"
    assert result.execution.sent_texts == [], "跳过的消息不该发任何话术"


def test_转义的CQ码也能剥离():
    """OneBot 部分实现会把 [ ] 转成 HTML 实体，清洗前必须先还原。"""
    sink: list[dict] = []
    adapter = FatFishAdapter(host=MockHostAPI(), core=_capture_client(sink))

    _run(adapter.on_event(InboundEvent("m1", "10001", "&#91;CQ:at,qq=1&#93; 你好")))

    assert len(sink) == 1
    assert sink[0]["message"] == "你好"
