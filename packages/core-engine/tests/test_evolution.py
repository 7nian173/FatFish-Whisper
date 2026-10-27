"""阶段五 · 7 日演化系统测试（Mock LLM，不联网）。

覆盖：
1. 正常演化：追加词写入人设卡且 ≤80 字；
2. OOC 探针驳回：人设卡一字不动；
3. 活跃度闸门 / 缺备份 / 无记忆 / LLM 未配置 四种跳过路径；
4. 膨胀压缩：人设卡超 1000 字强制压缩；
5. 模型话痨时追加词被硬截断；
6. Token 记账（MOD_EVOLVE / MOD_OOC）。
"""
from __future__ import annotations

import asyncio
import datetime
import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evolution import (
    MAX_APPEND_CHARS,
    MAX_PERSONA_CHARS,
    EvolutionEngine,
)
from memory_router import LocalSQLiteProvider
from models import Base, MessageCount
from monitor import MOD_EVOLVE, MOD_OOC, TokenMonitor

#: 演化基准日 = **当前 UTC 日**（不要硬编码具体日期！）
#: 历史教训：曾写死 2026-10-25，而 token 记账按 utcnow() 落库——跨过 UTC 零点后
#: 「查 10-25 但记录在 10-26」→ test_演化与ooc的token分别记账 必挂（日期炸弹）。
DAY = datetime.datetime.now(datetime.timezone.utc).date()
OLD_DAY = DAY - datetime.timedelta(days=7)


def _run(coro):
    return asyncio.run(coro)


def _llm_response(content: str, prompt_tokens: int = 12, completion_tokens: int = 6):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


def make_handler(append_text: str, conflict: bool = False, compressed: str = ""):
    """按 system prompt 关键字分派：演化 / OOC / 压缩。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        system = body["messages"][0]["content"]
        if "一致性审查员" in system:
            return _llm_response(json.dumps({"conflict": conflict, "reason": "测试"}))
        if "压缩器" in system:
            return _llm_response(compressed or "压缩后的人设卡")
        return _llm_response(append_text)

    return handler


@pytest.fixture
def env():
    return {
        "LLM_API_BASE": "https://api.deepseek.com/v1",
        "LLM_API_KEY": "sk-test-key",
        "LLM_MODEL": "deepseek-chat",
        "LLM_TIMEOUT": "2",
    }


@pytest.fixture
def core_db(tmp_path):
    db_path = str(tmp_path / "app.db")
    engine = create_engine("sqlite:///" + db_path, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    yield db_path, factory
    engine.dispose()


def _set_activity(factory, user_id: str, count: int, day: datetime.date = DAY) -> None:
    with factory() as session:
        session.add(MessageCount(user_id=user_id, day=day.isoformat(), count=count))
        session.commit()


def _build(tmp_path, core_db, env, handler, persona="你是冷酷无情的杀手，说话极简，从不笑。", with_memory=True):
    db_path, factory = core_db
    persona_path = tmp_path / "persona.txt"
    persona_path.write_text(persona, encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / ("persona_" + OLD_DAY.strftime("%Y%m%d") + ".txt")).write_text(
        "你是冷酷无情的杀手。", encoding="utf-8"
    )

    memory = LocalSQLiteProvider(str(tmp_path / "memory.db"))
    if with_memory:
        _run(memory.store("u1", "用户今天说天气很好，笑得很开心"))
        _run(memory.store("u1", "用户请他一起看夕阳"))

    monitor = TokenMonitor(factory)
    engine = EvolutionEngine(
        db_path=db_path,
        persona_path=str(persona_path),
        backups_dir=str(backups),
        memory=memory,
        env=env,
        monitor=monitor,
        clock=lambda: datetime.datetime.combine(DAY, datetime.time(3, 0)),
        session_factory=factory,
        transport=httpx.MockTransport(handler),
    )
    return engine, persona_path, factory


# ---------------------------------------------------------------- 正常路径


def test_演化生成追加词并写入人设卡(tmp_path, core_db, env):
    """死线 D11：演化只**产出建议**，建议由返回值携带，物理人设卡一字不动。"""
    text = "开始愿意陪用户看夕阳，语气略软。"
    engine, persona_path, _ = _build(tmp_path, core_db, env, make_handler(text))
    before = persona_path.read_text(encoding="utf-8")

    result = _run(engine.evolve_user("u1", DAY))

    assert result["appended"] is True, result
    assert result["ooc"]["passed"] is True

    # 1) 建议文本走返回值，不落盘
    assert "suggested_persona_append" in result
    assert "看夕阳" in result["suggested_persona_append"], result
    assert result["suggested_persona_append"] == result["append"]
    assert len(result["suggested_persona_append"]) <= MAX_APPEND_CHARS
    assert len(result["append"]) <= MAX_APPEND_CHARS

    # 2) 物理文件未变（不写盘才是正确行为）
    after = persona_path.read_text(encoding="utf-8")
    assert after == before, "演化建议不得写入物理人设卡"
    assert "看夕阳" not in after

    # 3) persona_chars 只是"若采纳"的字数估算，不是写盘结果
    assert result["persona_chars"] == len(before.rstrip("\n") + "\n" + result["append"] + "\n")


def test_追加词超80字被硬截断(tmp_path, core_db, env):
    long_text = "他很喜欢和用户聊天，" * 30  # 远超 80 字
    engine, persona_path, _ = _build(tmp_path, core_db, env, make_handler(long_text))

    result = _run(engine.evolve_user("u1", DAY))
    assert result["appended"] is True
    assert len(result["append"]) <= MAX_APPEND_CHARS
    assert len(engine.read_persona()) < len(long_text)


def test_模型加的引号和前缀被清掉(tmp_path, core_db, env):
    engine, _, _ = _build(
        tmp_path, core_db, env, make_handler('追加：\"对用户更耐心了一点。\"')
    )
    result = _run(engine.evolve_user("u1", DAY))
    assert result["append"] == "对用户更耐心了一点。"


# ---------------------------------------------------------------- OOC 拦截


def test_ooc冲突时驳回且人设卡不变(tmp_path, core_db, env):
    engine, persona_path, _ = _build(
        tmp_path, core_db, env, make_handler("变得阳光开朗，见谁都笑。", conflict=True)
    )
    before = persona_path.read_text(encoding="utf-8")

    result = _run(engine.evolve_user("u1", DAY))

    assert result["appended"] is False
    assert result["reason"] == "ooc_rejected"
    assert result["ooc"]["passed"] is False
    assert persona_path.read_text(encoding="utf-8") == before


def test_ooc探针挂了则放行(tmp_path, core_db, env):
    """探针不可用时不阻断演化（只保留「只做加法」的硬约束）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if "一致性审查员" in body["messages"][0]["content"]:
            raise httpx.ConnectError("probe down")
        return _llm_response("语气稍微柔和了一点。")

    engine, _, _ = _build(tmp_path, core_db, env, handler)
    result = _run(engine.evolve_user("u1", DAY))
    assert result["appended"] is True
    assert result["ooc"]["reason"] == "ooc_probe_unavailable"


