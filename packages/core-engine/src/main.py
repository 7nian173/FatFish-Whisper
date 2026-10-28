"""FatFish-Whisper Core-Engine 入口（FastAPI）。

同时提供：
- /admin          单页管理后台（Vue3 + Tailwind CDN）
- /api/v1/chat    Adapter 调用的情绪判定接口
- /api/v1/state   状态看板数据
- /api/v1/config  参数读写（写回 .env 并热更新 Track B）
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import os
import re
import shutil
import sys
import time

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

from engine import EmotionEngine, bands_meta
from security import (
    AUTH_MODE_LOOPBACK,
    AUTH_MODE_TOKEN,
    install_security,
    is_loopback_ip,
    verify_bind_policy,
)

WEB_DIR = os.path.join(SRC_DIR, "web")
INDEX_PATH = os.path.join(WEB_DIR, "index.html")
LEGACY_INDEX_PATH = os.path.join(WEB_DIR, "index.legacy.html")

HOST = os.environ.get("CORE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CORE_PORT", "8000"))
# RAW 保留「有没有显式配置」：None = 环境变量里没写，用于区分
# 「默认 loopback」和「用户显式写了 loopback」，两者告警文案不同。
AUTH_MODE_RAW = os.environ.get("AUTH_MODE")
AUTH_MODE = (AUTH_MODE_RAW or AUTH_MODE_LOOPBACK).strip().lower()
CORE_TOKEN = os.environ.get("CORE_TOKEN", "").strip()

# 启动策略校验放在**模块级**：无论 `python main.py` 还是 `uvicorn main:app`，
# 导入时都会执行，避免换一种启动方式就绕过校验。
# （不用 assert：python -O 会把它抹掉）
def _guard_launch_mode() -> None:
    """只允许 `python main.py` 这一种启动方式。

    为什么堵：`uvicorn main:app` 会
    ① 默认开启 proxy_headers（用 X-Forwarded-For 改写 request.client），
    ② 绕过 `_serve` 里的实际绑定校验与 --reload 拦截。
    两者叠加时，远端只要发 `X-Forwarded-For: 127.0.0.1` 就会被判成本机，
    loopback 模式等于零凭据裸奔。
    """
    argv = [str(a) for a in sys.argv]
    joined = " ".join(argv[1:]).lower()
    program = os.path.basename(argv[0]).lower() if argv else ""

    if "--reload" in joined:
        raise SystemExit(
            "[FATAL] 不支持 --reload：启动校验必须在真正服务的进程内完成，"
            "而 --reload 会另起子进程。请直接用 python main.py。"
        )

    if program.startswith("uvicorn") or "main:app" in joined:
        raise SystemExit(
            "[FATAL] 请用 `python main.py` 启动。\n"
            "        uvicorn CLI 会默认开启 proxy_headers（用 X-Forwarded-For 改写 request.client），\n"
            "        并绕过启动期绑定校验；远端伪造 `X-Forwarded-For: 127.0.0.1` 即可绕过 loopback 模式。"
        )

    for flag in ("--proxy-headers", "--forwarded-allow-ips", "--host", "--port"):
        if flag in joined:
            raise SystemExit(
                "[FATAL] 不要通过命令行传 {0}；请用环境变量 CORE_HOST / CORE_PORT 并运行 python main.py。".format(flag)
            )


_guard_launch_mode()
verify_bind_policy(HOST, AUTH_MODE, CORE_TOKEN, port=PORT)


@asynccontextmanager
async def lifespan(application: FastAPI):
    engine = EmotionEngine()
    application.state.engine = engine
    print("[FatFish-Whisper] 引擎就绪 · 控制台 http://" + HOST + ":" + str(PORT) + "/admin")

    # 阶段五：可选的夜间演化定时任务（EVOLUTION_ENABLED=1 才启动）
    evolution = None
    task = None
    if _evolution_enabled():
        evolution = _build_evolution(application, engine)
        task = asyncio.create_task(evolution.scheduler(on_report=_record_evolution))
        print("[FatFish-Whisper] 演化定时任务已启动（每天 03:00 UTC）")

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if evolution is not None:
            evolution.close()


app = FastAPI(title="FatFish-Whisper API", version="0.1.0", lifespan=lifespan)

# 安全加固：Host 校验（防 DNS rebinding）+ 非回环必须持令牌 + 写配置需自定义头。
# 令牌可选（CORE_TOKEN）；不设也能挡住浏览器侧攻击，只是挡不住本机其它进程。
SECURITY_SUMMARY = install_security(
    app,
    bound_host=HOST,
    token=CORE_TOKEN,
    auth_mode=AUTH_MODE_RAW,
    extra_hosts=os.environ.get("CORE_EXTRA_HOSTS", "").split(","),
)


# ------------------------------------------------------------------ 请求体

class ChatRequest(BaseModel):
    user_id: str = Field(default="10001", description="宿主侧的稳定用户 ID")
    message: str = Field(min_length=1, description="合并后的一条消息")
    debounce: bool = Field(default=False, description="是否由引擎做防抖合并（adapter 已防抖则传 false）")
    platform_message_id: Optional[str] = Field(
        default=None,
        description="宿主平台的原生消息 ID（NapCat/OneBot 的 message_id）。"
        "有它就用它做入口判重与幂等，比文本启发式准得多。",
    )
    group_id: Optional[str] = Field(default=None, description="群号；私聊为 None")
    chat_scope: Optional[str] = Field(
        default=None,
        description="会话分区，``g:<群号>`` 或 ``p:<QQ号>``。不传则由 group_id 派生"
        "（群聊 g:<群号>，否则 p:<user_id>）。**派生只发生在这里一次**，下游禁止自拼。",
    )
    is_at_bot: bool = Field(default=False, description="这条消息是否 @ 了 Bot")
    event_key: Optional[str] = Field(
        default=None,
        description="幂等键。不传时：有 platform_message_id 就用它，否则按批次自动生成",
    )
    timestamp: Optional[int] = Field(
        default=None,
        description="消息在**宿主侧**产生的时刻（整型毫秒 Unix 时间戳，P0-8 乱序防御）。"
        "不传则由 Core 用当前时间兜底；传了就参与乱序比对——比库里的 arrival_ts 旧的消息"
        "会被丢弃，绝不会覆盖更新的状态。",
    )


class ConfigRequest(BaseModel):
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class PersonaRequest(BaseModel):
    """人设卡全文。

    上限 20000 字：不是为了防人设长，而是防一次误粘贴把文件写成几十兆
    （演化压缩阈值只有 1000 字，超过会走 LLM 压缩，太离谱的直接拒收）。
    """

    text: str = Field(min_length=1, max_length=20000, description="人设卡全文")


# ------------------------------------------------------------------ .env 读写

#: 纯占位符（全是 x），不是真密钥，可以原样显示
_PLACEHOLDER_KEY = re.compile(r"^sk-x{3,}$", re.IGNORECASE)


def _mask(key: str) -> str:
    """脱敏显示。

    注意：不要按前缀判断——以前 ``startswith("sk-xxx")`` 会原样返回，
    任何以该前缀开头的**真实**密钥都会被完整回显。
    """
    if not key:
        return ""
    if _PLACEHOLDER_KEY.match(key):
        return key  # 就是占位符本身，没有泄露风险
    if len(key) <= 12:
        return "****"
    return key[:5] + "****" + key[-4:]


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


# ------------------------------------------------------------------ 演化 / 人设


def _persona_path() -> str:
    return os.environ.get("PERSONA_PATH") or os.path.join(CORE_DIR, "persona.txt")


def _backups_dir() -> str:
    return os.environ.get("BACKUP_DIR") or os.path.join(CORE_DIR, "backups")


def _evolution_enabled() -> bool:
    return os.environ.get("EVOLUTION_ENABLED", "0").strip() in ("1", "true", "True")


def _build_evolution(application: FastAPI, engine: Any) -> Any:
    """构造演化引擎（**懒加载**：不配 EVOLUTION_ENABLED 也不该在启动时建目录）。"""
    from evolution import EvolutionEngine

    evolution = EvolutionEngine(
        # 复用引擎自己的 db 路径：测试注入临时库时不会被偷偷指回真实 app.db
        db_path=getattr(engine, "db_path", None) or os.path.join(CORE_DIR, "app.db"),
        persona_path=_persona_path(),
        backups_dir=_backups_dir(),
        memory=engine.memory.local,
        env=engine.env,
        monitor=engine.monitor,
        session_factory=engine.Session,
    )
    application.state.evolution = evolution
    return evolution


def _get_evolution(application: FastAPI, engine: Any) -> Any:
    existing = getattr(application.state, "evolution", None)
    if existing is None:
        existing = _build_evolution(application, engine)
    return existing


def _record_evolution(report: Any) -> None:
    """定时任务每轮结束的回调：把报告留在 app.state 供后台查询。"""
    app.state.evolution_last_report = report.as_dict()
    app.state.evolution_last_run_at = (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(tzinfo=None)
        .isoformat()
        + "Z"
    )


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


@app.get("/admin/legacy", response_class=HTMLResponse, include_in_schema=False)
async def admin_legacy_panel() -> HTMLResponse:
    """旧版控制台（只保留状态看板 + 参数配置 + 测试对话）。

    留着它是为了让"新版渲染出问题"有一条一键回退的路，而不是让人去 git 里翻。
    """
    if not os.path.exists(LEGACY_INDEX_PATH):
        raise HTTPException(status_code=404, detail="旧版控制台已被移除")
    with open(LEGACY_INDEX_PATH, "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


app.mount(
    "/static",
    StaticFiles(directory=os.path.join(WEB_DIR, "static"), check_dir=False),
    name="static",
)


# ------------------------------------------------------------------ 接口

@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    """探活接口。

    token 模式下响应**只含 status**（不泄露模型、路径、引擎状态）；
    loopback 模式额外给一个 track_b_enabled，方便本机排查。
    """
    engine = getattr(app.state, "engine", None)
    status = "ok" if engine is not None else "starting"
    if AUTH_MODE == AUTH_MODE_TOKEN:
        return {"status": status}
    return {
        "status": status,
        "track_b_enabled": bool(engine is not None and engine.track_b.enabled),
    }


@app.get("/api/v1/state")
async def state_list(limit: int = 100) -> dict[str, Any]:
    engine = getattr(app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="引擎尚未就绪")
    # bot_mood 是「它自己的心情」（全局一份），与下面的 per-user 情绪是两层状态
    return {
        "users": engine.list_states(limit=limit),
        "bot_mood": engine._get_bot_mood(),
        "traits": engine.traits.as_dict(),
        # 六段情绪带的唯一真源（前端按 band 键做颜色映射，不再自己抄阈值）
        "bands": bands_meta(),
    }


@app.get("/api/v1/admin/evolution_status")
async def evolution_status() -> dict[str, Any]:
    """演化系统的运行状态（后台时间线的数据源）。

    字段里 ``last_result`` 只在本次进程内跑过演化时才有值——定时任务是
    进程内 asyncio 任务，重启即清空，这是有意的（不做持久化审计表）。
    """
    engine = getattr(app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="引擎尚未就绪")

    evolution = _get_evolution(app, engine)
    from evolution import MAX_PERSONA_CHARS

    latest = evolution.latest_backup()
    backup_count = 0
    try:
        backup_count = len(
            [
                name
                for name in os.listdir(_backups_dir())
                if name.startswith(os.path.splitext(os.path.basename(_persona_path()))[0])
                and name.endswith(".txt")
            ]
        )
    except OSError:
        backup_count = 0

    last = getattr(app.state, "evolution_last_report", None)
    ooc_passed = len(last.get("evolved") or []) if isinstance(last, dict) else 0
    ooc_rejected = 0
    if isinstance(last, dict):
        ooc_rejected = sum(
            1
            for item in (last.get("skipped") or [])
            if "ooc" in str(item.get("reason", "")).lower()
        )

    active = 0
    try:
        today = datetime.datetime.now(datetime.timezone.utc).date()
        active = len(evolution.active_users(today))
    except Exception:  # 数据库异常不该让后台整页 500
        active = 0

    return {
        "enabled": _evolution_enabled(),
        "next_run_at": evolution.next_run_at().isoformat() + "Z",
        "last_run_at": getattr(app.state, "evolution_last_run_at", None),
        "last_result": last,
        "backup": {
            "latest": os.path.basename(latest) if latest else None,
            "count": backup_count,
        },
        "ooc_probe": {"passed": ooc_passed, "rejected": ooc_rejected},
        "active_users": active,
        "persona": {
            "chars": evolution.persona_chars(),
            "max_chars": MAX_PERSONA_CHARS,
        },
    }


@app.get("/api/v1/admin/persona")
async def persona_get() -> dict[str, Any]:
    """读取人设卡正文（人设热加载：每次读文件，改完立刻生效）。"""
    from evolution import MAX_PERSONA_CHARS

    path = _persona_path()
    text = ""
    updated_at = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            updated_at = datetime.datetime.fromtimestamp(
                os.path.getmtime(path), datetime.timezone.utc
            ).isoformat()
        except OSError:
            raise HTTPException(status_code=500, detail="人设卡读取失败")

    return {
        "text": text,
        "chars": len(text),
        "max_chars": MAX_PERSONA_CHARS,
        "path": path,
        "updated_at": updated_at,
        "will_compress_next_run": len(text) > MAX_PERSONA_CHARS,
    }


@app.post("/api/v1/admin/persona")
async def persona_set(req: "PersonaRequest") -> dict[str, Any]:
    """保存人设卡（先备份再原子替换，写坏了也不会留下半个文件）。"""
    from evolution import MAX_PERSONA_CHARS

    path = _persona_path()
    text = req.text

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    # 覆盖前先留一份手工备份：演化每天只备份一次，手工改崩了还得靠它回滚
    if os.path.exists(path):
        try:
            os.makedirs(_backups_dir(), exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = os.path.splitext(os.path.basename(path))[0]
            shutil.copyfile(path, os.path.join(_backups_dir(), stem + "_manual_" + stamp + ".txt"))
        except OSError:
            pass  # 备份失败不阻塞保存

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise HTTPException(status_code=500, detail="人设卡写入失败")

    return {
        "ok": True,
        "chars": len(text),
        # 人设是每次读文件的，所以保存即生效，不需要重启
        "hot_reloaded": True,
        "will_compress_next_run": len(text) > MAX_PERSONA_CHARS,
    }


@app.get("/api/v1/admin/token_stats")
async def token_stats(day: Optional[str] = None) -> dict[str, Any]:
    """今日（或指定日期）各模块 token 消耗。

    Args:
        day: ``YYYY-MM-DD``，不传就是今天（UTC）。
    """
    engine = getattr(app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="引擎尚未就绪")

    target = None
    if day:
        try:
            target = datetime.date.fromisoformat(day)
        except ValueError:
            raise HTTPException(status_code=400, detail="day 必须是 YYYY-MM-DD")

    stats = engine.monitor.stats_today(target)
    payload = engine.monitor.summary_dict(stats)
    today = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).date()
    payload["day"] = (target or today).isoformat()
    return payload


@app.get("/api/v1/admin/memory_health")
async def memory_health() -> dict[str, Any]:
    """记忆路由各层健康度（排查"到底在用哪一级记忆"）。"""
    engine = getattr(app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="引擎尚未就绪")
    return {"providers": engine.memory.health()}


@app.get("/api/v1/config")
async def config_get() -> dict[str, Any]:
    env = _read_env(ENV_PATH)
    return {
        "api_base": env.get("LLM_API_BASE", ""),
        "api_key": _mask(env.get("LLM_API_KEY", "")),
        "model": env.get("LLM_MODEL", ""),
        "whitelist": env.get("ADMIN_WHITELIST", ""),
        "privileged_users": env.get("PRIVILEGED_USERS", ""),
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


def derive_chat_scope(
    user_id: str, group_id: Optional[str] = None, explicit: Optional[str] = None
) -> str:
    """派生 ``chat_scope``——**全仓唯一的派生点**（规格 §7/§8）。

    规则（顺序固定）：
        1. 调用方显式传入的 ``chat_scope`` 优先（Adapter 已按自己的 Target 算好时用）；
        2. ``group_id`` strip 后非空 → ``g:<group_id>``；
        3. 其余（含空串 / 纯空白 / None）→ ``p:<user_id>``。

    为什么空串 group_id 不能推成 ``"g:"``：``"g:"`` 不是一个合法分区，它会让所有
    「群号缺失」的用户挤进同一个假群，把彼此的情绪和记忆混在一起。宁可退化为私聊
    分区，也不制造一个共享的伪分区。
    """
    if explicit is not None:
        cleaned = str(explicit).strip()
        if cleaned:
            return cleaned
    group = "" if group_id is None else str(group_id).strip()
    if group:
        return "g:" + group
    return "p:" + str(user_id)


@app.post("/api/v1/chat")
async def chat_endpoint(req: ChatRequest) -> dict[str, Any]:
    engine = getattr(app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="引擎尚未就绪")

    # 会话分区在这里派生**唯一一次**，之后一路透传（引擎内部禁止自拼）。
    # 必须早于判重：判重键要含分区，否则同一个人在 A 群和 B 群说同一句话时，
    # 第二条会被当成幽灵消息直接吞掉（跨群串味的另一种形态）。
    chat_scope = derive_chat_scope(req.user_id, req.group_id, req.chat_scope)

    # ---- P0-8 乱序防御：提取本次请求的到达时间戳（整型毫秒）----
    # 宿主没传就用 Core 侧当前时间兜底：至少保证「同时刻到达的顺序」不被倒置，
    # 也不会因为缺字段而让整条链路拒绝服务。NaN/负数是非法的 epoch 毫秒，按缺失处理。
    request_ts = int(req.timestamp) if req.timestamp is not None and int(req.timestamp) > 0 else None
    if request_ts is None:
        request_ts = int(time.time() * 1000)

    # ---- 入口判重（幽灵消息）：有平台 message_id 就按 id 判，没有退化为文本短窗口 ----
    if engine.ingress_deduper.is_duplicate(
        req.user_id, req.message, req.platform_message_id, chat_scope
    ):
        return {
            "status": "duplicate",
            "duplicate": True,
            "applied": False,
            "actions": [],
            "inner_os": "",
            "debug": {
                "reason": "入口判重命中（platform_message_id={0}）".format(
                    req.platform_message_id or "无，按文本窗口"
                ),
                "platform_message_id": req.platform_message_id,
            },
        }

    # 幂等键优先级：调用方显式 event_key > 平台消息 ID > 引擎按批次生成
    event_key = req.event_key or req.platform_message_id

    try:
        payload = await engine.handle_message(
            req.user_id,
            req.message,
            debounce=req.debounce,
            event_key=event_key,
            chat_scope=chat_scope,
            request_ts=request_ts,
        )
    except Exception as exc:  # 兜底：绝不让情绪机把宿主 Bot 拖挂
        raise HTTPException(status_code=500, detail="判定失败: " + str(exc))

    payload["status"] = "ok"
    payload["duplicate"] = False
    payload["platform_message_id"] = req.platform_message_id
    payload["chat_scope"] = chat_scope
    # 业务层返回的是库中**实际**水位线；旧消息被丢弃时不能回显本次旧 timestamp。
    # 注意不能写成 `... or request_ts`：水位线真为 0（老行、尚未应用任何带时间戳
    # 的请求）时它是合法值，用 falsy 兜底会把本次 timestamp 冒充成台账。
    debug = payload.get("debug") or {}
    if "arrival_ts" in debug:
        payload["arrival_ts"] = int(debug.get("arrival_ts") or 0)
    else:
        payload["arrival_ts"] = request_ts
    return payload


def _serve() -> None:
    """启动服务：自己绑定 socket → 校验实际地址 → 交给 uvicorn 服务。

    为什么自己绑：``verify_bind_policy`` 只校验配置字面量/解析结果，
    真正监听哪个地址由 uvicorn 决定；自己绑一次才能确认**实际**地址。

    为什么用 ``serve(sockets=[...])`` 而不是 fd：Windows 上没有 ``socket.AF_UNIX``，
    uvicorn 的 ``fromfd`` 路径会抛 AttributeError（实测）。传 socket 对象跨平台可用。
    """
    import asyncio
    import socket as _socket

    import uvicorn

    # 版本断言用显式 raise：python -O 会把 assert 整条抹掉
    if ".".join(uvicorn.__version__.split(".")[:2]) != "0.54":
        raise SystemExit(
            "[FATAL] 启动校验依赖 uvicorn 0.54.x 的 serve(sockets=...) 行为，当前 {0}。"
            "升级前请先验证 security.verify_bind_policy 与 _serve 仍可用。".format(
                uvicorn.__version__)
        )

    if any(arg == "--reload" for arg in sys.argv):
        raise SystemExit(
            "[FATAL] 不支持 --reload：启动校验必须在真正服务的进程内完成，"
            "而 --reload 会另起子进程。请直接用 python main.py。"
        )

    family = _socket.AF_INET6 if ":" in HOST else _socket.AF_INET
    try:
        sock = _socket.create_server((HOST, PORT), family=family)
    except OSError as exc:
        raise SystemExit(
            "[FATAL] 端口 {0}:{1} 绑定失败：{2}".format(
                HOST, PORT, getattr(exc, "strerror", None) or exc)
        ) from exc

    try:
        bound_host = sock.getsockname()[0]
        if AUTH_MODE == AUTH_MODE_LOOPBACK and not is_loopback_ip(bound_host):
            raise SystemExit(
                "[FATAL] 实际绑定 {0} 非回环，与 AUTH_MODE=loopback 冲突。\n"
                "        本机自用请设 CORE_HOST=127.0.0.1；\n"
                "        需要对外提供服务请设 AUTH_MODE=token 并配置 CORE_TOKEN。".format(bound_host)
            )

        async def run() -> None:
            # 必须关掉 uvicorn 自带的 ProxyHeadersMiddleware：
            # 它默认信任 127.0.0.1 并用 X-Forwarded-For 改写 request.client，
            # 会让我们在 security.resolve_client 里对「已被改写过的 peer」二次解析。
            config = uvicorn.Config(app, log_level="info", proxy_headers=False)
            server = uvicorn.Server(config)
            await server.serve(sockets=[sock])

        asyncio.run(run())
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    _serve()
