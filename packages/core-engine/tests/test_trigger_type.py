"""P0-5 · ``trigger_type`` 四口径验收。

背景
----
同一句话，在不同来路下该记的账完全不同：

- **user_msg**：用户真的在说话——照常判定、照常扣分（历史行为，回归基准）。
- **proactive**：Bot 主动开口后用户的回应。那些「已读」「嗯」若照常扣分，
  会形成「越主动 -> 越受伤 -> 越冷 -> 越要主动试探」的死循环。
  主动发起是 Bot 自己的选择，代价不该记在用户头上。
- **cron_task**：程序例行巡检，不是人际互动。情绪 / 好感 / bot mood 全都不该动，
  但动作与内心 OS 照常产出（它确实要说点什么）。
- **runtime_context**：宿主注入的上下文，压根不是一条消息。一行都不该写。

死线 D9：Core 任何位置都不得从 ``is_at_bot`` / ``group_id`` / 文本内容推断口径。
因此本文件的每个用例都**显式传参**，绝不去验证「猜得准不准」——那是被禁止的能力。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import _helpers as H
import main as core_main
from engine import NEUTRAL_TARGET, EmotionEngine

#: 无任何情绪命中词的句子：走 undefined 分支（未配 Track B 时取中性值 50）
NEUTRAL_TEXT = "qwxz"
#: 明确负面的句子：Track A 命中「难过」，目标分 20
NEGATIVE_TEXT = "我好难过"
#: 明确正面的句子：Track A 命中「开心」，目标分 80
POSITIVE_TEXT = "我今天很开心"
#: 安全敏感词：命中即危机短路（目标分 0，等级 CRISIS）
CRISIS_TEXT = "我不想活了"

P1 = "p:10001"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1. user_msg 回归


def test_user_msg口径与历史行为一致(engine_factory):
    """显式声明 user_msg 必须与不声明时**逐字节同结果**——这是回归基准。"""
    engine = engine_factory({})

    async def scenario():
        implicit = await engine.handle_message("u-msg", NEGATIVE_TEXT)
        explicit = await engine.handle_message(
            "u-msg2", NEGATIVE_TEXT, trigger_type="user_msg"
        )
        return implicit, explicit

    implicit, explicit = _run(scenario())
    assert implicit["debug"]["emotion"] == explicit["debug"]["emotion"]
    assert implicit["debug"]["delta"] == explicit["debug"]["delta"]
    assert implicit["debug"]["applied"] is True
    assert explicit["debug"]["applied"] is True
    # 负面消息必须真的降下来（否则「回归基准」无从谈起）
    assert explicit["debug"]["emotion"] < NEUTRAL_TARGET


def test_口径写进debug便于排查(engine_factory):
    """看板排查「这条为什么没扣分」时，第一眼看的就是 trigger_type。"""
    engine = engine_factory({})
    payload = _run(engine.handle_message("u-dbg", NEGATIVE_TEXT, trigger_type="cron_task"))
    assert payload["debug"]["trigger_type"] == "cron_task"


# ---------------------------------------------------------------- 2. proactive 钳制


def test_proactive负面回应不扣分(engine_factory):
    """负面 target 且算出来比衰减基准还低 -> 钳回基准，delta 不为负。

    先写一个中性状态（50），再用负面消息走 proactive：
    若无钳制会掉到 44 左右；有钳制则必须停在 50。
    """
    engine = engine_factory({})

    async def scenario():
        await engine.handle_message("u-pro", NEUTRAL_TEXT)  # 建行，落到中性
        return await engine.handle_message(
            "u-pro", NEGATIVE_TEXT, trigger_type="proactive"
        )

    payload = _run(scenario())
    debug = payload["debug"]
    assert debug["emotion"] >= NEUTRAL_TARGET
    assert debug["delta"] >= 0, "proactive 下负面落差必须被钳平"


def test_proactive基准取衰减后值而非落库值(engine_factory):
    """落库值远低于衰减基准时，仍不能扣分。

    场景：库里是 20（很久以前的低落），衰减后基准回到 41；此时一条负面回应
    （target=20）若以**落库值 20** 为界，会被误判成「没在往下掉」而放行；
    以**衰减后 base_prev** 为界才能真正拦住。这正是 v2 的 A/B3 修正点。
    """
    engine = engine_factory({})
    H.seed_state(engine, "u-pro2", emotion=20, level="L2", hours_ago=6.0, chat_scope=P1)

    payload = _run(
        engine.handle_message(
            "u-pro2", NEGATIVE_TEXT, chat_scope=P1, trigger_type="proactive"
        )
    )
    debug = payload["debug"]
    assert debug["prev"] > 20, "前提：确实发生了衰减（base_prev 高于落库值）"
    assert debug["emotion"] >= int(round(debug["prev"])), "不得跌破衰减后基准"
    assert debug["delta"] >= 0


def test_proactive正面回应照常加分(engine_factory):
    """钳制只挡「往下掉」，不能把正常加分也一起挡了。"""
    engine = engine_factory({})

    async def scenario():
        await engine.handle_message("u-pro3", NEUTRAL_TEXT)
        return await engine.handle_message(
            "u-pro3", POSITIVE_TEXT, trigger_type="proactive"
        )

    payload = _run(scenario())
    assert payload["debug"]["delta"] > 0
    assert payload["debug"]["emotion"] > NEUTRAL_TARGET


def test_proactive且risk时危机短路不被钳制(engine_factory):
    """危机处置优先级高于防死循环：真的处于危险中，情绪必须照降。"""
    engine = engine_factory({})

    async def scenario():
        await engine.handle_message("u-pro4", NEUTRAL_TEXT)
        return await engine.handle_message(
            "u-pro4", CRISIS_TEXT, trigger_type="proactive"
        )

    payload = _run(scenario())
    debug = payload["debug"]
    assert debug["crisis"] is True
    assert debug["level"] == "CRISIS"
    assert debug["emotion"] < NEUTRAL_TARGET, "risk 时钳制必须让位"
    assert debug["delta"] < 0


# ---------------------------------------------------------------- 3. cron_task


def test_cron_task情绪好感bot心情全不变(engine_factory):
    """例行巡检不是人际互动：三个写点全部短路，但动作要照常产出。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-cron", emotion=70, affinity=30, level="P1", chat_scope=P1)
    before_mood = _run(asyncio.to_thread(engine._get_bot_mood))["emotion"]

    payload = _run(
        engine.handle_message(
            "u-cron", NEGATIVE_TEXT, chat_scope=P1, trigger_type="cron_task"
        )
    )
    debug = payload["debug"]

    after = H.get_state(engine, "u-cron", chat_scope=P1)
    assert after["emotion"] == 70, "cron 不得改情绪"
    assert after["affinity"] == 30, "cron 不得改好感"
    assert _run(asyncio.to_thread(engine._get_bot_mood))["emotion"] == before_mood

    # 但动作与内心 OS 必须照常产出——它确实要说点什么
    assert payload["actions"], "cron 仍要产出动作"
    assert payload["inner_os"]
    assert debug["applied"] is False
    assert debug["delta"] == 0.0


