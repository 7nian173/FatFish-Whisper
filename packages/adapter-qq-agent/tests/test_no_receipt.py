"""A-A6：失败动作不产生假回执。

背景：``executor`` 旧实现在 ``send_text`` 返回空/None 时仍把文本记进
``sent_texts`` 且不记 error——调用方会以为话已送到。本修复：无回执 =
未确认送达 → 进 ``failed_texts`` / ``failed_stickers`` + ``errors``，
**不进** ``sent_message_ids`` / ``sent_texts`` / ``stickers``；
``last_message_id`` 仍只认有回执的消息，因此撤回不会发生。
"""
from __future__ import annotations

import asyncio
import random

from fatfish_adapter import (
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

TARGET = Target(user_id="10001", group_id="88888")


async def _no_sleep(_seconds: float) -> None:
    return None


def _executor(host) -> ActionExecutor:
    return ActionExecutor(host, typing_delay=(0, 0), sleep=_no_sleep, rng=random.Random(0))


def _reply(actions, **extra) -> CoreReply:
    payload = {"status": "ok", "actions": actions}
    payload.update(extra)
    return CoreReply.from_raw(payload)


class NoReceiptHost:
    """send_text / send_sticker 一律返回空串（宿主没给回执）。"""

    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.recalls: list[dict] = []

    async def send_text(self, target: Target, text: str) -> str:
        self.sends.append({"kind": "text", "content": text, "message_id": ""})
        return ""

    async def send_poke(self, target: Target, user_id: str) -> None:
        self.sends.append({"kind": "poke", "content": user_id, "message_id": None})

    async def send_sticker(self, target: Target, tag: str) -> str:
        self.sends.append({"kind": "sticker", "content": tag, "message_id": ""})
        return ""

    async def recall(self, target: Target, message_id: str) -> None:
        self.recalls.append({"target": str(target), "message_id": message_id})


class MixedHost:
    """第一条文本有回执，第二条没有（模拟宿主偶发丢回执）。"""

    def __init__(self) -> None:
        self.sends: list[dict] = []

    async def send_text(self, target: Target, text: str) -> str:
        first = not any(s["kind"] == "text" for s in self.sends)
        message_id = "mid-ok-1" if first else ""
        self.sends.append({"kind": "text", "content": text, "message_id": message_id})
        return message_id

    async def send_poke(self, target: Target, user_id: str) -> None:
        return None

    async def send_sticker(self, target: Target, tag: str) -> str:
        return ""

    async def recall(self, target: Target, message_id: str) -> None:
        return None


# ---------------------------------------------------------------- 执行器


def test_文本无回执不进sent_texts():
    host = NoReceiptHost()
    result = asyncio.run(_executor(host).run(_reply([
        {"type": "text", "content": "你好呀"},
    ]), TARGET))

    assert result.sent_texts == [], "无回执的文本不能记成已发"
    assert result.failed_texts == ["你好呀"]
    assert result.errors == ["text:no_receipt"]
    assert result.sent_message_ids == []
    assert result.last_message_id is None


def test_表情无回执不进stickers():
    host = NoReceiptHost()
    result = asyncio.run(_executor(host).run(_reply([
        {"type": "sticker", "tag": "委屈"},
    ]), TARGET))

    assert result.stickers == []
    assert result.failed_stickers == ["委屈"]
    assert result.errors == ["sticker:no_receipt"]
    assert result.last_message_id is None


def test_有回执时行为不变():
    host = MockHostAPI()
    result = asyncio.run(_executor(host).run(_reply([
        {"type": "text", "content": "嗯。"},
        {"type": "sticker", "tag": "委屈"},
    ]), TARGET))

    assert result.sent_texts == ["嗯。"]
    assert result.stickers == ["委屈"]
    assert result.failed_texts == [] and result.failed_stickers == []
    assert result.errors == []
    assert result.last_message_id is not None


def test_部分无回执只影响失败的那条():
    host = MixedHost()
    result = asyncio.run(_executor(host).run(_reply([
        {"type": "text", "content": "第一条"},
        {"type": "text", "content": "第二条"},
    ]), TARGET))

    assert result.sent_texts == ["第一条"]
    assert result.failed_texts == ["第二条"]
    assert result.sent_message_ids == ["mid-ok-1"]
    assert result.last_message_id == "mid-ok-1", "撤回只能拿有回执的那个 id"
    assert "text:no_receipt" in result.errors


def test_发送抛异常仍走errors且不记已发():
    host = MockHostAPI(fail_on_send=True)
    result = asyncio.run(_executor(host).run(_reply([
        {"type": "text", "content": "你好呀"},
    ]), TARGET))

    assert result.sent_texts == []
    assert result.failed_texts == []
    assert len(result.errors) == 1 and result.errors[0].startswith("text:")


# ---------------------------------------------------------------- 整链路


def _adapter_with_fake_core(host) -> FatFishAdapter:
    cfg = AdapterConfig()
    cfg.core_api = "http://127.0.0.1:9/api/v1/chat"
    adapter = FatFishAdapter(
        host=host,
        core=CoreClient(cfg),
        roller=RecallRoller(host, cooldown=180, rng=random.Random(0)),
    )

    async def fake_ask(event):
        return _reply(
            [{"type": "text", "content": "你好呀"}],
            recall_hint={"probability": 1.0},
        )

    adapter.core.ask = fake_ask  # type: ignore[method-assign]
    return adapter


def test_无回执时不发起撤回():
    """有 recall_hint 但没拿到 message_id：撤回判定根本不该发生。"""
    host = NoReceiptHost()
    adapter = _adapter_with_fake_core(host)

    result = asyncio.run(adapter.on_event(
        InboundEvent(platform_message_id="m-noreceipt", user_id="10001", message="在吗")
    ))

    assert result.execution.sent_texts == []
    assert result.execution.failed_texts == ["你好呀"]
    assert result.recall is None, "没有 message_id 就不该判定撤回"
    assert host.recalls == []


def test_summary带失败字段():
    """失败留痕要能透过 summary 看到（可观测字段设计总则）。"""
    host = NoReceiptHost()
    adapter = _adapter_with_fake_core(host)

    result = asyncio.run(adapter.on_event(
        InboundEvent(platform_message_id="m-summary", user_id="10001", message="在吗")
    ))
    summary = result.summary()

    assert summary["sent"] == []
    assert summary["sent_message_ids"] == []
    assert summary["failed_texts"] == ["你好呀"]
    assert summary["errors"] == ["text:no_receipt"]
    assert summary["recalled"] is False
