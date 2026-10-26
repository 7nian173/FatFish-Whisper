"""阶段四验收脚本：模拟 Adapter 打 Core，验证「重推判重 + 动作执行 + 撤回冷却」。

跑法（在仓库根目录）：

    venv/Scripts/python.exe packages/adapter-qq-agent/simulate_duplicate.py

它做四件事：
1. **进程内**起一个真实 Core（FastAPI + 真 EmotionEngine，临时 db/cache，
   不碰你的 app.db），用 httpx.ASGITransport 直连，无需开端口；
2. 用同一个 ``platform_message_id`` 连发两次，打印两次响应，
   验证第二次被 ``IngressDeduper`` 拦下（status=duplicate / applied=False）；
3. 打印 Mock 宿主的发送日志（第一次有动作，第二次一条都没有）；
4. 单独演示撤回掷骰与冷却（probability=1.0 强制撤回，第二次进冷却）。
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE_SRC = os.path.join(os.path.dirname(_HERE), "core-engine", "src")
# 顺序：Core 的 src 必须在前面（它内部是平铺导入 models/engine/main），
# Adapter 用独立包名 fatfish_adapter，不会和 Core 抢模块名。
for _path in (_HERE, _CORE_SRC):
    while _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, _HERE)
sys.path.insert(0, _CORE_SRC)

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

LINE = "=" * 68


def _build_core(tmp_dir: str):
    """进程内构造一个真实 Core 应用（临时库，不污染 app.db）。"""
    from engine import EmotionEngine
    from main import app

    engine = EmotionEngine(
        db_path=os.path.join(tmp_dir, "sim.db"),
        cache_dir=os.path.join(tmp_dir, "sim_cache"),
        env={},  # 不配 LLM，Track B 直接走中性 50，测试不依赖外网
    )
    app.state.engine = engine
    return app, engine


def _print_reply(tag: str, result: Any) -> None:
    summary = result.summary()
    print(f"\n[{tag}] Core 响应摘要")
    print(f"   status        : {summary['status']}")
    print(f"   duplicate     : {summary['duplicate']}")
    print(f"   applied       : {result.reply.debug.get('applied')}")
    print(f"   emotion/level : {summary['emotion']} / {summary['level']}")
    print(f"   inner_os      : {summary['inner_os']}")
    print(f"   执行的动作     : {summary['sent'] or '（无）'}")
    print(f"   message_id    : {summary['sent_message_ids'] or '（无）'}")
    print(f"   撤回          : {summary['recall']}（recalled={summary['recalled']}）")


async def _demo_duplicate() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="ffw_sim_")
    app, engine = _build_core(tmp_dir)
    host = MockHostAPI()

    transport = httpx.ASGITransport(app=app)
    core = CoreClient(AdapterConfig(), transport=transport)
    adapter = FatFishAdapter(host=host, core=core)

    event = InboundEvent(
        platform_message_id="napcat-msg-777",   # ← 平台原生消息 ID
        user_id="10001",
        message="其实我今天真的很累",
        group_id="88888",
        is_at_bot=True,
    )

    print(LINE)
    print("① 第一次投递（正常处理）")
    print(LINE)
    print("   请求体:", event.to_payload())
    first = await adapter.on_event(event)
    _print_reply("第一次", first)

    print("\n" + LINE)
    print("② 瞬间重发同一个 platform_message_id（模拟 NapCat 重推）")
    print(LINE)
    second = await adapter.on_event(event)
    _print_reply("第二次", second)

    print("\n" + LINE)
    print("③ 判定结果")
    print(LINE)
    ok = second.reply.duplicate and not second.execution.sent_texts
    print(f"   第二次是否被判重 : {second.reply.duplicate}")
    print(f"   第二次是否发了消息: {bool(second.execution.sent_texts)}")
    print(f"   IngressDeduper   : {'✅ 生效' if ok else '❌ 未生效'}")
    print(f"   Core 返回 reason : {second.reply.debug.get('reason')}")

    print("\n" + LINE)
    print("④ Mock 宿主发送日志（重推没有产生任何发送）")
    print(LINE)
    if host.sends:
        for index, item in enumerate(host.sends, 1):
            print(f"   {index}. {item}")
    else:
        print("   （无）")

    await adapter.close()
    engine.cache.close()
    engine.db_engine.dispose()


async def _demo_recall() -> None:
    print("\n" + LINE)
    print("⑤ 撤回掷骰 + 冷却（probability=1.0 强制命中）")
    print(LINE)

    host = MockHostAPI()
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    executor = ActionExecutor(
        host, typing_delay=(0.5, 1.5), sleep=fake_sleep, rng=random.Random(0)
    )
    roller = RecallRoller(host, cooldown=180, rng=random.Random(0))
    target = Target(user_id="10001", group_id="88888")

    reply = CoreReply.from_raw(
        {
            "status": "ok",
            "actions": [
                {"type": "text", "content": "……你才想起来找我。"},
                {"type": "text", "content": "算了，理你一下。"},
                {"type": "poke", "target": "10001"},
            ],
            "recall_hint": {"probability": 1.0},
        }
    )

    result = await executor.run(reply, target)
    print(f"   发出消息   : {result.sent_texts}")
    print(f"   消息 ID    : {result.sent_message_ids}")
    print(f"   拟人打字延迟: {[round(s, 2) for s in sleep_calls]} 秒")

    decision = await roller.maybe_recall(
        target, result.last_message_id, reply.recall_probability, reply.recall_hint
    )
    print(f"   撤回判定   : recalled={decision.recalled} reason={decision.reason}")

    # 冷却期内再判定一次：应该被拦住
    reply2 = CoreReply.from_raw(
        {"status": "ok", "actions": [{"type": "text", "content": "嗯。"}],
         "recall_hint": {"probability": 1.0}}
    )
    result2 = await executor.run(reply2, target)
    decision2 = await roller.maybe_recall(
        target, result2.last_message_id, reply2.recall_probability, reply2.recall_hint
    )
    print(f"   冷却内再判定: recalled={decision2.recalled} reason={decision2.reason}")

    print("\n   Mock 撤回日志:")
    for item in host.recalls:
        print(f"     {item}")
    if not host.recalls:
        print("     （无）")


async def _demo_timeout() -> None:
    print("\n" + LINE)
    print("⑥ 超时降级（Core 不可达，超时 0.3s）")
    print(LINE)
    cfg = AdapterConfig()
    cfg.core_api = "http://127.0.0.1:9/api/v1/chat"  # discard 端口，必然连不上
    cfg.timeout = 0.3
    cfg.connect_timeout = 0.3

    host = MockHostAPI()
    adapter = FatFishAdapter(host=host, core=CoreClient(cfg))
    result = await adapter.on_event(
        InboundEvent(platform_message_id="m-timeout", user_id="10001", message="在吗")
    )
    print(f"   status     : {result.reply.status}")
    print(f"   reason     : {result.reply.debug.get('fallback_reason')}")
    print(f"   兜底发出的  : {result.execution.sent_texts}")
    await adapter.close()


async def main() -> None:
    await _demo_duplicate()
    await _demo_recall()
    await _demo_timeout()
    print("\n" + LINE)
    print("模拟结束。")
    print(LINE)


if __name__ == "__main__":
    asyncio.run(main())
