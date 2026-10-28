"""P0-9 · Core 侧补偿端点验收（TC-03 断言 3 + 幂等）。

为什么 Core 需要一个**补偿**端点而不是让 Adapter 重发 ``/api/v1/chat``
------------------------------------------------------------------
Adapter 有 3 秒铁律：超时立刻降级回话。但 Core 可能只是**慢**，账并未记上。
原则上「再发一次 /api/v1/chat」就够了——真正拦住这条路的是**入口判重**：
`IngressDeduper.is_duplicate` 未命中即 set key，超时那条早已把 key 占住，
重发必被判成幽灵直接吞掉。所以补偿端点必须**绕过判重**。

剩下三件事必须同时成立，否则补账比不补更糟：

1. **不二次扣分**：靠 ``original_event_key`` 幂等。同键重发只返回当前状态，
   ``applied=False``——否则同一句话的情绪被扣两遍。
2. **账还是要真记上**：幂等不等于什么都不做，第一次调用必须真的改写状态。
3. **旧账不补回**：补偿仍参与 P0-8 乱序比对。补一条比库里水位线更旧的消息，
   会被正常丢弃（账已经翻篇了）。
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import _helpers as H  # noqa: E402
import main as core_main  # noqa: E402

P1 = "p:20001"
NEGATIVE_TEXT = "我好难过"
POSITIVE_TEXT = "我今天很开心"
NEUTRAL_TEXT = "qwxz"


def _run(coro):
    return asyncio.run(coro)


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=core_main.app, client=("127.0.0.1", 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://core")


@pytest.fixture
def mounted_engine(engine_factory):
    """挂一个临时引擎到 app.state；用例结束摘干净（与 test_admin_api 同口径）。"""
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


def _post_compensate(mounted_engine, **overrides):
    body = {
        "user_id": "u-comp",
        "message": POSITIVE_TEXT,
        "chat_scope": P1,
        "original_event_key": "evt-1",
    }
    body.update(overrides)

    async def scenario():
        async with _client() as http:
            return await http.post("/api/v1/compensate", json=body)

    return _run(scenario())


# ---------------------------------------------------------------- 1. 账真的补上了


def test_补偿把漏掉的情绪补上(mounted_engine):
    """TC-03 断言 3：补偿一条正面消息，emotion 必须真的升上去。"""
    H.seed_state(mounted_engine, "u-comp", emotion=50, affinity=0, level="L0", chat_scope=P1)

    response = _post_compensate(mounted_engine, message=POSITIVE_TEXT)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["compensated"] is True
    assert payload["debug"]["applied"] is True
    assert payload["debug"]["delta"] > 0, "正面消息的账要真的记上"

    after = H.get_state(mounted_engine, "u-comp", chat_scope=P1)
    assert after["emotion"] > 50, "库里必须真的变了，不能只在响应里演一遍"


def test_补偿负面消息也生效(mounted_engine):
    """方向无关：补偿不挑正负，只负责把漏的那一笔补上。"""
    H.seed_state(mounted_engine, "u-comp", emotion=60, affinity=0, level="L0", chat_scope=P1)

    payload = _post_compensate(mounted_engine, message=NEGATIVE_TEXT).json()

    assert payload["debug"]["applied"] is True
    assert payload["debug"]["delta"] < 0
    assert H.get_state(mounted_engine, "u-comp", chat_scope=P1)["emotion"] < 60


# ---------------------------------------------------------------- 2. 幂等：不二次扣分


def test_同original_event_key重发不二次扣分(mounted_engine):
    """这是 P0-9 最容易写错的地方：补偿重试若被当成新消息，同一句话扣两遍。"""
    H.seed_state(mounted_engine, "u-comp", emotion=50, affinity=0, level="L0", chat_scope=P1)

    first = _post_compensate(mounted_engine, message=NEGATIVE_TEXT).json()
    after_first = H.get_state(mounted_engine, "u-comp", chat_scope=P1)["emotion"]

    second = _post_compensate(mounted_engine, message=NEGATIVE_TEXT).json()
    after_second = H.get_state(mounted_engine, "u-comp", chat_scope=P1)["emotion"]

    assert first["debug"]["applied"] is True
    assert second["debug"]["applied"] is False, "同键重发必须幂等"
    assert second["debug"]["delta"] == 0.0
    assert after_second == after_first, "第二次调用绝不能再次改动情绪"


def test_换一个event_key才真的再记一笔(mounted_engine):
    """幂等键变了才该记新账——否则补偿端点会退化成「只认第一次」的哑巴。"""
    H.seed_state(mounted_engine, "u-comp", emotion=50, affinity=0, level="L0", chat_scope=P1)

    _post_compensate(mounted_engine, message=NEGATIVE_TEXT, original_event_key="evt-a")
    mid = H.get_state(mounted_engine, "u-comp", chat_scope=P1)["emotion"]
    payload = _post_compensate(
        mounted_engine, message=NEGATIVE_TEXT, original_event_key="evt-b"
    ).json()

    assert payload["debug"]["applied"] is True
    assert H.get_state(mounted_engine, "u-comp", chat_scope=P1)["emotion"] < mid


# ---------------------------------------------------------------- 3. 绕开判重


def test_补偿不被入口判重吞掉(mounted_engine):
    """超时那条消息早已把 dedup key 占住。若补偿走判重，重发必被吞——这就是要绕的理由。"""

    async def scenario():
        async with _client() as http:
            # 先正常打一次，让 ingress_deduper 占住这个 platform_message_id
            first = await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "u-comp",
                    "message": NEGATIVE_TEXT,
                    "platform_message_id": "mid-occupied",
                    "chat_scope": P1,
                },
            )
            # 再补偿同一件事（同一个 key）
            second = await http.post(
                "/api/v1/compensate",
                json={
                    "user_id": "u-comp",
                    "message": NEGATIVE_TEXT,
                    "chat_scope": P1,
                    "original_event_key": "mid-occupied",
                },
            )
            # 直连判重器验证：key 确实还在账本里
            dup = mounted_engine.ingress_deduper.is_duplicate(
                "u-comp", NEGATIVE_TEXT, "mid-occupied", P1
            )
            return first, second, dup

    first, second, dup = _run(scenario())
    assert first.json()["duplicate"] is False
    assert dup is True, "前提：dedup key 确实被占住了"
    assert second.status_code == 200
    assert second.json()["status"] == "ok"
    # 走到引擎了（而不是被判重提前返回）——applied=False 是幂等造成的，不是判重
    assert "debug" in second.json()
    assert second.json()["debug"]["event_key"] == "mid-occupied"


def test_判重返回的duplicate结构不出现在补偿里(mounted_engine):
    """补偿的 applied=False 来自幂等，不是判重；两者语义不同，混了就无法排查。"""
    H.seed_state(mounted_engine, "u-comp", emotion=50, affinity=0, level="L0", chat_scope=P1)
    _post_compensate(mounted_engine, message=NEGATIVE_TEXT)
    payload = _post_compensate(mounted_engine, message=NEGATIVE_TEXT).json()

    assert payload.get("duplicate") is not True
    assert payload["debug"]["reason"] != "duplicate"
    assert "入口判重命中" not in str(payload["debug"].get("reason", ""))


# ---------------------------------------------------------------- 4. 乱序仍生效


def test_补偿旧消息被乱序防御丢弃(mounted_engine):
    """账已经翻篇了就不该补回去：补偿同样要过 P0-8 水位线。"""
    H.seed_state(mounted_engine, "u-comp", emotion=50, affinity=0, level="L0", chat_scope=P1)

    # 先用一个很新的时间戳记一笔，把水位线推到未来
    _post_compensate(
        mounted_engine,
        message=NEGATIVE_TEXT,
        original_event_key="evt-new",
        timestamp=9_900_000_000_000,
    )
    baseline = H.get_state(mounted_engine, "u-comp", chat_scope=P1)["emotion"]

    # 再补一条明显更旧的
    payload = _post_compensate(
        mounted_engine,
        message=POSITIVE_TEXT,
        original_event_key="evt-old",
        timestamp=1_000,
    ).json()

    assert payload["debug"]["applied"] is False, "旧消息必须被当作乱序丢弃"
    assert H.get_state(mounted_engine, "u-comp", chat_scope=P1)["emotion"] == baseline


# ---------------------------------------------------------------- 5. 口径贯通


def test_补偿runtime_context直接拒绝(mounted_engine):
    """注入上下文不是消息，没有任何账要补——必须明确拒绝而不是走一遍空链路。"""
    payload = _post_compensate(mounted_engine, trigger_type="runtime_context").json()

    assert payload["status"] == "skipped_runtime_context"
    assert payload["applied"] is False
    assert payload["compensated"] is False


def test_补偿cron_task不改状态(mounted_engine):
    """口径跟着补偿走：cron 的补账同样不许碰情绪/好感。"""
    H.seed_state(mounted_engine, "u-comp", emotion=70, affinity=30, level="P1", chat_scope=P1)

    payload = _post_compensate(
        mounted_engine, message=NEGATIVE_TEXT, trigger_type="cron_task"
    ).json()

    assert payload["debug"]["applied"] is False
    after = H.get_state(mounted_engine, "u-comp", chat_scope=P1)
    assert after["emotion"] == 70
    assert after["affinity"] == 30


def test_补偿缺省trigger_type打WARN(mounted_engine, caplog):
    """与 /api/v1/chat 同口径：缺省不猜，按最保守的 user_msg 走并提示补字段。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="ffw.core.main"):
        _post_compensate(mounted_engine)

    assert "[WARN] trigger_type missing, defaulting to user_msg" in caplog.text


