"""Adapter 入口（manifest.entry）。

宿主接入只要三步：

    from main import FatFishAdapter, InboundEvent
    from fatfish_adapter.host import HostAPI      # 自己实现四个方法

    adapter = FatFishAdapter(host=MyNapCatHost(bot))
    bot.on_message(lambda e: adapter.on_event(InboundEvent(
        platform_message_id=str(e.message_id),   # ← 必须透传，判重靠它
        user_id=str(e.user_id),
        message=e.raw_message,
        group_id=str(e.group_id) if e.group_id else None,
        is_at_bot=e.is_at_bot,
    )))

也兼容阶段一的旧写法（三个回调）：

    adapter = FatFishAdapter(send_text=bot.send, send_poke=bot.poke,
                             recall_message=bot.recall)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Optional

# 宿主以文件路径加载时，保证同目录的 fatfish_adapter 包可被 import
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fatfish_adapter import (  # noqa: E402
    Action,
    ActionExecutor,
    AdapterConfig,
    CallbackHost,
    CoreClient,
    CoreReply,
    FatFishAdapter as _ServiceAdapter,
    HostAPI,
    InboundEvent,
    MockHostAPI,
    NativeLLM,
    RecallDecision,
    RecallRoller,
    Target,
    TurnResult,
)

__all__ = [
    "FatFishAdapter",
    "AdapterConfig",
    "CoreClient",
    "ActionExecutor",
    "RecallRoller",
    "HostAPI",
    "MockHostAPI",
    "CallbackHost",
    "InboundEvent",
    "Target",
    "Action",
    "CoreReply",
    "TurnResult",
    "RecallDecision",
]

logger = logging.getLogger("ffw.adapter")


class FatFishAdapter(_ServiceAdapter):
    """门面：既支持 ``host=`` 新写法，也兼容三个回调的旧写法。"""

    def __init__(
        self,
        host: Optional[HostAPI] = None,
        send_text: Optional[Callable[[str, str], Awaitable[Any]]] = None,
        send_poke: Optional[Callable[[str, str], Awaitable[Any]]] = None,
        recall_message: Optional[Callable[[Any], Awaitable[Any]]] = None,
        config: Optional[AdapterConfig] = None,
        native_llm: Optional[NativeLLM] = None,
        core: Optional[CoreClient] = None,
        executor: Optional[ActionExecutor] = None,
        roller: Optional[RecallRoller] = None,
    ) -> None:
        if host is None:
            host = CallbackHost(send_text, send_poke, recall_message)
        super().__init__(
            host=host,
            config=config,
            native_llm=native_llm,
            core=core,
            executor=executor,
            roller=roller,
        )


# ---------------------------------------------------------------- 本地演示

async def _demo() -> None:
    """不连 Core 也能跑：用一个假 Core 响应走完整执行链路。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host = MockHostAPI()
    adapter = FatFishAdapter(host=host)

    fake_reply = CoreReply.from_raw(
        {
            "status": "ok",
            "actions": [
                {"type": "text", "content": "（揉揉眼睛）你回来啦。"},
                {"type": "poke", "target": "10001"},
                {"type": "sticker", "tag": "委屈"},
            ],
            "inner_os": "（他居然还记得我。）",
            "recall_hint": {"probability": 0.0},
            "debug": {"emotion": 62, "level": "L0"},
        }
    )
    result = await adapter.executor.run(fake_reply, Target(user_id="10001"))
    print("执行结果:", result)
    print("Mock 发送日志:")
    for item in host.sends:
        print("  ", item)
    await adapter.close()


if __name__ == "__main__":
    asyncio.run(_demo())
