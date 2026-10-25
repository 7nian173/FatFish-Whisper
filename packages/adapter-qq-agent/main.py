"""QQ Bot 侧 Adapter：防抖合并 -> 请求情绪机 -> 执行动作 -> 撤回掷骰。

宿主接入方式：把 on_message 接到你的消息事件上即可，例如

    adapter = FatFishAdapter(send_text=bot.send, send_poke=bot.poke, recall=bot.recall)
    bot.on_message(lambda event: adapter.on_message(str(event.user_id), event.message))
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

CORE_API = os.environ.get("FFW_CORE_API", "http://127.0.0.1:8000/api/v1/chat")
TIMEOUT = float(os.environ.get("FFW_TIMEOUT", "3.0"))
DEBOUNCE = float(os.environ.get("FFW_DEBOUNCE", "1.0"))
RECALL_COOLDOWN = float(os.environ.get("FFW_RECALL_COOLDOWN", "180"))

SendText = Callable[[str, str], Awaitable[Any]]
SendPoke = Callable[[str, str], Awaitable[Any]]
RecallMessage = Callable[[Any], Awaitable[Any]]


class Debouncer:
    """同一用户在窗口内的连续消息合并成一条。"""

    def __init__(self, window: float = 1.0):
        self.window = window
        self._buffers: dict[str, list[str]] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def merge(self, user_id: str, message: str) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._buffers.setdefault(user_id, []).append(message)
        self._waiters.setdefault(user_id, []).append(future)

        task = self._tasks.get(user_id)
        if task is None or task.done():
            self._tasks[user_id] = asyncio.create_task(self._flush(user_id))
        return await future

    async def _flush(self, user_id: str) -> None:
        await asyncio.sleep(self.window)
        texts = self._buffers.pop(user_id, [])
        waiters = self._waiters.pop(user_id, [])
        self._tasks.pop(user_id, None)
        merged = "\n".join(texts)
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(merged)


class RecallGuard:
    """撤回冷却：同一用户短时间内只掷一次骰子。"""

    def __init__(self, cooldown: float = 180.0):
        self.cooldown = cooldown
        self._last: dict[str, float] = {}

    def allow(self, user_id: str) -> bool:
        now = time.time()
        last = self._last.get(user_id, 0.0)
        if now - last < self.cooldown:
            return False
        self._last[user_id] = now
        return True


class FatFishAdapter:
    def __init__(self, send_text: Optional[SendText] = None,
                 send_poke: Optional[SendPoke] = None,
                 recall_message: Optional[RecallMessage] = None):
        self.send_text = send_text
        self.send_poke = send_poke
        self.recall_message = recall_message
        self.debouncer = Debouncer(DEBOUNCE)
        self.guard = RecallGuard(RECALL_COOLDOWN)
        self._client: Optional[httpx.AsyncClient] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def on_message(self, user_id: str, message: str) -> Optional[dict[str, Any]]:
        """宿主消息事件入口；返回情绪机原始响应（便于宿主侧调试）。"""
        merged = await self.debouncer.merge(user_id, message)

        try:
            client = await self._http()
            resp = await client.post(CORE_API, json={"user_id": user_id, "message": merged})
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            print("[FFW] 情绪机超时，降级到原生 LLM 回复")
            return None
        except (httpx.HTTPError, ValueError) as exc:
            print("[FFW] 情绪机异常，降级: " + str(exc))
            return None

        await self._execute(user_id, data)
        return data

    async def _execute(self, user_id: str, data: dict[str, Any]) -> None:
        for action in data.get("actions", []):
            kind = action.get("type")
            if kind == "text" and self.send_text is not None:
                await self.send_text(user_id, action.get("content", ""))
            elif kind == "poke" and self.send_poke is not None:
                await self.send_poke(action.get("target", user_id), action.get("suffix", ""))
            else:
                print("[FFW] 未实现的动作类型: " + str(kind))

        hint = data.get("recall_hint")
        if hint and self.recall_message is not None and self.guard.allow(user_id):
            probability = float(hint.get("probability", 0.0))
            if random.random() < probability:
                await self.recall_message(user_id)


# ---------------------------------------------------------------- 自测

async def _demo() -> None:
    async def send_text(user_id: str, text: str) -> None:
        print("[发送 -> " + user_id + "] " + text)

    adapter = FatFishAdapter(send_text=send_text, send_poke=None, recall_message=None)
    for message in ("今天好累啊", "不过还是想和你说说话", "我没事，真的"):
        data = await adapter.on_message("10001", message)
        if data:
            print("  内心OS: " + str(data.get("inner_os")))
            print("  debug : " + str(data.get("debug")))
    await adapter.close()


if __name__ == "__main__":
    asyncio.run(_demo())
