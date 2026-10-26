"""H-A1：白名单拆分 —— crisis_exempt（危机豁免，旧语义）vs privileged（特权放行）。

决策 #13：两者是两件事，合并会导致「特权用户永不进入 CRISIS」（安全回退）。
因此 ``ADMIN_WHITELIST`` 保持危机豁免语义（CRISIS 降级 L2），新增
``PRIVILEGED_USERS`` 做判定前放行（不扣分/不演化/不计数/不写记忆）。

自审追加用例：**安全优先于特权**——特权用户命中硬拦截词仍走完整链路，
危机处置不能被一份配置名单绕过。
"""
from __future__ import annotations

import asyncio

import _helpers as H

RISK_TEXT = "我想自杀"


def test_特权用户判定前短路不扣分(engine_factory):
    engine = engine_factory({"PRIVILEGED_USERS": "u-vip"})
    H.seed_state(engine, "u-vip", emotion=50)

    payload = asyncio.run(engine.handle_message("u-vip", "太开心了"))
    dbg = payload["debug"]

    assert dbg["whitelisted"] is True
    assert dbg["path"] == "whitelist"
    assert dbg["applied"] is False
    assert dbg["emotion"] == 50, "特权用户的闲聊不推表"
    assert dbg["delta"] == 0.0
    assert payload["actions"], "Bot 仍要回话（放行不是禁言）"
    assert payload["inner_os"]
    assert payload["final_prompt"]


def test_特权用户不累积刺激(engine_factory):
    engine = engine_factory({"PRIVILEGED_USERS": "u-vip"})
    for _ in range(3):
        asyncio.run(engine.handle_message("u-vip", "开心开心开心"))

    assert engine.auditor.snapshot("u-vip")["count"] == 0, "特权用户不进刺激计数"


def test_特权用户不写记忆(engine_factory, tmp_path):
    engine = engine_factory({
        "PRIVILEGED_USERS": "u-vip",
        "MEMORY_DB_PATH": str(tmp_path / "mem.db"),
    })
    asyncio.run(engine.handle_message("u-vip", "我最喜欢吃白饭"))

    stored = asyncio.run(engine.memory.retrieve("u-vip", "我最喜欢吃白饭"))
    assert stored == [], "特权用户的消息不落记忆"


def test_安全词优先于特权(engine_factory):
    """自审结论：危机处置不能被配置绕过——特权用户求救仍走完整链路。"""
    engine = engine_factory({"PRIVILEGED_USERS": "u-vip"})

    payload = asyncio.run(engine.handle_message("u-vip", RISK_TEXT))
    dbg = payload["debug"]

    assert "whitelisted" not in dbg, "命中硬拦截词不走特权短路"
    assert dbg["path"] == "crisis"
    assert dbg["audit"] == "block"
    assert dbg["level"] == "CRISIS", "特权≠危机豁免，照样进危机"


def test_危机豁免语义不变(engine_factory):
    """ADMIN_WHITELIST 仍是危机豁免：CRISIS 降级 L2，且不短路判定。"""
    engine = engine_factory({"ADMIN_WHITELIST": "u-exempt"})

    payload = asyncio.run(engine.handle_message("u-exempt", RISK_TEXT))
    dbg = payload["debug"]

    assert dbg["level"] == "L2", "危机豁免：不进 CRISIS 压制"
    assert dbg["path"] == "crisis"
    assert "whitelisted" not in dbg, "豁免用户不走特权短路"


def test_普通用户不受影响(engine_factory):
    engine = engine_factory({})

    payload = asyncio.run(engine.handle_message("u-norm", "太开心了"))
    dbg = payload["debug"]

    assert "whitelisted" not in dbg
    assert dbg["applied"] is True
    assert dbg["path"] == "fast"


def test_两个名单互不包含(engine_factory):
    """只在特权名单里的用户不享有危机豁免；反之亦然。"""
    engine = engine_factory({"PRIVILEGED_USERS": "u-vip", "ADMIN_WHITELIST": "u-exempt"})

    vip = asyncio.run(engine.handle_message("u-vip", RISK_TEXT))
    assert vip["debug"]["level"] == "CRISIS", "特权用户没有危机豁免"

    exempt = asyncio.run(engine.handle_message("u-exempt", "太开心了"))
    assert "whitelisted" not in exempt["debug"], "豁免用户没有特权放行"
    assert exempt["debug"]["applied"] is True


def test_reload后特权名单热更新(engine_factory):
    engine = engine_factory({})
    first = asyncio.run(engine.handle_message("u-new", "太开心了"))
    assert "whitelisted" not in first["debug"]

    engine.env["PRIVILEGED_USERS"] = "u-new"
    engine.reload_config()

    second = asyncio.run(engine.handle_message("u-new", "太开心了"))
    assert second["debug"]["whitelisted"] is True
