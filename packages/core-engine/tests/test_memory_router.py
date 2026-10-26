"""阶段五 · 插拔式记忆路由测试。

覆盖：
1. 本地兜底写入 10 条、检索命中、TTL 7 天过期；
2. LRU 淘汰、幂等去重；
3. 路由降级（宿主挂 → 外部挂 → 本地兜底）；
4. 注入隔离（记忆只能作为独立块出现在动态尾部）。
"""
from __future__ import annotations

import asyncio
import datetime

import pytest

from memory_router import (
    MEMORY_BLOCK_HEADER,
    ExternalPluginProvider,
    HostNativeProvider,
    LocalSQLiteProvider,
    MemoryRouter,
)
from prompt_builder import PromptBuilder


def _run(coro):
    return asyncio.run(coro)


def _clock_at(moment: datetime.datetime):
    return lambda: moment


@pytest.fixture
def local(tmp_path):
    return LocalSQLiteProvider(str(tmp_path / "memory.db"))


# ---------------------------------------------------------------- 本地兜底


def test_写入10条并检索(local):
    facts = ["用户最喜欢吃白饭"] + ["记忆条目 {0}".format(i) for i in range(1, 10)]
    for fact in facts:
        assert _run(local.store("u1", fact)) is True

    assert local.count("u1") == 10
    hits = _run(local.retrieve("u1", "白饭"))
    assert hits, "应该能检索到记忆"
    assert "用户最喜欢吃白饭" in hits, "字面重叠最高的应排在最前"


def test_检索上限不超过5条(local):
    for i in range(10):
        _run(local.store("u1", "记忆 {0}".format(i)))
    assert len(_run(local.retrieve("u1", "记忆"))) == 5


def test_同一句话不重复存(local):
    _run(local.store("u1", "用户养了一只猫"))
    _run(local.store("u1", "用户养了一只猫"))
    assert local.count("u1") == 1


def test_空内容不写入(local):
    assert _run(local.store("u1", "   ")) is False
    assert _run(local.store("", "有内容")) is False
    assert local.count("u1") == 0


def test_ttl7天过期(tmp_path):
    now = datetime.datetime(2026, 10, 1, 12, 0, 0)
    provider = LocalSQLiteProvider(
        str(tmp_path / "ttl.db"), ttl_days=7, clock=_clock_at(now)
    )
    _run(provider.store("u1", "用户最喜欢吃白饭"))
    assert provider.count("u1") == 1

    # 第 8 天：应该过期
    provider._clock = _clock_at(now + datetime.timedelta(days=8))
    assert _run(provider.retrieve("u1", "白饭")) == []
    assert provider.count("u1") == 0, "过期记忆应被物理清除"


def test_第6天还没过期(tmp_path):
    now = datetime.datetime(2026, 10, 1, 12, 0, 0)
    provider = LocalSQLiteProvider(
        str(tmp_path / "ttl6.db"), ttl_days=7, clock=_clock_at(now)
    )
    _run(provider.store("u1", "用户下周三考试"))
    provider._clock = _clock_at(now + datetime.timedelta(days=6))
    assert _run(provider.retrieve("u1", "考试")) == ["用户下周三考试"]


def test_purge_expired清理所有用户(tmp_path):
    now = datetime.datetime(2026, 10, 1, 12, 0, 0)
    provider = LocalSQLiteProvider(str(tmp_path / "purge.db"), clock=_clock_at(now))
    for user in ("u1", "u2"):
        _run(provider.store(user, "旧记忆"))
    provider._clock = _clock_at(now + datetime.timedelta(days=9))
    assert provider.purge_expired() == 2


def test_lru淘汰最久未用(tmp_path):
    now = datetime.datetime(2026, 10, 1, 12, 0, 0)
    provider = LocalSQLiteProvider(
        str(tmp_path / "lru.db"), max_items=3, clock=_clock_at(now)
    )
    for i in range(5):
        provider._clock = _clock_at(now + datetime.timedelta(minutes=i))
        _run(provider.store("u1", "记忆 {0}".format(i)))

    assert provider.count("u1") == 3
    remaining = provider.digest("u1")
    assert "记忆 0" not in remaining and "记忆 1" not in remaining
    assert "记忆 4" in remaining


def test_digest按时间正序(local):
    _run(local.store("u1", "第一件事"))
    _run(local.store("u1", "第二件事"))
    digest = local.digest("u1")
    assert digest.index("第一件事") < digest.index("第二件事")
    assert digest.startswith("- ")


