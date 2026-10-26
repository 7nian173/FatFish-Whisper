"""阶段五 · Token 监控测试（含 /api/v1/admin/token_stats 接口）。"""
from __future__ import annotations

import asyncio
import datetime

import httpx
import pytest

from monitor import (
    MOD_COMPRESS,
    MOD_EVOLVE,
    MOD_OOC,
    MOD_TRACK_B,
    TokenMonitor,
    parse_usage,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 解析


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"usage": {"prompt_tokens": 10, "completion_tokens": 5}}, (10, 5)),
        ({"usage": {"prompt_tokens": "7", "completion_tokens": None}}, (7, 0)),
        ({"usage": {}}, (0, 0)),
        ({}, (0, 0)),
        (None, (0, 0)),
        ("not a dict", (0, 0)),
        ({"usage": {"prompt_tokens": -3, "completion_tokens": 2}}, (0, 2)),
    ],
)
def test_parse_usage容错(payload, expected):
    assert parse_usage(payload) == expected


# ---------------------------------------------------------------- 记账


def test_按模块分组统计(engine):
    monitor = engine.monitor
    monitor.record(MOD_TRACK_B, (100, 20), "u1")
    monitor.record(MOD_TRACK_B, (50, 10), "u1")
    monitor.record(MOD_EVOLVE, (30, 40), "u1")

    stats = monitor.stats_today()
    assert stats[MOD_TRACK_B]["prompt_tokens"] == 150
    assert stats[MOD_TRACK_B]["completion_tokens"] == 30
    assert stats[MOD_TRACK_B]["total_tokens"] == 180
    assert stats[MOD_TRACK_B]["calls"] == 2
    assert stats[MOD_EVOLVE]["total_tokens"] == 70


def test_零消耗不记账(engine):
    engine.monitor.record(MOD_TRACK_B, (0, 0), "u1")
    assert engine.monitor.stats_today() == {}


def test_汇总包含总计(engine):
    engine.monitor.record(MOD_TRACK_B, (10, 5), "u1")
    engine.monitor.record(MOD_OOC, (20, 8), "u1")
    summary = engine.monitor.summary_dict(engine.monitor.stats_today())
    assert summary["total"]["prompt_tokens"] == 30
    assert summary["total"]["completion_tokens"] == 13
    assert summary["total"]["total_tokens"] == 43
    assert summary["total"]["calls"] == 2
    assert set(summary["modules"]) == {MOD_TRACK_B, MOD_OOC}


def test_按日期过滤(engine):
    yesterday = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=1)
    engine.monitor.record(MOD_TRACK_B, (10, 5), "u1", created_at=yesterday)
    assert engine.monitor.stats_today() == {}
    assert MOD_TRACK_B in engine.monitor.stats_today(yesterday.date())


def test_记账失败不影响主流程():
    """session_factory 抛异常时只记日志，不冒泡。"""

    class Broken:
        def __call__(self):
            raise RuntimeError("db is gone")

    monitor = TokenMonitor(Broken())
    monitor.record(MOD_TRACK_B, (5, 5), "u1")  # 不应抛异常
    # 落库失败时自动回退到内存账本，数据不丢
    assert monitor.stats_today()[MOD_TRACK_B]["total_tokens"] == 10


# ---------------------------------------------------------------- 接口


@pytest.fixture
def client(engine, monkeypatch):
    from main import app

    monkeypatch.setattr(app.state, "engine", engine, raising=False)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://core")


def test_token_stats接口(client, engine):
    engine.monitor.record(MOD_TRACK_B, (100, 20), "u1")
    engine.monitor.record(MOD_EVOLVE, (30, 40), "u1")

    async def scenario():
        async with client as http:
            return await http.get("/api/v1/admin/token_stats", headers={"X-FFW-Admin": "1"})

    response = _run(scenario())
    assert response.status_code == 200
    data = response.json()
    assert data["modules"][MOD_TRACK_B]["total_tokens"] == 120
    assert data["modules"][MOD_EVOLVE]["total_tokens"] == 70
    assert data["total"]["total_tokens"] == 190
    assert data["day"] == datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def test_token_stats非法日期返回400(client):
    async def scenario():
        async with client as http:
            return await http.get("/api/v1/admin/token_stats", params={"day": "2026/10/25"}, headers={"X-FFW-Admin": "1"})

    assert _run(scenario()).status_code == 400


def test_memory_health接口(client):
    async def scenario():
        async with client as http:
            return await http.get("/api/v1/admin/memory_health", headers={"X-FFW-Admin": "1"})

    data = _run(scenario()).json()
    assert "local_sqlite" in data["providers"]
    assert data["providers"]["local_sqlite"] is True


# ---------------------------------------------------------------- 端到端


def test_普通对话的token也进账本(client, engine):
    """走完整 handle_message：Track A 不花 token，但 debug 里能看记忆条数。"""

    async def scenario():
        async with client as http:
            return await http.post(
                "/api/v1/chat",
                json={"user_id": "u-token", "message": "今天天气不错", "platform_message_id": "m-1"},
            )

    response = _run(scenario())
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "memories" in payload["debug"]