# ---------------------------------------------------------------- 跳过路径


def test_没有7天前备份则跳过(tmp_path, core_db, env):
    engine, _, _ = _build(tmp_path, core_db, env, make_handler("随便"))
    (tmp_path / "backups" / ("persona_" + OLD_DAY.strftime("%Y%m%d") + ".txt")).unlink()

    result = _run(engine.evolve_user("u1", DAY))
    assert result["appended"] is False
    assert result["reason"] == "no_backup_7_days_ago"


def test_没有记忆则跳过(tmp_path, core_db, env):
    engine, _, _ = _build(tmp_path, core_db, env, make_handler("随便"), with_memory=False)
    result = _run(engine.evolve_user("u1", DAY))
    assert result["reason"] == "no_memory"


def test_llm未配置则跳过(tmp_path, core_db):
    engine, _, _ = _build(
        tmp_path, core_db, {"LLM_API_BASE": "", "LLM_API_KEY": "", "LLM_MODEL": ""}, make_handler("随便")
    )
    result = _run(engine.evolve_user("u1", DAY))
    assert result["reason"] == "llm_disabled"


def test_活跃度不足不备份不演化(tmp_path, core_db, env):
    engine, _, factory = _build(tmp_path, core_db, env, make_handler("语气柔和了一点。"))
    _set_activity(factory, "u1", 5)  # 低于 20 条门槛

    report = _run(engine.run(DAY))
    assert report.active_users == []
    assert report.evolved == []
    assert report.backed_up == []


def test_活跃用户才会备份并演化(tmp_path, core_db, env):
    engine, _, factory = _build(tmp_path, core_db, env, make_handler("语气柔和了一点。"))
    _set_activity(factory, "u1", 25)

    report = _run(engine.run(DAY))
    assert report.active_users == ["u1"]
    assert len(report.backed_up) == 1
    assert report.evolved and report.evolved[0]["user_id"] == "u1"


