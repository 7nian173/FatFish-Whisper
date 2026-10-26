"""PR1 验收：鉴权缺陷修复。

覆盖：
1. IP 归一化（IPv4-mapped / IPv6 回环）——本机调用不被误杀
2. `token_ok_strict`：空令牌必须 False（缺陷回归锁）
3. loopback 模式：回环放行、非回环 403、client 缺失 403
4. 管理头矩阵：`/api/v1/admin/*`、`POST /config`、`/static`
5. 启动期 fail-fast：绑定地址 / 令牌长度（显式 SystemExit，不依赖 assert）
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI

from security import (
    AUTH_MODE_LOOPBACK,
    AUTH_MODE_TOKEN,
    install_security,
    is_loopback_ip,
    normalize_ip,
    parse_trusted_proxies,
    token_ok_strict,
    verify_bind_policy,
)

LOOPBACK = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.9", 40000)
IPV6_LOOPBACK = ("::1", 40000)
IPV4_MAPPED = ("::ffff:127.0.0.1", 40000)


def _run(coro):
    return asyncio.run(coro)


def _client(app, peer):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url="http://core",
    )


@pytest.fixture
def probe_app():
    """最小应用：装同一套中间件，不依赖真实引擎。"""
    app = FastAPI()

    @app.get("/api/v1/state")
    async def state():
        return {"ok": True}

    @app.get("/api/v1/admin/token_stats")
    async def admin_stats():
        return {"ok": True}

    @app.post("/api/v1/config")
    async def config_set():
        return {"ok": True}

    @app.get("/admin")
    async def admin_page():
        return {"ok": True}

    @app.get("/static/x.css")
    async def static_asset():
        return {"ok": True}

    install_security(app, auth_mode=AUTH_MODE_LOOPBACK, extra_hosts=["core"])
    return app


def _get(app, peer, path, **kwargs):
    async def scenario():
        async with _client(app, peer) as http:
            return await http.get(path, **kwargs)

    return _run(scenario())


def _post(app, peer, path, **kwargs):
    async def scenario():
        async with _client(app, peer) as http:
            return await http.post(path, **kwargs)

    return _run(scenario())


# ---------------------------------------------------------------- 1. IP 归一化


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("::1", "::1"),
        ("::ffff:127.0.0.1", "127.0.0.1"),      # IPv4-mapped 必须归一
        ("::ffff:10.0.0.5", "10.0.0.5"),
        ("203.0.113.9", "203.0.113.9"),
        ("not-an-ip", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_ip(raw, expected):
    assert normalize_ip(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),   # Windows 双栈下就是这个形态
        ("127.0.0.2", True),
        ("203.0.113.9", False),
        ("10.0.0.5", False),
        ("bad", False),
    ],
)
def test_is_loopback_ip(raw, expected):
    assert is_loopback_ip(raw) is expected


def test_trusted_proxies_解析为IP集合():
    parsed = parse_trusted_proxies("127.0.0.1, ::1 ,::ffff:10.0.0.5")
    assert parsed == frozenset({"127.0.0.1", "::1", "10.0.0.5"})


def test_trusted_proxies_非法项抛错():
    with pytest.raises(ValueError):
        parse_trusted_proxies("127.0.0.1,not-an-ip")


# ---------------------------------------------------------------- 2. 令牌


def test_空令牌严格校验必须为False():
    """缺陷回归锁：旧版 token_ok 在 expected 为空时返回 True。"""
    class _Req:
        headers = {}

    assert token_ok_strict(_Req(), "") is False


# ---------------------------------------------------------------- 3. loopback 模式


def test_回环来源放行(probe_app):
    assert _get(probe_app, LOOPBACK, "/api/v1/state").status_code == 200


def test_IPv6回环放行(probe_app):
    assert _get(probe_app, IPV6_LOOPBACK, "/api/v1/state").status_code == 200


def test_IPv4映射回环放行(probe_app):
    """本机 adapter 在 Windows 双栈下可能就是这个形态，不能被误杀。"""
    assert _get(probe_app, IPV4_MAPPED, "/api/v1/state").status_code == 200


def test_非回环来源拒绝(probe_app):
    resp = _get(probe_app, REMOTE, "/api/v1/state")
    assert resp.status_code == 403
    assert "loopback" in resp.text


def test_client缺失返回403(probe_app):
    async def scenario():
        transport = httpx.ASGITransport(app=probe_app, client=None)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as http:
            return await http.get("/api/v1/state")

    assert _run(scenario()).status_code == 403


def test_Host伪造仍然拦截(probe_app):
    assert _get(probe_app, LOOPBACK, "/api/v1/state", headers={"host": "evil.example.com"}).status_code == 400


# ---------------------------------------------------------------- 4. 管理头


def test_管理GET缺头被拒(probe_app):
    assert _get(probe_app, LOOPBACK, "/api/v1/admin/token_stats").status_code == 403


def test_管理GET带头放行(probe_app):
    resp = _get(probe_app, LOOPBACK, "/api/v1/admin/token_stats", headers={"X-FFW-Admin": "1"})
    assert resp.status_code == 200


def test_写配置缺头被拒(probe_app):
    assert _post(probe_app, LOOPBACK, "/api/v1/config", json={}).status_code == 403


def test_写配置带头放行(probe_app):
    resp = _post(probe_app, LOOPBACK, "/api/v1/config", json={}, headers={"X-FFW-Admin": "1"})
    assert resp.status_code == 200


def test_静态资源不要求管理头(probe_app):
    """子资源（<script>/<link>）加不了自定义头，不能被管理头拦死。"""
    assert _get(probe_app, LOOPBACK, "/static/x.css").status_code == 200


def test_loopback模式下admin页面不要求管理头(probe_app):
    assert _get(probe_app, LOOPBACK, "/admin").status_code == 200


# ---------------------------------------------------------------- 5. token 模式


@pytest.fixture
def token_app():
    app = FastAPI()

    @app.get("/api/v1/state")
    async def state():
        return {"ok": True}

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.get("/admin")
    async def admin_page():
        return {"ok": True}

    install_security(app, auth_mode=AUTH_MODE_TOKEN, token="t" * 24, extra_hosts=["core"])
    return app


def test_token模式无令牌401(token_app):
    assert _get(token_app, LOOPBACK, "/api/v1/state").status_code == 401


def test_token模式错令牌401(token_app):
    assert _get(token_app, LOOPBACK, "/api/v1/state", headers={"X-Core-Token": "wrong"}).status_code == 401


def test_token模式正确令牌放行(token_app):
    assert _get(token_app, LOOPBACK, "/api/v1/state", headers={"X-Core-Token": "t" * 24}).status_code == 200


def test_token模式回环也要令牌(token_app):
    """回环不豁免：token 模式下令牌是唯一凭据。"""
    assert _get(token_app, LOOPBACK, "/api/v1/state").status_code == 401


def test_token模式health豁免(token_app):
    assert _get(token_app, LOOPBACK, "/api/v1/health").status_code == 200


def test_token模式admin页面需要管理头(token_app):
    token = {"X-Core-Token": "t" * 24}
    assert _get(token_app, LOOPBACK, "/admin", headers=token).status_code == 403
    assert _get(token_app, LOOPBACK, "/admin", headers={**token, "X-FFW-Admin": "1"}).status_code == 200


# ---------------------------------------------------------------- 6. 启动校验


def test_绑定0_0_0_0在loopback模式拒绝启动():
    with pytest.raises(SystemExit) as exc:
        verify_bind_policy("0.0.0.0", AUTH_MODE_LOOPBACK, "")
    assert "AUTH_MODE=token" in str(exc.value)


def test_绑定双栈通配拒绝启动():
    with pytest.raises(SystemExit):
        verify_bind_policy("::", AUTH_MODE_LOOPBACK, "")


def test_主机名解析到非回环拒绝启动():
    with pytest.raises(SystemExit):
        verify_bind_policy("myhost.local", AUTH_MODE_LOOPBACK, "", resolver=lambda h, p: {"192.168.1.7"})


def test_localhost解析为回环允许启动():
    verify_bind_policy("localhost", AUTH_MODE_LOOPBACK, "", resolver=lambda h, p: {"127.0.0.1", "::1"})


def test_主机名解析失败拒绝启动():
    def boom(host, port):
        raise OSError("name resolution failed")

    with pytest.raises(SystemExit):
        verify_bind_policy("nope.invalid", AUTH_MODE_LOOPBACK, "", resolver=boom)


def test_token模式空令牌拒绝启动():
    with pytest.raises(SystemExit):
        verify_bind_policy("0.0.0.0", AUTH_MODE_TOKEN, "")


def test_token模式短令牌拒绝启动():
    with pytest.raises(SystemExit):
        verify_bind_policy("0.0.0.0", AUTH_MODE_TOKEN, "short")


def test_token模式合法配置通过():
    verify_bind_policy("0.0.0.0", AUTH_MODE_TOKEN, "x" * 32)


def test_非法模式拒绝启动():
    with pytest.raises(SystemExit):
        verify_bind_policy("127.0.0.1", "whatever", "")


# ---------------------------------------------------------------- 7. 合并前复审补充


def test_配了令牌但loopback模式要显式标记被忽略():
    """安全降级必须可发现：配了 CORE_TOKEN 却没开 token 模式。"""
    app = FastAPI()
    summary = install_security(app, auth_mode=AUTH_MODE_LOOPBACK, token="x" * 24)
    assert summary["token_ignored"] is True
    assert summary["token_required"] is False


def test_token模式不会误报token_ignored():
    app = FastAPI()
    summary = install_security(app, auth_mode=AUTH_MODE_TOKEN, token="x" * 24)
    assert summary["token_ignored"] is False
    assert summary["token_required"] is True


def test_非ASCII令牌头不抛异常():
    """hmac.compare_digest 对非 ASCII str 会抛 TypeError → 500。

    Starlette 用 latin-1 解码头，客户端发 `Authorization: Bearer 令` 就能触发。
    修复后必须返回 False，而不是抛异常。
    """
    class _Req:
        def __init__(self, headers):
            self.headers = headers

    assert token_ok_strict(_Req({"x-core-token": "令牌"}), "x" * 24) is False
    assert token_ok_strict(_Req({"authorization": "Bearer 令"}), "x" * 24) is False


def test_非法AUTH_MODE直接拒绝而不是静默降级():
    with pytest.raises(SystemExit) as exc:
        install_security(FastAPI(), auth_mode="whatever", token="")
    assert "AUTH_MODE" in str(exc.value)


def test_token模式外部Host未列入白名单返回400(token_app):
    """Host 校验在鉴权之前：域名/公网 IP 访问需配 CORE_EXTRA_HOSTS。"""
    resp = _get(
        token_app,
        LOOPBACK,
        "/api/v1/health",
        headers={"host": "bot.example.com", "X-Core-Token": "t" * 24},
    )
    assert resp.status_code == 400


def test_token模式列入白名单的Host可通过(token_app):
    resp = _get(token_app, LOOPBACK, "/api/v1/health", headers={"host": "core"})
    assert resp.status_code == 200


# ---------------------------------------------------------------- 8. REVISE 补充

#: 真实启动入口所在目录（tests/ 的上一级是 core-engine/tests → src）
_SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

#: 直接用 ASGI scope 调用：httpx 会把头按 latin-1 编码，喂不进「非 ASCII 原样字节」，
#: 而真实网络/反代过来的就是原始字节，所以这里绕过客户端，只测服务端行为。
def _asgi_status(app, path, headers) -> int:
    async def scenario():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 40000),
            "server": ("core", 80),
        }
        await app(scope, receive, send)
        return next(m["status"] for m in messages if m["type"] == "http.response.start")

    return _run(scenario())


def test_未显式设置AUTH_MODE且配了令牌要告警(caplog, monkeypatch):
    """安全降级必须可发现：CORE_TOKEN 配了、AUTH_MODE 没设（默认 loopback）。"""
    monkeypatch.delenv("AUTH_MODE", raising=False)
    with caplog.at_level(logging.WARNING, logger="ffw.security"):
        summary = install_security(FastAPI(), token="x" * 24)
    assert summary["token_ignored"] is True
    assert summary["auth_mode_implicit"] is True
    assert "令牌不会生效" in caplog.text


def test_显式loopback且配了令牌同样告警(caplog):
    with caplog.at_level(logging.WARNING, logger="ffw.security"):
        summary = install_security(FastAPI(), auth_mode=AUTH_MODE_LOOPBACK, token="x" * 24)
    assert summary["token_ignored"] is True
    assert summary["auth_mode_implicit"] is False
    assert "令牌不会生效" in caplog.text


def test_没配令牌时不告警(caplog):
    with caplog.at_level(logging.WARNING, logger="ffw.security"):
        summary = install_security(FastAPI(), token="")
    assert summary["token_ignored"] is False
    assert "令牌不会生效" not in caplog.text


def test_token模式非ASCII令牌头返回401而不是500(token_app):
    """旧版 compare_digest 对非 ASCII str 抛 TypeError → 500；现在必须是 401。"""
    status = _asgi_status(
        token_app,
        "/api/v1/state",
        [(b"host", b"core"), (b"x-core-token", "令牌".encode("utf-8"))],
    )
    assert status == 401


def test_token模式非ASCII的Bearer头返回401而不是500(token_app):
    status = _asgi_status(
        token_app,
        "/api/v1/state",
        [(b"host", b"core"), (b"authorization", "Bearer 令牌".encode("utf-8"))],
    )
    assert status == 401


#: main.py 里 load_dotenv(override=True) 会用 .env 覆盖进程环境变量，
#: 这里把 dotenv 换成 no-op 桩，保证「启动路径」用例只受显式设置的环境变量影响。
_BOOTSTRAP = (
    "import sys, types\n"
    "stub = types.ModuleType('dotenv')\n"
    "stub.load_dotenv = lambda *a, **k: None\n"
    "sys.modules['dotenv'] = stub\n"
    "import main\n"
)


def _boot_main(overrides: dict) -> subprocess.CompletedProcess:
    """在独立进程里走真实启动路径（`python -c "import main"`）。"""
    env = dict(os.environ)
    env.pop("AUTH_MODE", None)
    env.pop("CORE_TOKEN", None)
    env.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP],
        cwd=_SRC_DIR,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=180,
    )


def test_启动路径非法AUTH_MODE直接退出():
    """模块导入即校验：换 uvicorn / python main.py 都绕不过去。"""
    proc = _boot_main({"AUTH_MODE": "whatever", "CORE_HOST": "127.0.0.1"})
    assert proc.returncode != 0, proc.stdout
    assert "AUTH_MODE" in proc.stderr, proc.stderr


def test_启动路径合法loopback配置可正常导入():
    proc = _boot_main({"AUTH_MODE": AUTH_MODE_LOOPBACK, "CORE_HOST": "127.0.0.1"})
    assert proc.returncode == 0, proc.stderr


def test_启动路径配了令牌但没设AUTH_MODE要告警且保留本机行为():
    """本机 Adapter 行为不变（仍可启动、仍按来源判断），但必须有显式告警。"""
    proc = _boot_main({"CORE_TOKEN": "x" * 24, "CORE_HOST": "127.0.0.1"})
    assert proc.returncode == 0, proc.stderr
    assert "AUTH_MODE" in proc.stderr, proc.stderr
