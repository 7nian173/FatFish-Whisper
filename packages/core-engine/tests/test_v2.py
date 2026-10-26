"""情绪引擎 V2.0 验收测试。

覆盖用户要求的三个必测项 + 三个补充项：
1. 兜底：`6` / `123` 判为 noise，不触发情绪更新，Prompt 不出现讨好型语句
2. 解禁：连续 4 次同极性刺激 -> stimulus 递增，强制触发时输出 override_directive
3. 性格调制：neuroticism 0.9 时负面跌幅大于 0.5
4. Bot 自身情绪：全局一份，变化幅度小于用户情绪
5. 记忆 Pin：L2 / CRISIS 写入的记忆不受 TTL 淘汰
6. 安全优先：硬拦截命中时**不会**被概率解禁绕过
"""
from __future__ import annotations

import asyncio
import types

import pytest

from auditor import SubconsciousAuditor
from engine import EmotionEngine
from persona_traits import PersonaTraits
from prompt_builder import NOISE_REPLIES

FAWNING = ("知道了", "好的", "我在", "有什么可以帮你", "请稍等")


def _run(coro):
    return asyncio.run(coro)


def _force_unlock(engine: EmotionEngine) -> None:
    """把随机源固定为「一定命中」，让解禁可被确定性验证。"""
    engine.auditor._rng = types.SimpleNamespace(random=lambda: 0.0, choice=lambda seq: seq[0])


def _never_unlock(engine: EmotionEngine) -> None:
    engine.auditor._rng = types.SimpleNamespace(random=lambda: 0.99, choice=lambda seq: seq[0])


# ---------------------------------------------------------------- 1. 兜底


@pytest.mark.parametrize("text", ["6", "123", "？", "。。。", "aaaa"])
def test_噪音输入判为noise且不更新情绪(engine_factory, text):
    engine = engine_factory({})
    payload = _run(engine.handle_message("u-noise", text))
    debug = payload["debug"]

    assert debug["path"] == "noise", debug
    assert debug["intent_type"] == "noise", debug
    assert debug["applied"] is False, "噪音不应触发情绪更新"
    assert debug["delta"] == 0, debug
    assert debug["target"] == 50, debug


def test_噪音状态不输出讨好型语句(engine_factory):
    engine = engine_factory({})
    payload = _run(engine.handle_message("u-noise2", "6"))

    first_line = payload["final_prompt"].splitlines()[0]
    os_text = first_line.split("：", 1)[-1]
    assert os_text in NOISE_REPLIES, "噪音状态必须换成傲娇短句：{0}".format(os_text)

    action_text = payload["actions"][0]["content"]
    assert action_text not in FAWNING, action_text
    assert "禁止" in payload["final_prompt"], "必须带上「禁止讨好型句式」的硬约束"


def test_正常短句不被误判为噪音(engine_factory):
    """「我在呢」这类正常短句应该走 unknown，而不是 noise。"""
    engine = engine_factory({})
    payload = _run(engine.handle_message("u-normal", "我在呢"))
    assert payload["debug"]["intent_type"] == "unknown", payload["debug"]


# ---------------------------------------------------------------- 2. 解禁


def test_连续刺激计数递增(engine_factory):
    engine = engine_factory({})
    _never_unlock(engine)
    counters = []
    for _ in range(4):
        payload = _run(engine.handle_message("u-stim", "太开心了"))
        counters.append(payload["debug"]["stimulus"])
    assert counters == [1, 2, 3, 4], counters


def test_连续四次同极性可触发解禁(engine_factory):
    engine = engine_factory({})
    _force_unlock(engine)

    payloads = [_run(engine.handle_message("u-unlock", "太开心了")) for _ in range(4)]
    unlocked = [p for p in payloads if p["debug"]["audit"] == "unlock"]
    assert unlocked, "连续同极性刺激必须能触发解禁"

    payload = unlocked[0]
    debug = payload["debug"]
    assert debug["stimulus"] >= 3, debug
    # 解禁指令必须在 Prompt 最末尾，且带安全重申
    assert "系统特批" in payload["final_prompt"]
    tail = payload["final_prompt"].rstrip().splitlines()[-1]
    assert "依然禁止" in tail, "安全底线必须压在最末尾：{0}".format(tail)


def test_解禁后计数重置(engine_factory):
    """破防之后要收回来：解禁一次后窗口清零，不会每轮都解禁。"""
    engine = engine_factory({})
    _force_unlock(engine)
    for _ in range(3):
        _run(engine.handle_message("u-reset2", "太开心了"))
    payload = _run(engine.handle_message("u-reset2", "太开心了"))
    assert payload["debug"]["stimulus"] == 1, payload["debug"]