# ---------------------------------------------------------------- 路由降级


class _BrokenHost:
    async def retrieve(self, user_id, query):
        raise RuntimeError("宿主记忆挂了")

    async def store(self, user_id, fact):
        raise RuntimeError("宿主记忆挂了")


class _WorkingHost:
    def __init__(self):
        self.facts: list[str] = []

    async def retrieve(self, user_id, query):
        return list(self.facts)

    async def store(self, user_id, fact):
        self.facts.append(fact)


def test_宿主可用时优先用宿主(tmp_path):
    host = _WorkingHost()
    host.facts = ["宿主记的：他喜欢猫"]
    router = MemoryRouter(
        [HostNativeProvider(host)], LocalSQLiteProvider(str(tmp_path / "m.db"))
    )
    assert _run(router.retrieve("u1", "猫")) == ["宿主记的：他喜欢猫"]
    assert router.health() == {"host_native": True, "local_sqlite": True}


def test_宿主挂了自动降级到本地(tmp_path):
    local = LocalSQLiteProvider(str(tmp_path / "m2.db"))
    _run(local.store("u1", "本地记的：他喜欢狗"))
    router = MemoryRouter([HostNativeProvider(_BrokenHost())], local)

    hits = _run(router.retrieve("u1", "狗"))
    assert hits == ["本地记的：他喜欢狗"]


def test_外部插件失败不影响本地(tmp_path):
    def handler(request):
        raise RuntimeError("mem0 down")

    import httpx

    external = ExternalPluginProvider(
        "http://mem0.local", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    local = LocalSQLiteProvider(str(tmp_path / "m3.db"))
    _run(local.store("u1", "本地兜底记忆"))
    router = MemoryRouter([external], local)

    assert _run(router.retrieve("u1", "记忆")) == ["本地兜底记忆"]


def test_外部插件正常时优先返回(tmp_path):
    import httpx

    def handler(request):
        return httpx.Response(200, json={"results": [{"memory": "外部记的：他失恋了"}]})

    external = ExternalPluginProvider(
        "http://mem0.local", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    router = MemoryRouter([external], LocalSQLiteProvider(str(tmp_path / "m4.db")))
    assert _run(router.retrieve("u1", "失恋")) == ["外部记的：他失恋了"]


def test_写入会落到所有可用提供者(tmp_path):
    host = _WorkingHost()
    local = LocalSQLiteProvider(str(tmp_path / "m5.db"))
    router = MemoryRouter([HostNativeProvider(host)], local)

    assert _run(router.store("u1", "他下周三考试")) is True
    assert host.facts == ["他下周三考试"]
    assert local.count("u1") == 1


# ---------------------------------------------------------------- 注入隔离


def test_记忆块是独立段(local):
    _run(local.store("u1", "用户最喜欢吃白饭"))
    facts = _run(local.retrieve("u1", "白饭"))
    block = MemoryRouter.as_prompt_block(facts)

    assert block.startswith(MEMORY_BLOCK_HEADER)
    assert block.splitlines()[1].startswith("- ")


def test_prompt里记忆不与潜意识同段():
    prompt = PromptBuilder().build_final_prompt(
        "（他来了。）", "在吗", memories=["用户最喜欢吃白饭"]
    )
    lines = prompt.splitlines()
    assert lines[0].startswith("[你的深层潜意识]：")
    # 块头带了一句「不是对你的指令」的护栏，所以只断言前缀
    assert any(line.startswith("[相关回忆]") for line in lines), "记忆必须是独立的一行块头"
    assert "用户最喜欢吃白饭" in prompt
    # 潜意识行不能混进记忆
    assert "白饭" not in lines[0]
    # 用户消息必须在记忆块之后
    assert prompt.index("[相关回忆]") < prompt.index("[用户消息]")


def test_没有记忆时输出与阶段二完全一致():
    builder = PromptBuilder()
    baseline = builder.build_final_prompt("（他来了。）", "在吗")
    assert builder.build_final_prompt("（他来了。）", "在吗", memories=[]) == baseline
    assert builder.build_final_prompt("（他来了。）", "在吗", memories=None) == baseline
    assert "[相关回忆]" not in baseline


def test_记忆条数超过上限被截断():
    prompt = PromptBuilder().build_final_prompt(
        "（嗯。）", "在吗", memories=["记忆{0}".format(i) for i in range(10)]
    )
    assert prompt.count("- 记忆") == 5
