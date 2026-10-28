"""判据实跑证据（判据 2/3/7 的原始输出，供计划员逐条核对）。

不参与 pytest 采集（文件名不以 test_ 开头）。跑法：
    E:/sandbox/venv-p0/Scripts/python.exe packages/core-engine/tests/criteria_probe.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 与 conftest.py:27 同口径：security 中间件只放行回环 Host，测试主机须显式加白，
# 否则所有请求都会先被 400「Host 不在允许列表」挡掉，根本走不到本单的判据。
os.environ["CORE_EXTRA_HOSTS"] = "testserver,core"

import httpx  # noqa: E402

import main as core_main  # noqa: E402
from engine import EmotionEngine  # noqa: E402

import tempfile  # noqa: E402

TMP = tempfile.mkdtemp(prefix="criteria-")


def _client():
    transport = httpx.ASGITransport(app=core_main.app, client=("127.0.0.1", 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://core")


def _engine(name):
    return EmotionEngine(
        db_path=os.path.join(TMP, name + ".db"),
        cache_dir=os.path.join(TMP, "c-" + name),
        env={},
    )


def main() -> int:
    engine = _engine("criteria")
    core_main.app.state.engine = engine

    async def run():
        async with _client() as http:
            print("--- 判据 2：非法 trigger_type -> 422 ---")
            r = await http.post(
                "/api/v1/chat",
                json={"user_id": "U1", "message": "在吗", "trigger_type": "user_message"},
            )
            print("HTTP", r.status_code)
            print(r.text[:260])

            print()
            print("--- 判据 3：缺省 trigger_type -> WARN + user_msg ---")
            logs: list[str] = []

            class Cap(logging.Handler):
                def emit(self, record):
                    logs.append(record.getMessage())

            handler = Cap()
            logging.getLogger("ffw.core.main").addHandler(handler)
            logging.getLogger("ffw.core.main").setLevel(logging.WARNING)
            r = await http.post("/api/v1/chat", json={"user_id": "U1", "message": "我好难过"})
            print("HTTP", r.status_code, "trigger_type =", r.json()["debug"]["trigger_type"])
            print("captured:", [m for m in logs if "trigger_type" in m])
            logging.getLogger("ffw.core.main").removeHandler(handler)

            print()
            print("--- 判据 7：补偿补上账 + 同键重发幂等 ---")
            before = await http.get("/api/v1/state/U1") if False else None
            first = await http.post(
                "/api/v1/compensate",
                json={
                    "user_id": "U1",
                    "message": "我今天很开心",
                    "original_event_key": "evt-c-1",
                },
            )
            p1 = first.json()
            print("第一次:", {k: p1["debug"].get(k) for k in ("emotion", "prev", "delta", "applied", "event_key")})

            second = await http.post(
                "/api/v1/compensate",
                json={
                    "user_id": "U1",
                    "message": "我今天很开心",
                    "original_event_key": "evt-c-1",
                },
            )
            p2 = second.json()
            print("同键重发:", {k: p2["debug"].get(k) for k in ("emotion", "prev", "delta", "applied", "event_key")})
            print("emotion 上升:", p1["debug"]["emotion"] > p1["debug"]["prev"])
            print("二次 applied=False:", p2["debug"]["applied"] is False)

            print()
            print("--- 判据 4：runtime_context 前后状态不变 + 不占判重键 ---")
            st = await http.post(
                "/api/v1/chat",
                json={"user_id": "U4", "message": "qwxz", "chat_scope": "p:U4"},
            )
            before = st.json()["debug"]
            ctx = await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "U4",
                    "message": "（注入上下文）",
                    "chat_scope": "p:U4",
                    "platform_message_id": "mid-ctx",
                    "trigger_type": "runtime_context",
                },
            )
            cj = ctx.json()
            after_real = await http.post(
                "/api/v1/chat",
                json={
                    "user_id": "U4",
                    "message": "我好难过",
                    "chat_scope": "p:U4",
                    "platform_message_id": "mid-ctx",
                    "trigger_type": "user_msg",
                },
            )
            print("context:", {
                "status": cj["status"],
                "applied": cj["debug"]["applied"],
                "actions": cj["actions"],
                "emotion": cj["debug"]["emotion"],
                "affinity": cj["debug"]["affinity"],
                "bot_mood": cj["debug"]["bot_mood"],
            })
            print("前后 emotion/affinity/bot_mood 均不变:",
                  (before["emotion"], before["affinity"], before["bot_mood"]) ==
                  (cj["debug"]["emotion"], cj["debug"]["affinity"], cj["debug"]["bot_mood"]))
            print("同 message_id 后续真消息未被吞:", after_real.json()["duplicate"] is False,
                  "applied=", after_real.json()["debug"]["applied"])

            print()
            print("--- 判据 5：cron_task 状态不变但动作产出 ---")
            await http.post("/api/v1/chat",
                            json={"user_id": "U5", "message": "qwxz", "chat_scope": "p:U5"})
            cron = await http.post(
                "/api/v1/chat",
                json={"user_id": "U5", "message": "我好难过", "chat_scope": "p:U5",
                      "trigger_type": "cron_task"},
            )
            c5 = cron.json()["debug"]
            print({
                "applied": c5["applied"],
                "delta": c5["delta"],
                "emotion": c5["emotion"],
                "affinity": c5["affinity"],
                "bot_mood": c5["bot_mood"],
                "actions": len(cron.json()["actions"]),
                "inner_os": bool(cron.json()["inner_os"]),
            })

            print()
            print("--- 判据 6：proactive 钳制 + risk 优先级 ---")
            await http.post("/api/v1/chat",
                            json={"user_id": "U6", "message": "qwxz", "chat_scope": "p:U6"})
            pro = await http.post(
                "/api/v1/chat",
                json={"user_id": "U6", "message": "我好难过", "chat_scope": "p:U6",
                      "trigger_type": "proactive"},
            )
            d6 = pro.json()["debug"]
            print("proactive 负面: delta =", d6["delta"], "emotion =", d6["emotion"],
                  "prev =", d6["prev"], "-> delta>=0:", d6["delta"] >= 0)

            cris = await http.post(
                "/api/v1/chat",
                json={"user_id": "U6", "message": "我不想活了", "chat_scope": "p:U6",
                      "trigger_type": "proactive"},
            )
            c6 = cris.json()["debug"]
            print("proactive+risk: crisis =", c6["crisis"], "level =", c6["level"],
                  "delta =", c6["delta"], "-> 仍降:", c6["delta"] < 0)

    asyncio.run(run())
    try:
        engine.cache.close()
        engine.db_engine.dispose()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