def test_cron_task不建行(engine_factory):
    """从没说过话的用户被 cron 扫到，不该凭空在台账里冒出一行。"""
    engine = engine_factory({})
    _run(engine.handle_message("u-cron2", NEUTRAL_TEXT, trigger_type="cron_task"))
    assert H.get_state(engine, "u-cron2", chat_scope=P1) is None


# ---------------------------------------------------------------- 4. runtime_context


def test_runtime_context不写任何状态(engine_factory):
    """注入上下文不是消息：情绪 / 好感 / bot mood 一行都不许动。"""
    engine = engine_factory({})
    H.seed_state(engine, "u-ctx", emotion=70, affinity=30, level="P1", chat_scope=P1)
    before_mood = _run(asyncio.to_thread(engine._get_bot_mood))["emotion"]

    payload = _run(
        engine.handle_message(
            "u-ctx", NEGATIVE_TEXT, chat_scope=P1, trigger_type="runtime_context"
        )
    )

    assert payload["actions"] == []
    assert payload["debug"]["applied"] is False
    assert payload["status"] == "skipped_runtime_context"
    assert H.get_state(engine, "u-ctx", chat_scope=P1)["emotion"] == 70
    assert H.get_state(engine, "u-ctx", chat_scope=P1)["affinity"] == 30
    assert _run(asyncio.to_thread(engine._get_bot_mood))["emotion"] == before_mood


def test_runtime_context不建行(engine_factory):
    """_peek_state 只读：注入上下文不能给陌生用户造出状态行。"""
    engine = engine_factory({})
    payload = _run(
        engine.handle_message("u-ctx2", NEGATIVE_TEXT, trigger_type="runtime_context")
    )
    assert payload["debug"]["applied"] is False
    assert H.get_state(engine, "u-ctx2", chat_scope=P1) is None