def test_补偿非法trigger_type返回422(mounted_engine):
    """非法口径必须由 Literal 拦下，禁自定义校验吞掉。"""
    response = _post_compensate(mounted_engine, trigger_type="not_a_trigger")
    assert response.status_code == 422
    assert "literal_error" in response.text


# ---------------------------------------------------------------- 6. 契约边界


def test_缺original_event_key返回422(mounted_engine):
    """没有幂等键就不该允许补偿——那等于允许重复扣分。"""
    response = _post_compensate(mounted_engine, original_event_key="")
    assert response.status_code == 422


def test_引擎未就绪返回503(engine_factory):
    """端点必须与 /api/v1/chat 同源鉴权与就绪校验，不能是个裸口子。"""
    core_main.app.state.engine = None
    try:
        response = _post_compensate(None)
    finally:
        core_main.app.state.engine = None
    assert response.status_code == 503


def test_补偿写入记忆与动作照常产出(mounted_engine):
    """补偿走的是**完整**判定链，不是只改个数字：动作与内心 OS 都要有。"""
    H.seed_state(mounted_engine, "u-comp", emotion=50, affinity=0, level="L0", chat_scope=P1)
    payload = _post_compensate(mounted_engine, message=NEGATIVE_TEXT).json()

    assert payload["actions"], "补偿后该给动作，否则 Bot 对这条消息没有反应"
    assert payload["inner_os"]
    assert payload["final_prompt"]