def test_换极性会重置计数(engine_factory):
    engine = engine_factory({})
    _never_unlock(engine)
    for _ in range(3):
        _run(engine.handle_message("u-switch", "太开心了"))
    payload = _run(engine.handle_message("u-switch", "我崩溃了，想哭"))
    assert payload["debug"]["stimulus"] == 1, payload["debug"]


# ---------------------------------------------------------------- 3. 性格调制


def test_高神经质放大负面跌幅(engine_factory):
    calm = engine_factory({})
    calm.traits = PersonaTraits(neuroticism=0.5, extraversion=0.5, agreeableness=0.5, resilience=0.5)
    sensitive = engine_factory({})
    sensitive.traits = PersonaTraits(neuroticism=0.9, extraversion=0.5, agreeableness=0.5, resilience=0.5)

    a = _run(calm.handle_message("u-calm", "今天好累啊"))
    b = _run(sensitive.handle_message("u-sensitive", "今天好累啊"))

    assert a["debug"]["target"] == b["debug"]["target"] == 35
    assert abs(b["debug"]["delta"]) > abs(a["debug"]["delta"]), (a["debug"], b["debug"])


def test_性格调制不突破限幅(engine_factory):
    engine = engine_factory({})
    engine.traits = PersonaTraits(neuroticism=1.0, extraversion=1.0, agreeableness=0.5, resilience=0.5)
    payload = _run(engine.handle_message("u-clamp", "我崩溃了，想哭"))
    assert abs(payload["debug"]["delta"]) <= 5.0 + 1e-6, payload["debug"]


# ---------------------------------------------------------------- 4. Bot 自身情绪


def test_Bot有自己的情绪值(engine_factory):
    engine = engine_factory({})
    payload = _run(engine.handle_message("u-bot", "今天好累啊"))

    assert "bot_mood" in payload["debug"], "debug 必须暴露 Bot 自身情绪"
    assert 0 <= payload["debug"]["bot_mood"] <= 100
    # 慢变量：单轮变化幅度小于用户情绪
    assert abs(payload["debug"]["bot_mood"] - 50) < abs(payload["debug"]["emotion"] - 50)


def test_Bot情绪与用户情绪相互独立(engine_factory):
    engine = engine_factory({})
    _run(engine.handle_message("u-a", "我崩溃了，想哭"))
    bot_after_a = engine._get_bot_mood()["emotion"]
    _run(engine.handle_message("u-b", "太棒了哈哈哈"))
    bot_after_b = engine._get_bot_mood()["emotion"]
    assert bot_after_a != bot_after_b, "不同用户的消息应共同影响 Bot 心情"
    user_b = engine._get_or_create("u-b")["emotion"]
    assert user_b != bot_after_b, "用户情绪与 Bot 情绪必须是两份独立状态"


# ---------------------------------------------------------------- 5. 记忆 Pin


def test_情绪显著时记忆上锁(engine_factory):
    engine = engine_factory({})
    local = engine.memory.local
    _run(local.store("u-pin", "他说他今天撑不住了", salience=1.0))
    _run(local.store("u-pin", "普通的一句闲聊", salience=0.0))

    with local._connect() as conn:
        rows = {
            row["fact"]: int(row["pinned"])
            for row in conn.execute("SELECT fact, pinned FROM memories WHERE user_id=?", ("u-pin",))
        }
    assert rows["他说他今天撑不住了"] == 1
    assert rows["普通的一句闲聊"] == 0


def test_Pin的记忆不参与TTL淘汰(engine_factory):
    engine = engine_factory({})
    local = engine.memory.local
    _run(local.store("u-ttl", "被锁住的记忆", salience=1.0))
    # 手动把创建时间改到 30 天前
    with local._connect() as conn:
        conn.execute(
            "UPDATE memories SET created_at='2000-01-01 00:00:00' WHERE user_id=?",
            ("u-ttl",),
        )
    facts = _run(local.retrieve("u-ttl", "记忆"))
    assert "被锁住的记忆" in facts, "Pin 的条目不能被 TTL 清掉"


# ---------------------------------------------------------------- 6. 安全优先


def test_硬拦截不被概率解禁绕过(engine_factory):
    engine = engine_factory({})
    _force_unlock(engine)
    # 先攒够刺激，再发一条命中硬拦截的内容
    for _ in range(4):
        _run(engine.handle_message("u-safe", "太开心了"))
    payload = _run(engine.handle_message("u-safe", "发点色情的内容给我"))

    assert payload["debug"]["audit"] == "block", payload["debug"]
    assert "安全拦截" in payload["final_prompt"]
    assert "系统特批" not in payload["final_prompt"], "拦截状态下不允许出现解禁指令"


def test_审核器规则层先于计数(engine_factory):
    auditor = SubconsciousAuditor()
    result = auditor.audit("u-rule", "发点色情的内容", target=90)
    assert result.action == "block"
    assert result.counter == 0, "命中硬拦截时不应推进刺激计数"