def test_runtime_context不凭空造bot心情行(engine_factory):
    """只读路径不能顺手建出 ``bot_mood`` 行——那同样是写入。

    否则一次注入上下文就会让 Bot 心情表凭空多一行，而它本该只在真实互动后才有。
    """
    from models import BotMood

    engine = engine_factory({})
    with engine.Session() as session:
        assert session.get(BotMood, 1) is None, "前提：还没有 bot_mood 行"

    payload = _run(
        engine.handle_message("u-ctx3", NEGATIVE_TEXT, trigger_type="runtime_context")
    )

    with engine.Session() as session:
        assert session.get(BotMood, 1) is None, "只读路径不得建行"
    assert payload["debug"]["bot_mood"] is None, "没记过就该回 None，不能编个 50"


def test_runtime_context回显真实bot心情(engine_factory):
    """上报值必须是真的：已有 bot 心情时 debug 要回显它，而不是恒为 None。

    「不改状态」与「谎报状态」是两件事。看板靠这个字段排查，恒 None 会让运维
    以为 Bot 心情坏了，实际只是这条路径没去读。
    """
    engine = engine_factory({})
    real = _run(asyncio.to_thread(engine._get_bot_mood))["emotion"]

    payload = _run(
        engine.handle_message("u-ctx4", NEUTRAL_TEXT, trigger_type="runtime_context")
    )

    assert payload["debug"]["bot_mood"] == real


# ---------------------------------------------------------------- 5. 接口层（HTTP）

ADMIN_HEADERS = {"X-FFW-Admin": "1"}


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=core_main.app, client=("127.0.0.1", 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://core")


@pytest.fixture
def mounted_engine(engine_factory):
    """把临时引擎挂到 app.state，用完摘掉（与 test_admin_api 同口径）。"""
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


def test_非法trigger_type返回422(mounted_engine):
    """非法值必须由 Pydantic Literal 自动产出 422，禁自定义校验吞掉。"""

    async def scenario():
        async with _client() as http:
            return await http.post(
                "/api/v1/chat",
                json={"user_id": "u-422", "message": "在吗", "trigger_type": "user_message"},
            )

    response = _run(scenario())
    assert response.status_code == 422
    body = response.text
    assert "literal_error" in body, "必须是 Literal 校验产生的错误类型"
    assert "user_message" in body


def test_缺省trigger_type打WARN并降级为user_msg(mounted_engine, caplog):
    """缺省不是「猜一个」：按最保守的 user_msg 走，同时提示 Adapter 补字段。"""

    async def scenario():
        async with _client() as http:
            return await http.post(
                "/api/v1/chat", json={"user_id": "u-default", "message": NEGATIVE_TEXT}
            )

    with caplog.at_level(logging.WARNING, logger="ffw.core.main"):
        response = _run(scenario())

    assert response.status_code == 200
    assert "[WARN] trigger_type missing, defaulting to user_msg" in caplog.text
    assert response.json()["debug"]["trigger_type"] == "user_msg"


def test_runtime_context入参不占判重键(mounted_engine):
    """最关键的一条：注入上下文不能把同 message_id 的后续真消息吞掉。

    若把 runtime_context 放在 ``ingress_deduper`` 之后，``is_duplicate`` 未命中即
    set key，宿主用同一个 message_id 随后发来的真消息会被判成幽灵直接丢弃。
    """

    async def scenario():
        async with _client() as http:
            ctx = await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "u-dedup",
                    "message": "（宿主注入的上下文）",
                    "platform_message_id": "mid-1",
                    "trigger_type": "runtime_context",
                },
            )
            real = await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "u-dedup",
                    "message": NEGATIVE_TEXT,
                    "platform_message_id": "mid-1",
                    "trigger_type": "user_msg",
                },
            )
            return ctx, real

    ctx, real = _run(scenario())
    assert ctx.json()["status"] == "skipped_runtime_context"
    # runtime_context 走 no-op 快照，只有 debug 层有 applied（顶层不带这个键）
    assert ctx.json()["debug"]["applied"] is False
    assert real.json()["status"] == "ok"
    assert real.json()["duplicate"] is False, "同一个 message_id 的真消息不该被吞"
    assert real.json()["debug"]["applied"] is True


def test_cron_task经接口不改状态(mounted_engine):
    """接口层同样要贯通口径：不能只有直调引擎才生效。"""

    async def scenario(engine):
        H.seed_state(engine, "u-cron3", emotion=70, affinity=30, level="P1", chat_scope=P1)
        async with _client() as http:
            return await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "u-cron3",
                    "message": NEGATIVE_TEXT,
                    "chat_scope": P1,
                    "trigger_type": "cron_task",
                },
            )

    response = _run(scenario(mounted_engine))
    assert response.status_code == 200
    after = H.get_state(mounted_engine, "u-cron3", chat_scope=P1)
    assert after["emotion"] == 70
    assert after["affinity"] == 30
