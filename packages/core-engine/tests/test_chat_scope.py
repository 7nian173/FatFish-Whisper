"""P0 · ``chat_scope`` 分房验收（I-26：锚点全局、情绪分房、理由分房）。

本文件是 task-4 的新增验收，覆盖：

1. **WAL**：SQLite 连接建立后 ``journal_mode`` 必须是 ``wal``（引擎库 + 记忆库各一条）；
2. **情绪分房**：同一个 ``user_id`` 在 ``g:111`` 与 ``g:222`` 各自独立的情绪行，
   一边推高不影响另一边；老库 ``""`` 分区既不参与也不被猜成私聊；
3. **理由分房**：``auditor._stimuli`` 按 ``(user_id, chat_scope)`` 计数，
   A 群连夸三次不会让 B 群第一句就命中解禁；
4. **记忆分房**：``g:A`` 写的记忆在 ``g:B`` 检索不到（跨群隐私泄漏修复点）；
5. **锚点全局**：好感度与 BotMood 跨分区共享——这是 I-26 的另一半，
   分房分过头同样是缺陷；
6. **派生点唯一**：``main.derive_chat_scope`` 的四种输入（显式 / 群号 /
   空串群号 / None），空串不得推成 ``"g:"``；
7. **老库迁移幂等**：``migrate_chat_scope`` 连续跑两次，第二次是空操作，
   且好感度在拆表后仍然完整。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import _helpers as H

G1 = "g:111"
G2 = "g:222"
P1 = "p:10001"


def _run(coro):
    return asyncio.run(coro)


def _journal_mode(db_path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()
    finally:
        conn.close()


# ---------------------------------------------------------------- 1. WAL


def test_引擎库开启WAL(engine_factory, tmp_path):
    engine = engine_factory({"MEMORY_DB_PATH": str(tmp_path / "mem_wal.db")})
    # 触发器：让引擎真正取一条连接（建表时已取过，这里再确保一次）
    asyncio.run(engine.update_emotion("u-wal", 60))
    assert _journal_mode(engine.db_path) == "wal", "引擎库必须处于 WAL 模式"


def test_记忆库开启WAL(engine_factory, tmp_path):
    mem_path = tmp_path / "mem_wal2.db"
    engine = engine_factory({"MEMORY_DB_PATH": str(mem_path)})
    asyncio.run(engine.memory.store("u-wal2", "我最喜欢吃白饭", chat_scope=P1))
    assert _journal_mode(mem_path) == "wal", "记忆库也必须处于 WAL 模式"


def test_引擎连接启用busy_timeout与foreign_keys(engine_factory):
    engine = engine_factory({})
    with engine.db_engine.connect() as conn:
        from sqlalchemy import text

        busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert int(busy) == 5000, "busy_timeout 必须是 5000ms"
    assert int(fk) == 1, "foreign_keys 必须显式打开"


# ---------------------------------------------------------------- 2. 情绪分房


def test_同一用户两个群情绪互相隔离(engine_factory):
    """TC-05 核心断言：G1 推高情绪，G2 必须保持基准值。"""
    engine = engine_factory({})
    H.seed_state(engine, "u1", emotion=50, chat_scope=G1)
    H.seed_state(engine, "u1", emotion=50, chat_scope=G2)

    _run(engine.update_emotion("u1", 90, event_key="e-g1", chat_scope=G1))

    assert H.get_state(engine, "u1", G1)["emotion"] > 50, "G1 应被推高"
    assert H.get_state(engine, "u1", G2)["emotion"] == 50, "G2 不得被 G1 影响"


def test_两个群各自独立演化(engine_factory):
    engine = engine_factory({})
    H.seed_state(engine, "u2", emotion=50, chat_scope=G1)
    H.seed_state(engine, "u2", emotion=50, chat_scope=G2)

    for _ in range(4):
        _run(engine.update_emotion("u2", 90, chat_scope=G1))
    for _ in range(4):
        _run(engine.update_emotion("u2", 10, chat_scope=G2))

    high = H.get_state(engine, "u2", G1)["emotion"]
    low = H.get_state(engine, "u2", G2)["emotion"]
    assert high > 50 > low, "两个群必须能各自往相反方向走，实际 G1={0} G2={1}".format(
        high, low
    )


def test_分房后行数按分区增长(engine_factory):
    engine = engine_factory({})
    _run(engine.update_emotion("u3", 80, chat_scope=G1))
    _run(engine.update_emotion("u3", 80, chat_scope=G2))
    _run(engine.update_emotion("u3", 80, chat_scope=P1))

    assert H.count_users(engine, "u3") == 3, "三个分区应各有独立一行"


def test_老库空串分区不被猜成私聊(engine_factory):
    """老数据 ``chat_scope=""`` 只作为历史分区，不与 ``p:<user_id>`` 混读。"""
    engine = engine_factory({})
    H.seed_state(engine, "u4", emotion=88, chat_scope="")

    assert H.get_state(engine, "u4", "")["emotion"] == 88, "老分区数据必须原样可读"
    assert H.get_state(engine, "u4", P1) is None, (
        "老数据不得被静默当成 p:<user_id>，否则私聊分区会凭空多出历史情绪"
    )


# ---------------------------------------------------------------- 3. 理由分房


def test_刺激计数按分区隔离(engine_factory):
    """TC-05：G1 连说三次 -> _stimuli[G1]==3；G2 说一次 -> _stimuli[G2]==1。

    ``allow_unlock=False``：解禁是**概率性**的（到次数后按 traits 掷骰），
    掷中就 reset 清零。计数用例必须关掉解禁这条随机支路，否则断言会随机红。
    """
    engine = engine_factory({})
    aud = engine.auditor

    for _ in range(3):
        aud.audit("u5", "太开心了", target=90, chat_scope=G1, allow_unlock=False)
    aud.audit("u5", "太开心了", target=90, chat_scope=G2, allow_unlock=False)

    assert aud.snapshot("u5", G1)["count"] == 3
    assert aud.snapshot("u5", G2)["count"] == 1, (
        "G2 的计数必须是 1：若为 4 说明 A 群的连续刺激串到了 B 群"
    )
    # 键必须是 (user_id, chat_scope) 元组——按 user_id 单键就做不到分房
    assert aud._stimuli[("u5", G1)].count == 3
    assert aud._stimuli[("u5", G2)].count == 1


def test_一个分区解禁不影响另一个分区(engine_factory):
    """G1 攒满次数后，G2 的第一句必须仍是 count=1 / 未达门槛。"""
    engine = engine_factory({})
    aud = engine.auditor
    for _ in range(3):
        aud.audit("u6", "太开心了", target=90, chat_scope=G1, allow_unlock=False)

    result = aud.audit("u6", "太开心了", target=90, chat_scope=G2, allow_unlock=False)
    assert result.action == "pass", "B 群不能因为 A 群连续刺激就解禁人设"
    assert result.counter == 1, (
        "G2 的计数器必须从 1 开始，实际 {0}（>1 说明跨分区累加了）".format(
            result.counter
        )
    )
    assert aud.snapshot("u6", G2)["count"] == 1


def test_reset只清指定分区(engine_factory):
    engine = engine_factory({})
    aud = engine.auditor
    aud.audit("u7", "太开心了", target=90, chat_scope=G1)
    aud.audit("u7", "太开心了", target=90, chat_scope=G2)

    aud.reset("u7", G1)
    assert aud.snapshot("u7", G1)["count"] == 0
    assert aud.snapshot("u7", G2)["count"] == 1, "不该误删另一个分区"

    aud.reset("u7")
    assert aud.snapshot("u7", G2)["count"] == 0, "单参数 reset 仍清该用户所有分区"


def test_习惯化窗口按分区隔离(engine_factory):
    """在 G1 连夸三次后的 alpha 降档，不能影响 G2 的第一次。"""
    engine = engine_factory({})
    alphas = []
    for _ in range(3):
        alpha, _count = engine._habituation_alpha("u8", 90, chat_scope=G1)
        alphas.append(alpha)
    assert alphas[-1] < alphas[0], "G1 内部应发生习惯化降档"

    fresh, _count = engine._habituation_alpha("u8", 90, chat_scope=G2)
    assert fresh == pytest.approx(alphas[0]), "G2 的第一次必须是满 alpha"


# ---------------------------------------------------------------- 4. 记忆分房


def test_记忆按分区物理隔离(engine_factory, tmp_path):
    """TC-05：G1 说的内容在 G2 检索不到。"""
    engine = engine_factory({"MEMORY_DB_PATH": str(tmp_path / "mem_iso.db")})

    _run(engine.memory.store("u9", "我下周三要考试", 0.9, chat_scope=G1))

    assert _run(engine.memory.retrieve("u9", "考试", chat_scope=G1)) == [
        "我下周三要考试"
    ]
    assert _run(engine.memory.retrieve("u9", "考试", chat_scope=G2)) == [], (
        "B 群绝不能检索到 A 群的记忆（跨群隐私泄漏）"
    )
    assert _run(engine.memory.retrieve("u9", "考试", chat_scope=P1)) == []


def test_同一句话在不同分区各存一条(engine_factory, tmp_path):
    engine = engine_factory({"MEMORY_DB_PATH": str(tmp_path / "mem_dup.db")})
    local = engine.memory.local

    _run(engine.memory.store("u10", "我最喜欢吃白饭", 0.1, chat_scope=G1))
    _run(engine.memory.store("u10", "我最喜欢吃白饭", 0.1, chat_scope=G2))

    assert local.count("u10", chat_scope=G1) == 1
    assert local.count("u10", chat_scope=G2) == 1
    assert local.count("u10") == 2, "不带 scope 的 count 是跨分区汇总（看板视角）"


def test_记忆LRU淘汰按分区计数(engine_factory, tmp_path):
    """上限是**每分区** 200 条：一个分区灌满不应把另一个分区的条目挤掉。"""
    engine = engine_factory({"MEMORY_DB_PATH": str(tmp_path / "mem_lru.db")})
    local = engine.memory.local
    local.max_items = 3

    for index in range(5):
        _run(engine.memory.store("u11", "G1 的第 {0} 条".format(index), 0.1, chat_scope=G1))
    _run(engine.memory.store("u11", "G2 唯一一条", 0.1, chat_scope=G2))

    assert local.count("u11", chat_scope=G1) == 3, "G1 超上限应被裁到 3"
    assert local.count("u11", chat_scope=G2) == 1, "G2 不该被 G1 的淘汰波及"


def test_完整链路不跨群注入记忆(engine_factory, tmp_path):
    """走 handle_message：G1 说的内容不能在 G2 的 debug.memories 里出现。"""
    engine = engine_factory({"MEMORY_DB_PATH": str(tmp_path / "mem_flow.db")})

    _run(engine.handle_message("u12", "我下周三要考试", chat_scope=G1))
    payload = _run(engine.handle_message("u12", "考试", chat_scope=G2))

    assert payload["debug"]["chat_scope"] == G2, "debug 必须留痕分区"
    assert payload["debug"]["memories"] == 0, (
        "G2 的分区里没有历史记忆，注入条数必须是 0"
    )
    # 反向确认：同一句话在 G1 检索得到，说明 0 条确实是分区隔离而不是没存进去
    assert _run(engine.memory.retrieve("u12", "考试", chat_scope=G1)) == [
        "我下周三要考试"
    ]


# ---------------------------------------------------------------- 5. 锚点全局


def test_好感度跨分区共享(engine_factory):
    """I-26 的另一半：好感度**不分房**。

    走 ``process`` 而不是 ``update_emotion``：好感度微调（``_bump_affinity``）
    是主链路的步骤 4，只改情绪不会碰它。
    """
    engine = engine_factory({})
    H.seed_state(engine, "u13", emotion=50, affinity=7, chat_scope=G1)
    H.seed_state(engine, "u13", emotion=50, affinity=7, chat_scope=G2)

    for _ in range(4):
        _run(engine.process("u13", "哈哈哈太好了", chat_scope=G1))

    a1 = H.get_state(engine, "u13", G1)["affinity"]
    a2 = H.get_state(engine, "u13", G2)["affinity"]
    assert a1 == a2, "好感度必须全局一致，实际 G1={0} G2={1}".format(a1, a2)
    assert a1 > 7, "G1 的持续正反馈应推高全局好感度，实际 {0}".format(a1)

    # G2 的情绪完全没动，但读到的好感度已经是 G1 推上去的值
    assert H.get_state(engine, "u13", G2)["emotion"] == 50


def test_好感度读写不接受chat_scope(engine_factory):
    """签名层面杜绝分房：多传一个 chat_scope 会直接 TypeError。"""
    engine = engine_factory({})
    with pytest.raises(TypeError):
        engine._read_affinity("u14", G1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        engine._write_affinity("u14", 5, G1)  # type: ignore[call-arg]


def test_BotMood全局单行(engine_factory):
    engine = engine_factory({})
    H.seed_state(engine, "u15", emotion=50, chat_scope=G1)
    H.seed_state(engine, "u15", emotion=50, chat_scope=G2)

    _run(engine.process("u15", "哈哈哈太好了", chat_scope=G1))
    mood = engine._get_bot_mood()
    assert mood["emotion"] > 50, "Bot 的心情是全局慢变量，应被 G1 推动"

    # 另一个群再走一轮，读到的还是同一份心情（不是新建一行）
    _run(engine.process("u15", "哈哈哈太好了", chat_scope=G2))
    from models import BotMood
    from sqlalchemy import func, select

    with engine.Session() as session:
        rows = session.execute(select(func.count()).select_from(BotMood)).scalar_one()
    assert int(rows) == 1, "BotMood 必须始终只有一行"


# ---------------------------------------------------------------- 6. 派生点唯一


def test_派生规则四种输入():
    from main import derive_chat_scope

    assert derive_chat_scope("10001") == "p:10001"
    assert derive_chat_scope("10001", "88888") == "g:88888"
    assert derive_chat_scope("10001", "88888", "g:custom") == "g:custom", "显式优先"
    assert derive_chat_scope("10001", "") == "p:10001", "空串群号不得推成 'g:'"
    assert derive_chat_scope("10001", "   ") == "p:10001", "纯空白同理"
    assert derive_chat_scope("10001", None, "  ") == "p:10001", "显式空串回落派生"


def test_接口层派生并透传(engine_factory, monkeypatch):
    """``/api/v1/chat`` 必须自己派生一次并透传给引擎，响应里回显。"""
    import httpx

    from main import app

    engine = engine_factory({})
    monkeypatch.setattr(app.state, "engine", engine, raising=False)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core"
    )

    async def scenario():
        async with client as http:
            grouped = await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "u16",
                    "message": "今天好累啊",
                    "group_id": "88888",
                    "platform_message_id": "m-g",
                },
            )
            private = await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "u16",
                    "message": "今天好累啊",
                    "platform_message_id": "m-p",
                },
            )
            return grouped, private

    grouped, private = _run(scenario())
    assert grouped.json()["chat_scope"] == "g:88888"
    assert private.json()["chat_scope"] == "p:u16"

    # 两个分区各一行，情绪互不影响
    assert H.count_users(engine, "u16") == 2


def test_同人跨群同文本不算重复(engine_factory, monkeypatch):
    """入口判重键必须含分区，否则同人跨群说同一句话会被吞掉第二条。

    回归背景：``IngressDeduper._key`` 已支持 ``chat_scope``，但 ``main.py`` 的
    判重调用曾漏传，导致该修复是死代码。本用例**故意不传 platform_message_id**——
    传了就走 ``ingress:mid:`` 分支，天然不冲突，测不出缺陷（旧用例正是这样绕过的）。
    """
    import httpx

    from main import app

    engine = engine_factory({})
    monkeypatch.setattr(app.state, "engine", engine, raising=False)

    def post(payload):
        # httpx.AsyncClient 关闭后不能重开，每次请求新建一个（ASGITransport 无网络开销）
        async def go():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://core"
            ) as http:
                return await http.post("/api/v1/chat", json=payload)

        return _run(go())

    first = post({"user_id": "u20", "message": "在吗", "group_id": "111"})
    second = post({"user_id": "u20", "message": "在吗", "group_id": "222"})
    assert first.json()["status"] == "ok"
    assert second.json()["status"] == "ok", "不同分区的同文本不能被判重吞掉"
    assert second.json()["chat_scope"] == "g:222"

    # 同一分区内重复推送仍必须判重（别把判重彻底关掉）
    again = post({"user_id": "u20", "message": "在吗", "group_id": "111"})
    assert again.json()["status"] == "duplicate", "同分区重复推送仍要判重"


# ---------------------------------------------------------------- 7. 老库迁移


def test_老库迁移幂等且好感度不丢(tmp_path):
    """模拟分房之前的老库：user_states 带 affinity_raw、无 chat_scope。"""
    from sqlalchemy import create_engine, text

    from models import Base, UserAffinity, UserState, migrate_chat_scope

    db_path = tmp_path / "legacy.db"
    bind = create_engine("sqlite:///" + str(db_path), future=True)
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE user_states ("
                " user_id VARCHAR PRIMARY KEY,"
                " emotion_current INTEGER NOT NULL DEFAULT 50,"
                " emotion_level VARCHAR NOT NULL DEFAULT 'L0',"
                " affinity_raw INTEGER NOT NULL DEFAULT 0,"
                " last_update DATETIME NOT NULL,"
                " last_event_key VARCHAR)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO user_states"
                " (user_id, emotion_current, emotion_level, affinity_raw,"
                " last_update, last_event_key)"
                " VALUES ('old-user', 73, 'L1', 42, '2026-01-01 00:00:00', 'evt-old')"
            )
        )

    # 建出全局好感度表（真实启动顺序：create_all 先跑，再跑迁移）
    Base.metadata.create_all(bind)
    steps = migrate_chat_scope(bind)
    assert steps, "老库必须真的执行迁移"

    with bind.begin() as conn:
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(user_states)")).fetchall()
        }
        pk = [
            row[1]
            for row in conn.execute(text("PRAGMA table_info(user_states)")).fetchall()
            if row[5]
        ]
    assert "chat_scope" in columns and "affinity_raw" not in columns
    assert pk == ["user_id", "chat_scope"], "主键必须改成复合主键"

    # 数据搬迁：情绪留空串分区，好感度进全局表
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=bind, expire_on_commit=False, future=True)
    with Session() as session:
        row = session.get(UserState, ("old-user", ""))
        assert row is not None, "老数据必须落在 '' 分区"
        assert int(row.emotion_current) == 73
        assert row.emotion_level == "L1"
        assert row.last_event_key == "evt-old"

        anchor = session.get(UserAffinity, "old-user")
        assert anchor is not None and int(anchor.affinity_raw) == 42, "好感度不得丢"

    # 幂等：第二次是空操作，不重复搬、不报错
    assert migrate_chat_scope(bind) == [], "重复迁移必须是空操作"
    with Session() as session:
        assert int(session.get(UserAffinity, "old-user").affinity_raw) == 42
    bind.dispose()


def test_新库迁移是空操作(engine_factory):
    engine = engine_factory({})
    assert engine.migrations == [] or all(
        "user_states" not in item for item in engine.migrations
    ), "新建库不需要重建 user_states，实际 {0}".format(engine.migrations)
