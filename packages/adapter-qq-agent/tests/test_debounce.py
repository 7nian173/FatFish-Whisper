"""防抖合并队列测试：合并、批内判重、批满即冲、关闭不悬挂。

跑法（仓库根目录）：
    venv/Scripts/python.exe -m pytest packages/adapter-qq-agent/tests -q
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER = os.path.dirname(_HERE)
if _ADAPTER not in sys.path:
    sys.path.insert(0, _ADAPTER)

from fatfish_adapter import DebounceQueue, InboundEvent, merge_events  # noqa: E402


def _ev(mid: str, text: str, user: str = "10001", group: str | None = None) -> InboundEvent:
    return InboundEvent(platform_message_id=mid, user_id=user, message=text, group_id=group)


async def _tick(times: int = 4) -> None:
    """让出若干次事件循环，确保所有 submit 都已入批。"""
    for _ in range(times):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- 合并本身


def test_合并多条消息用换行连接():
    merged = merge_events([_ev("1", "在吗"), _ev("2", "在干嘛"), _ev("3", "理我一下")])
    assert merged.message == "在吗\n在干嘛\n理我一下"
    assert merged.platform_message_id == "1", "批次身份取第一条消息的 id"


def test_合并时任一at即视为at():
    first = _ev("1", "a")
    second = InboundEvent("2", "10001", "b", is_at_bot=True)
    assert merge_events([first, second]).is_at_bot is True


def test_合并空列表报错():
    with pytest.raises(ValueError):
        merge_events([])


# ---------------------------------------------------------------- 队列行为


def test_窗口内的连发只冲刷一次():
    calls: list[tuple[str, int]] = []

    async def handler(event: InboundEvent, count: int) -> str:
        calls.append((event.message, count))
        return "result-" + str(count)

    async def scenario():
        gate = asyncio.Event()

        async def gated_sleep(_seconds: float) -> None:
            await gate.wait()

        queue = DebounceQueue(handler, window=0.3, clock_sleep=gated_sleep)
        tasks = [
            asyncio.create_task(queue.submit(_ev("1", "在吗"))),
            asyncio.create_task(queue.submit(_ev("2", "在干嘛"))),
            asyncio.create_task(queue.submit(_ev("3", "理我一下"))),
        ]
        await _tick()
        gate.set()
        return await asyncio.gather(*tasks), queue

    results, queue = asyncio.run(scenario())

    assert len(calls) == 1, "三条消息只应产生一次 Core 请求"
    assert calls[0] == ("在吗\n在干嘛\n理我一下", 3)
    assert results == ["result-3"] * 3, "同一批的调用方共享同一份结果"
    assert queue.pending_count() == 0


def test_不同用户不合并():
    calls: list[tuple[str, int]] = []

    async def handler(event: InboundEvent, count: int) -> str:
        calls.append((event.user_id, count))
        return "ok"

    async def scenario():
        gate = asyncio.Event()

        async def gated_sleep(_seconds: float) -> None:
            await gate.wait()

        queue = DebounceQueue(handler, window=0.3, clock_sleep=gated_sleep)
        tasks = [
            asyncio.create_task(queue.submit(_ev("1", "我是A", user="A"))),
            asyncio.create_task(queue.submit(_ev("2", "我是B", user="B"))),
        ]
        await _tick()
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert len(calls) == 2, "两个用户各成一批"
    assert all(count == 1 for _user, count in calls)


def test_同群不同用户不合并():
    """只按 Target.key 合并会把全群消息揉成一条，这里必须按用户分。"""
    calls: list[tuple[str, int]] = []

    async def handler(event: InboundEvent, count: int) -> str:
        calls.append((event.user_id, count))
        return "ok"

    async def scenario():
        gate = asyncio.Event()

        async def gated_sleep(_seconds: float) -> None:
            await gate.wait()

        queue = DebounceQueue(handler, window=0.3, clock_sleep=gated_sleep)
        tasks = [
            asyncio.create_task(queue.submit(_ev("1", "A说的话", user="A", group="g1"))),
            asyncio.create_task(queue.submit(_ev("2", "B说的话", user="B", group="g1"))),
        ]
        await _tick()
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert len(calls) == 2


def test_批内相同消息ID只算一条():
    calls: list[tuple[str, int]] = []

    async def handler(event: InboundEvent, count: int) -> str:
        calls.append((event.message, count))
        return "ok"

    async def scenario():
        gate = asyncio.Event()

        async def gated_sleep(_seconds: float) -> None:
            await gate.wait()

        queue = DebounceQueue(handler, window=0.3, clock_sleep=gated_sleep)
        tasks = [
            asyncio.create_task(queue.submit(_ev("dup", "同一条"))),
            asyncio.create_task(queue.submit(_ev("dup", "同一条"))),
        ]
        await _tick()
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert calls == [("同一条", 1)], "宿主重推的消息不该重复入批"


def test_批满立刻冲刷不等窗口():
    calls: list[tuple[str, int]] = []

    async def handler(event: InboundEvent, count: int) -> str:
        calls.append((event.message, count))
        return "ok"

    async def scenario():
        async def never_sleep(_seconds: float) -> None:
            await asyncio.Event().wait()  # 永不返回：只能靠批满触发

        queue = DebounceQueue(handler, window=10.0, max_batch=2, clock_sleep=never_sleep)
        tasks = [
            asyncio.create_task(queue.submit(_ev("1", "a"))),
            asyncio.create_task(queue.submit(_ev("2", "b"))),
        ]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)

    asyncio.run(scenario())
    assert calls == [("a\nb", 2)]


def test_关闭队列时等待方拿到None():
    async def handler(event: InboundEvent, count: int) -> str:
        return "ok"

    async def scenario():
        async def never_sleep(_seconds: float) -> None:
            await asyncio.Event().wait()

        queue = DebounceQueue(handler, window=10.0, clock_sleep=never_sleep)
        task = asyncio.create_task(queue.submit(_ev("1", "在吗")))
        await _tick()
        await queue.close()
        return await asyncio.wait_for(task, timeout=1.0)

    assert asyncio.run(scenario()) is None


def test_处理器异常传给等待方():
    async def handler(event: InboundEvent, count: int) -> str:
        raise RuntimeError("core boom")

    async def scenario():
        gate = asyncio.Event()

        async def gated_sleep(_seconds: float) -> None:
            await gate.wait()

        queue = DebounceQueue(handler, window=0.3, clock_sleep=gated_sleep)
        task = asyncio.create_task(queue.submit(_ev("1", "在吗")))
        await _tick()
        gate.set()
        return await task

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())
