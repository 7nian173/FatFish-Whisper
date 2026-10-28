"""后台接口测试：bands 真源、人设读写、演化状态、SQLite WAL。

这几条对应"把已实现能力露出来"的那批改动：
- ``/api/v1/state`` 返回六段阈值元数据（前端不再抄一份）；
- ``/api/v1/admin/persona`` 读写人设卡（保存即生效）；
- ``/api/v1/admin/evolution_status`` 给演化时间线供数；
- app.db / memory.db 开 WAL（并发写不再撞 ``database is locked``）。

注意：这里**不跑 lifespan**（``ASGITransport`` 默认不跑），直接把临时引擎挂到
``app.state`` 上，因此绝不会碰到仓库里的真实 ``app.db``。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import httpx
import pytest

import main as core_main

ADMIN_HEADERS = {"X-FFW-Admin": "1"}


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=core_main.app, client=("127.0.0.1", 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://core")


@pytest.fixture
def mounted_engine(engine_factory):
    """把临时引擎挂到 app.state，用完摘掉（避免污染其它用例）。"""
    engine = engine_factory({})
    core_main.app.state.engine = engine
    try:
        yield engine
    finally:
        for attr in ("engine", "evolution", "evolution_last_report", "evolution_last_run_at"):
            if hasattr(core_main.app.state, attr):
                try:
                    delattr(core_main.app.state, attr)
                except AttributeError:
                    pass


# ---------------------------------------------------------------- bands 真源


def test_state返回六段bands(mounted_engine):
    async def scenario():
        async with _client() as http:
            return await http.get("/api/v1/state", headers=ADMIN_HEADERS)

    response = asyncio.run(scenario())
    assert response.status_code == 200
    body = response.json()
    bands = body["bands"]
    assert len(bands) == 6
    assert bands[0]["min"] == 0 and bands[-1]["max"] == 100
    assert [item["label"] for item in bands] == ["CRISIS", "L2", "L1", "L0", "P1", "P2"]
    assert "color" not in bands[0], "颜色属于设计系统，后端不该下发"


def test_state仍然返回bot_mood与traits(mounted_engine):
    async def scenario():
        async with _client() as http:
            return await http.get("/api/v1/state", headers=ADMIN_HEADERS)

    body = asyncio.run(scenario()).json()
    assert "bot_mood" in body
    assert "traits" in body


# ---------------------------------------------------------------- 人设读写


def test_人设读取_文件不存在时返回空(mounted_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONA_PATH", str(tmp_path / "persona.txt"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    async def scenario():
        async with _client() as http:
            return await http.get("/api/v1/admin/persona", headers=ADMIN_HEADERS)

    body = asyncio.run(scenario()).json()
    assert body["text"] == ""
    assert body["chars"] == 0
    assert body["max_chars"] == 1000
    assert body["will_compress_next_run"] is False


def test_人设保存后立刻可读(mounted_engine, tmp_path, monkeypatch):
    persona_path = tmp_path / "persona.txt"
    monkeypatch.setenv("PERSONA_PATH", str(persona_path))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    text = "你是潜鱼潜语，一只嘴硬心软的鲸。"

    async def scenario():
        async with _client() as http:
            saved = await http.post(
                "/api/v1/admin/persona", headers=ADMIN_HEADERS, json={"text": text}
            )
            loaded = await http.get("/api/v1/admin/persona", headers=ADMIN_HEADERS)
            return saved, loaded

    saved, loaded = asyncio.run(scenario())
    assert saved.status_code == 200
    assert saved.json()["ok"] is True
    assert saved.json()["hot_reloaded"] is True
    assert saved.json()["chars"] == len(text)
    assert loaded.json()["text"] == text
    assert persona_path.read_text(encoding="utf-8") == text


def test_人设超长提示将触发压缩(mounted_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONA_PATH", str(tmp_path / "persona.txt"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    async def scenario():
        async with _client() as http:
            return await http.post(
                "/api/v1/admin/persona",
                headers=ADMIN_HEADERS,
                json={"text": "设" * 1200},
            )

    body = asyncio.run(scenario()).json()
    assert body["chars"] == 1200
    assert body["will_compress_next_run"] is True


def test_人设覆盖前留手工备份(mounted_engine, tmp_path, monkeypatch):
    persona_path = tmp_path / "persona.txt"
    backups = tmp_path / "backups"
    monkeypatch.setenv("PERSONA_PATH", str(persona_path))
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    persona_path.write_text("旧人设", encoding="utf-8")

    async def scenario():
        async with _client() as http:
            return await http.post(
                "/api/v1/admin/persona", headers=ADMIN_HEADERS, json={"text": "新人设"}
            )

    assert asyncio.run(scenario()).status_code == 200
    names = os.listdir(backups)
    assert any("_manual_" in name for name in names), names
    assert persona_path.read_text(encoding="utf-8") == "新人设"


def test_人设空文本被拒(mounted_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONA_PATH", str(tmp_path / "persona.txt"))

    async def scenario():
        async with _client() as http:
            return await http.post(
                "/api/v1/admin/persona", headers=ADMIN_HEADERS, json={"text": ""}
            )

    assert asyncio.run(scenario()).status_code == 422


# ---------------------------------------------------------------- 演化状态


def test_演化状态返回完整字段(mounted_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONA_PATH", str(tmp_path / "persona.txt"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("EVOLUTION_ENABLED", "1")

    async def scenario():
        async with _client() as http:
            return await http.get("/api/v1/admin/evolution_status", headers=ADMIN_HEADERS)

    response = asyncio.run(scenario())
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["next_run_at"].endswith("Z")
    assert body["last_result"] is None, "进程内还没跑过演化"
    assert body["backup"] == {"latest": None, "count": 0}
    assert body["ooc_probe"] == {"passed": 0, "rejected": 0}
    assert body["persona"]["max_chars"] == 1000
    assert body["active_users"] == 0


def test_演化状态未启用时enabled为假(mounted_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONA_PATH", str(tmp_path / "persona.txt"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("EVOLUTION_ENABLED", raising=False)

    async def scenario():
        async with _client() as http:
            return await http.get("/api/v1/admin/evolution_status", headers=ADMIN_HEADERS)

    assert asyncio.run(scenario()).json()["enabled"] is False


def test_演化报告回调写入state(mounted_engine):
    from evolution import EvolutionReport

    report = EvolutionReport(day="2026-10-26", persona_chars=742)
    report.evolved.append({"user_id": "1", "appended": True})
    report.skipped.append({"user_id": "2", "reason": "ooc_conflict"})

    core_main._record_evolution(report)

    assert core_main.app.state.evolution_last_report["persona_chars"] == 742
    assert core_main.app.state.evolution_last_run_at.endswith("Z")


def test_演化状态统计ooc通过与被拒(mounted_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONA_PATH", str(tmp_path / "persona.txt"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    from evolution import EvolutionReport

    report = EvolutionReport(day="2026-10-26")
    report.evolved.extend([{"user_id": "1"}, {"user_id": "2"}])
    report.skipped.extend(
        [{"user_id": "3", "reason": "ooc_conflict"}, {"user_id": "4", "reason": "inactive"}]
    )
    core_main._record_evolution(report)

    async def scenario():
        async with _client() as http:
            return await http.get("/api/v1/admin/evolution_status", headers=ADMIN_HEADERS)

    body = asyncio.run(scenario()).json()
    assert body["ooc_probe"] == {"passed": 2, "rejected": 1}


# ---------------------------------------------------------------- WAL


def test_app_db开启WAL(engine_factory):
    engine = engine_factory({})
    with engine.db_engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert str(mode).lower() == "wal"


def test_memory_db开启WAL(tmp_path):
    from memory_router import LocalSQLiteProvider

    db_path = str(tmp_path / "memory.db")
    provider = LocalSQLiteProvider(db_path)
    asyncio.run(provider.store("10001", "他喜欢下雨天"))

    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


def test_WAL下并发写不锁库(engine_factory):
    """演化任务与 API 写入并发时，WAL 至少要让"先等一会儿"而不是立刻抛锁错误。"""
    import threading

    engine = engine_factory({})
    errors: list[str] = []

    def writer(text: str) -> None:
        try:
            asyncio.run(engine.memory.local.store("10001", text))
        except Exception as exc:  # pragma: no cover - 出问题才走这里
            errors.append(str(exc))

    threads = [threading.Thread(target=writer, args=("记忆" + str(i),)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == [], errors
