"""阶段五 · 上真机前的三项自检（全离线，用 Mock LLM）。

跑法（仓库根目录）：
    venv/Scripts/python.exe packages/core-engine/scripts/phase5_verify.py

自检内容
--------
1. **Token 看板**：普通对话 + Track B 语义判定，都能按模块分类记账，
   并能在 ``GET /api/v1/admin/token_stats`` 里看到；
2. **记忆 TTL 兜底**：说「我最喜欢吃白饭」→ 记得；把时间戳改到 8 天前
   → 再说一次，不再注入这条记忆（TTL 生效）；
3. **演化 OOC 拦截**：人设卡「冷酷无情的杀手」+ 一堆阳光记忆，
   生成的追加词被 OOC 探针驳回，人设卡一字不动。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sqlite3
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# PR1 安全基线之后必配：本脚本的客户端 base_url 是 http://core（Host: core），
# 不把它加进 Host 白名单会被 security 中间件 400 挡掉（表现为 KeyError: 'debug'）。
os.environ.setdefault("CORE_EXTRA_HOSTS", "core")
# PR1 之后管理接口 GET 也要 X-FFW-Admin 头，否则 403（表现为 KeyError: 'modules'）。
ADMIN_HEADERS = {"X-FFW-Admin": "1"}

import httpx  # noqa: E402

LINE = "=" * 70


def _section(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core")


# ---------------------------------------------------------------- 1. Token 看板


async def check_token_dashboard(tmp: str) -> None:
    _section("① Token 看板：普通对话 vs Track B 语义判定")

    from engine import EmotionEngine, TrackB
    from main import app

    engine = EmotionEngine(
        db_path=os.path.join(tmp, "app.db"),
        cache_dir=os.path.join(tmp, "cache"),
        env={
            "MEMORY_DB_PATH": os.path.join(tmp, "memory.db"),
            "LLM_API_BASE": "https://api.deepseek.com/v1",
            "LLM_API_KEY": "sk-mock-key",
            "LLM_MODEL": "deepseek-chat",
        },
    )
    app.state.engine = engine

    # 用替身顶掉真实 HTTP：返回合法 JSON + usage
    async def fake_request(self, body, headers):
        return {
            "choices": [{"message": {"content": '{"emotion_target": 22}'}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 8},
        }

    original = TrackB._request
    TrackB._request = fake_request
    try:
        async with await _client(app) as http:
            # 普通对话：走 Track A，不花 token
            r1 = await http.post(
                "/api/v1/chat",
                json={"user_id": "10001", "message": "今天真开心", "platform_message_id": "m-a"},
            )
            d1 = r1.json()["debug"]
            print(f"   Track A 对话  -> path={d1['path']} target={d1['target']}")

            # 带转折词：触发 Track B，花 token
            r2 = await http.post(
                "/api/v1/chat",
                json={"user_id": "10001", "message": "其实我挺好的", "platform_message_id": "m-b"},
            )
            d2 = r2.json()["debug"]
            print(f"   Track B 对话  -> path={d2['path']} target={d2['target']}")

            stats = (
                await http.get("/api/v1/admin/token_stats", headers=ADMIN_HEADERS)
            ).json()

        print("\n   /api/v1/admin/token_stats:")
        for module, item in stats["modules"].items():
            print(f"     {module:<14} calls={item['calls']} "
                  f"in={item['prompt_tokens']} out={item['completion_tokens']} "
                  f"total={item['total_tokens']}")
        print(f"     总计: {stats['total']}")

        ok = (
            "MOD_TRACK_B" in stats["modules"]
            and stats["modules"]["MOD_TRACK_B"]["calls"] == 1
            and stats["modules"]["MOD_TRACK_B"]["prompt_tokens"] == 120
        )
        print(f"\n   判定: {'✅ Track B 消耗已分类入账' if ok else '❌ 记账异常'}")
    finally:
        TrackB._request = original
        engine.cache.close()
        engine.db_engine.dispose()


# ---------------------------------------------------------------- 2. 记忆 TTL


async def check_memory_ttl(tmp: str) -> None:
    _section("② 记忆 TTL 兜底：8 天后应该「忘记」")

    from engine import EmotionEngine

    memory_db = os.path.join(tmp, "ttl_memory.db")
    engine = EmotionEngine(
        db_path=os.path.join(tmp, "ttl.db"),
        cache_dir=os.path.join(tmp, "ttl_cache"),
        env={"MEMORY_DB_PATH": memory_db, "MEMORY_TTL_DAYS": "7"},
    )

    first = await engine.handle_message("10001", "我最喜欢吃白饭了")
    print(f"   第 1 次对话 -> 注入记忆条数 = {first['debug']['memories']}")

    second = await engine.handle_message("10001", "我最喜欢吃什么")
    print(f"   第 2 次对话 -> 注入记忆条数 = {second['debug']['memories']}（应该 > 0）")
    hit_now = second["debug"]["memories"] > 0
    prompt_has_fact = "白饭" in second["final_prompt"]
    print(f"   final_prompt 里出现「白饭」: {prompt_has_fact}")

    # 手动把时间戳改到 8 天前（模拟时间流逝）
    past = (datetime.datetime.now() - datetime.timedelta(days=8)).isoformat(sep=" ", timespec="seconds")
    conn = sqlite3.connect(memory_db)
    conn.execute("UPDATE memories SET created_at=? WHERE user_id='10001'", (past,))
    conn.commit()
    conn.close()
    print(f"   已把 created_at 改成 {past}（8 天前）")

    third = await engine.handle_message("10001", "我最喜欢吃什么")
    print(f"   第 3 次对话 -> 注入记忆条数 = {third['debug']['memories']}（应该 = 0）")

    ok = hit_now and third["debug"]["memories"] == 0
    print(f"\n   判定: {'✅ TTL 生效，Bot 忘了这件事' if ok else '❌ TTL 未生效'}")
    engine.cache.close()
    engine.db_engine.dispose()


# ---------------------------------------------------------------- 3. OOC 拦截


async def check_ooc(tmp: str) -> None:
    _section("③ 演化 OOC 拦截：杀手不该变成阳光大男孩")

    from evolution import EvolutionEngine
    from memory_router import LocalSQLiteProvider
    from models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = os.path.join(tmp, "evo.db")
    sa_engine = create_engine("sqlite:///" + db_path, future=True)
    Base.metadata.create_all(sa_engine)
    factory = sessionmaker(bind=sa_engine, expire_on_commit=False, future=True)

    persona_path = os.path.join(tmp, "persona.txt")
    with open(persona_path, "w", encoding="utf-8") as handle:
        handle.write("你是冷酷无情的杀手，说话极简，从不笑，对任何人都保持距离。\n")

    backups = os.path.join(tmp, "backups")
    os.makedirs(backups, exist_ok=True)
    old_day = datetime.date.today() - datetime.timedelta(days=7)
    with open(os.path.join(backups, "persona_" + old_day.strftime("%Y%m%d") + ".txt"),
              "w", encoding="utf-8") as handle:
        handle.write("你是冷酷无情的杀手。\n")

    memory = LocalSQLiteProvider(os.path.join(tmp, "evo_memory.db"))
    for fact in ["用户说今天天气真好", "用户分享了开心的事", "用户笑得停不下来"]:
        await memory.store("10001", fact)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        system = body["messages"][0]["content"]
        if "一致性审查员" in system:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(
                    {"conflict": True, "reason": "杀手不应变得阳光开朗、见谁都笑"})}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20},
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "变得阳光开朗，喜欢和用户一起大笑。"}}],
            "usage": {"prompt_tokens": 150, "completion_tokens": 30},
        })

    evolution = EvolutionEngine(
        db_path=db_path,
        persona_path=persona_path,
        backups_dir=backups,
        memory=memory,
        env={"LLM_API_BASE": "https://api.deepseek.com/v1",
             "LLM_API_KEY": "sk-mock", "LLM_MODEL": "deepseek-chat"},
        session_factory=factory,
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime.datetime.combine(datetime.date.today(), datetime.time(3, 0)),
    )

    before = evolution.read_persona()
    result = await evolution.evolve_user("10001", datetime.date.today())
    after = evolution.read_persona()

    print(f"   生成的追加词 : {result.get('append')}")
    print(f"   OOC 探针     : passed={result['ooc']['passed']} reason={result['ooc']['reason']}")
    print(f"   写入结果     : appended={result['appended']} reason={result['reason']}")
    print(f"   人设卡是否被改: {before != after}")

    ok = (result["appended"] is False and before == after
          and result["reason"] == "ooc_rejected")
    print(f"\n   判定: {'✅ OOC 拦截生效，人设卡未被污染' if ok else '❌ 拦截失效'}")
    evolution.close()
    sa_engine.dispose()


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="ffw_phase5_")
    print(f"临时目录：{tmp}")
    await check_token_dashboard(tmp)
    await check_memory_ttl(tmp)
    await check_ooc(tmp)
    print("\n" + LINE)
    print("三项自检结束。")
    print(LINE)


if __name__ == "__main__":
    asyncio.run(main())
