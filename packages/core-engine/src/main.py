"""FatFish-Whisper Core-Engine 入口（FastAPI）。

同时提供：
- /admin          单页管理后台（Vue3 + Tailwind CDN）
- /api/v1/chat    Adapter 调用的情绪判定接口
- /api/v1/state   状态看板数据
- /api/v1/config  参数读写（写回 .env 并热更新 Track B）
"""
from __future__ import annotations

import os
import sys

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(SRC_DIR)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv

ENV_PATH = os.path.join(CORE_DIR, ".env")
load_dotenv(ENV_PATH, override=True)

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import EmotionEngine

WEB_DIR = os.path.join(SRC_DIR, "web")
INDEX_PATH = os.path.join(WEB_DIR, "index.html")

HOST = os.environ.get("CORE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CORE_PORT", "8000"))


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.engine = EmotionEngine()
    print("[FatFish-Whisper] 引擎就绪 · 控制台 http://" + HOST + ":" + str(PORT) + "/admin")
    yield


app = FastAPI(title="FatFish-Whisper API", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------ 请求体

class ChatRequest(BaseModel):
    user_id: str = Field(default="10001", description="宿主侧的稳定用户 ID")
    message: str = Field(min_length=1, description="合并后的一条消息")
    debounce: bool = Field(default=False, description="是否由引擎做防抖合并（adapter 已防抖则传 false）")


class ConfigRequest(BaseModel):
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


# ------------------------------------------------------------------ .env 读写

def _mask(key: str) -> str:
    if not key:
        return ""
    if key.startswith("sk-xxx"):
        return key
    return key[:5] + "****" + key[-4:] if len(key) > 12 else "****"


def _read_env(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    if not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            data[key.strip()] = value.strip()
    return data


def _update_env_file(path: str, updates: dict[str, str]) -> None:
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(key + "=" + updates[key])
                seen.add(key)
                continue
        output.append(line)

    missing = [key for key in updates if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# ===== 由控制台写入 =====")
        for key in missing:
            output.append(key + "=" + updates[key])

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(output).rstrip("\n") + "\n")


# ------------------------------------------------------------------ 页面

@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin")


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_panel() -> HTMLResponse:
    if not os.path.exists(INDEX_PATH):
        raise HTTPException(status_code=500, detail="web/index.html 缺失")
    with open(INDEX_PATH, "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


app.mount(
    "/static",
    StaticFiles(directory=os.path.join(WEB_DIR, "static"), check_dir=False),
    name="static",
)


# ------------------------------------------------------------------ 接口

@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    engine = getattr(app.state, "engine", None)
    return {
        "status": "ok" if engine is not None else "starting",
        "track_b_enabled": bool(engine is not None and engine.track_b.enabled),
        "db": os.path.join(CORE_DIR, "app.db"),
        "model": engine.track_b.model if engine is not None else "",
    }


@app.get("/api/v1/state")
async def state_list(limit: int = 100) -> dict[str, Any]:
    engine = getattr(app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="引擎尚未就绪")
    return {"users": engine.list_states(limit=limit)}


@app.get("/api/v1/config")
async def config_get() -> dict[str, Any]:
    env = _read_env(ENV_PATH)
    return {
        "api_base": env.get("LLM_API_BASE", ""),
        "api_key": _mask(env.get("LLM_API_KEY", "")),
        "model": env.get("LLM_MODEL", ""),
        "whitelist": env.get("ADMIN_WHITELIST", ""),
        "env_path": ENV_PATH,
    }


@app.post("/api/v1/config")
async def config_set(req: ConfigRequest) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if req.api_base is not None and req.api_base.strip():
        updates["LLM_API_BASE"] = req.api_base.strip()
    if req.model is not None and req.model.strip():
        updates["LLM_MODEL"] = req.model.strip()
    if req.api_key is not None and req.api_key.strip() and "****" not in req.api_key:
        updates["LLM_API_KEY"] = req.api_key.strip()

    if not updates:
        return {"ok": False, "message": "没有需要写入的字段"}

    _update_env_file(ENV_PATH, updates)
    os.environ.update(updates)

    engine = getattr(app.state, "engine", None)
    if engine is not None:
        engine.env.update(updates)
        engine.reload_config()

    return {
        "ok": True,
        "message": "已写入 " + ", ".join(sorted(updates)) + " 并热更新",
        "track_b_enabled": bool(engine is not None and engine.track_b.enabled),
    }


@app.post("/api/v1/chat")
async def chat_endpoint(req: ChatRequest) -> dict[str, Any]:
    engine = getattr(app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="引擎尚未就绪")
    try:
        return await engine.handle_message(req.user_id, req.message, debounce=req.debounce)
    except Exception as exc:  # 兜底：绝不让情绪机把宿主 Bot 拖挂
        raise HTTPException(status_code=500, detail="判定失败: " + str(exc))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