def test_force跳过活跃度闸门(tmp_path, core_db, env):
    engine, _, factory = _build(tmp_path, core_db, env, make_handler("语气柔和了一点。"))
    _set_activity(factory, "u1", 0)

    report = _run(engine.run(DAY, force=True))
    assert report.active_users == ["u1"]


# ---------------------------------------------------------------- 膨胀压缩


def test_人设卡超1000字触发压缩(tmp_path, core_db, env):
    """死线 D11：压缩只影响**建议内容**，物理人设卡一字不动。"""
    long_persona = ("你是冷酷无情的杀手。" + "设定细节。" * 500)[:995]
    compressed = "压缩后：你是冷酷无情的杀手，说话极简。"
    seen: dict[str, str] = {}
    base = make_handler("开始愿意陪用户看夕阳。", compressed=compressed)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if "压缩器" in body["messages"][0]["content"]:
            seen["compress_prompt"] = body["messages"][-1]["content"]
        return base(request)

    engine, persona_path, _ = _build(
        tmp_path,
        core_db,
        env,
        handler,
        persona=long_persona,
    )
    before = persona_path.read_text(encoding="utf-8")
    assert len(before) <= MAX_PERSONA_CHARS

    result = _run(engine.evolve_user("u1", DAY))

    # 1) 压缩链路确实被触发：压缩器收到了「原人设 + 追加词」的超限内容
    assert "compress_prompt" in seen, "超 1000 字必须调用压缩器"
    assert "设定细节。" in seen["compress_prompt"]
    assert result["appended"] is True
    assert result["compressed"] is True

    # 2) 物理文件未变：压缩结果不落盘
    after = persona_path.read_text(encoding="utf-8")
    assert after == before, "演化建议（含压缩结果）不得写入物理人设卡"
    assert engine.read_persona() == before
    assert "压缩后" not in engine.read_persona()

    # 3) 建议内容 = 压缩后文本：persona_chars 是「若采纳」的字数（= len(compressed)）
    assert len(compressed) <= MAX_PERSONA_CHARS
    assert result["persona_chars"] == len(compressed)
    assert result["persona_chars"] <= MAX_PERSONA_CHARS
    # suggested_persona_append 语义见 evolution.evolve_user docstring：
    # 它只承载本次演化生成的原始追加词；压缩后的全文由 compressed/persona_chars 表达。
    assert result["suggested_persona_append"] == "开始愿意陪用户看夕阳。"


def test_压缩失败则保持原样不写入(tmp_path, core_db, env):
    long_persona = ("你是杀手。" * 250)[:990]
    engine, persona_path, _ = _build(
        tmp_path,
        core_db,
        env,
        make_handler("开始愿意陪用户看夕阳。", compressed="还是很长" * 300),
        persona=long_persona,
    )
    before = engine.read_persona()

    result = _run(engine.evolve_user("u1", DAY))
    assert result["appended"] is False
    assert result["reason"] == "compress_failed_keep_original"
    assert engine.read_persona() == before


# ---------------------------------------------------------------- Token 记账


def test_演化与ooc的token分别记账(tmp_path, core_db, env):
    engine, _, factory = _build(tmp_path, core_db, env, make_handler("语气柔和了一点。"))
    _run(engine.evolve_user("u1", DAY))

    stats = engine.monitor.stats_today(DAY)
    assert MOD_EVOLVE in stats
    assert MOD_OOC in stats
    assert stats[MOD_EVOLVE]["calls"] == 1
    assert stats[MOD_EVOLVE]["total_tokens"] == 18


# ---------------------------------------------------------------- 备份


def test_备份幂等不覆盖(tmp_path, core_db, env):
    engine, _, _ = _build(tmp_path, core_db, env, make_handler("x"))
    first = engine.backup(DAY)
    content = open(first, encoding="utf-8").read()
    engine.write_persona(content + "\n后来改了一句。")
    second = engine.backup(DAY)

    assert first == second
    assert open(second, encoding="utf-8").read() == content, "同一天重复备份不应覆盖"


def test_下次运行时间是明天3点(tmp_path, core_db, env):
    engine, _, _ = _build(tmp_path, core_db, env, make_handler("x"))
    now = datetime.datetime(2026, 10, 25, 3, 0, 0)
    assert engine.next_run_at(now) == datetime.datetime(2026, 10, 26, 3, 0, 0)
    early = datetime.datetime(2026, 10, 25, 2, 0, 0)
    assert engine.next_run_at(early) == datetime.datetime(2026, 10, 25, 3, 0, 0)
