"""PR2 验收：XFF 信任模型、代理边界、绑定地址校验。

覆盖《安全专项 v4》7.1 / 7.2 / 7.5 的用例。
"""
from __future__ import annotations

import asyncio
import socket
import sys

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.requests import Request

from security import (
    AUTH_MODE_LOOPBACK,
    install_security,
    resolve_client,
)

TRUSTED = frozenset({"127.0.0.1", "::1"})


def _request(peer, headers=()):
    """构造一个最小 ASGI Request；headers 是 [(name, value)]，允许重复。"""
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": raw, "client": (peer, 12345) if peer else None,
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


# ---------------------------------------------------------------- 7.1 XFF 信任

def test_非可信来源忽略XFF():
    client, source = resolve_client(
        _request("203.0.113.9", [("x-forwarded-for", "127.0.0.1")]), TRUSTED)
    assert (client, source) == ("203.0.113.9", "peer")


def test_可信来源单跳():
    client, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", "1.2.3.4")]), TRUSTED)
    assert (client, source) == ("1.2.3.4", "xff")


def test_多级代理取右起第一个非可信():
    client, _ = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", "1.2.3.4, 127.0.0.1")]), TRUSTED)
    assert client == "1.2.3.4"


def test_伪造左端被跳过():
    client, _ = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", "127.0.0.1, 1.2.3.4")]), TRUSTED)
    assert client == "1.2.3.4"


def test_多跳全部可信时回落到peer():
    client, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", "127.0.0.1, ::1")]), TRUSTED)
    assert source == "xff_all_trusted"


def test_XFF超长被拒():
    hops = ", ".join(["10.0.0.%d" % i for i in range(9)])
    client, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", hops)]), TRUSTED)
    assert (client, source) == ("", "xff_too_long")


def test_XFF畸形被拒():
    _, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", "not-an-ip")]), TRUSTED)
    assert source == "xff_malformed"


def test_重复XFF头被拒():
    _, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", "1.2.3.4"),
                               ("x-forwarded-for", "5.6.7.8")]), TRUSTED)
    assert source == "xff_duplicated"


def test_本机直连无XFF():
    client, source = resolve_client(_request("127.0.0.1"), TRUSTED)
    assert (client, source) == ("127.0.0.1", "peer_loopback_no_xff")


def test_IPv6回环被识别():
    client, source = resolve_client(_request("::1"), TRUSTED)
    assert source == "peer_loopback_no_xff"


def test_IPv4映射回环被归一化():
    client, source = resolve_client(_request("::ffff:127.0.0.1"), TRUSTED)
    assert client == "127.0.0.1"
    assert source == "peer_loopback_no_xff"


def test_client缺失():
    _, source = resolve_client(_request(None), TRUSTED)
    assert source == "client_missing"


def test_代理特征头但无XFF判为不可信():
    """peer 回环 + 有代理特征头 + 无 XFF → 不能当本机放行（fail-closed）。"""
    client, source = resolve_client(_request("127.0.0.1", [("via", "1.1 nginx")]), TRUSTED)
    assert (client, source) == ("127.0.0.1", "proxy_hints_no_xff")


# ---------------------------------------------------------------- 7.2 代理边界

def _probe_app():
    app = FastAPI()

    @app.get("/api/v1/state")
    async def state():
        return {"ok": True}

    install_security(app, auth_mode=AUTH_MODE_LOOPBACK, extra_hosts=["core"],
                     trusted_proxies="127.0.0.1,::1")
    return app


def _get(app, peer, headers=None):
    async def scenario():
        transport = httpx.ASGITransport(app=app, client=(peer, 40000) if peer else None)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as http:
            return await http.get("/api/v1/state", headers=headers or {})

    return asyncio.run(scenario())


def test_本机直连放行():
    assert _get(_probe_app(), "127.0.0.1").status_code == 200


def test_本机反代本机客户端放行():
    assert _get(_probe_app(), "127.0.0.1",
                {"X-Forwarded-For": "127.0.0.1"}).status_code == 200


def test_本机反代远程用户被拒():
    resp = _get(_probe_app(), "127.0.0.1", {"X-Forwarded-For": "203.0.113.9"})
    assert resp.status_code == 403


def test_远程直连被拒():
    assert _get(_probe_app(), "203.0.113.9").status_code == 403


def test_远程伪造XFF仍被拒():
    assert _get(_probe_app(), "203.0.113.9",
                {"X-Forwarded-For": "127.0.0.1"}).status_code == 403


def test_重复XFF头返回400():
    app = _probe_app()

    async def scenario():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as http:
            return await http.get("/api/v1/state", headers=[
                ("X-Forwarded-For", "1.2.3.4"), ("X-Forwarded-For", "5.6.7.8")])

    assert asyncio.run(scenario()).status_code == 400


# ---------------------------------------------------------------- 7.5 启动校验

def test_uvicorn版本不符拒绝启动(monkeypatch):
    import main

    monkeypatch.setattr(uvicorn, "__version__", "0.99.0")
    with pytest.raises(SystemExit) as exc:
        main._serve()
    assert "uvicorn" in str(exc.value)


def test_拒绝reload启动(monkeypatch):
    import main

    monkeypatch.setattr(sys, "argv", ["main.py", "--reload"])
    with pytest.raises(SystemExit) as exc:
        main._serve()
    assert "reload" in str(exc.value)


def test_端口占用给出可读错误(monkeypatch):
    import main

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.listen(1)
    busy_port = probe.getsockname()[1]
    try:
        monkeypatch.setattr(main, "PORT", busy_port)
        with pytest.raises(SystemExit) as exc:
            main._serve()
        assert str(busy_port) in str(exc.value)
    finally:
        probe.close()


def test_非法可信代理配置拒绝启动():
    app = FastAPI()
    with pytest.raises(SystemExit) as exc:
        install_security(app, auth_mode=AUTH_MODE_LOOPBACK, trusted_proxies="127.0.0.1,oops")
    assert "TRUSTED_PROXIES" in str(exc.value)


def test_启动必须关闭uvicorn自带proxy_headers(monkeypatch):
    """uvicorn 默认会用 XFF 改写 request.client，必须关掉，否则我们二次解析。"""
    import main

    captured = {}

    class FakeConfig:
        def __init__(self, app, **kwargs):
            captured.update(kwargs)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    monkeypatch.setattr(main, "PORT", free_port)
    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server",
                        lambda cfg: (_ for _ in ()).throw(SystemExit("stop")))

    with pytest.raises(SystemExit):
        main._serve()
    assert captured.get("proxy_headers") is False, captured


def test_XFF只有逗号视为畸形():
    _, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", ",")]), TRUSTED)
    assert source == "xff_malformed"


def test_XFF带端口视为畸形():
    _, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", "1.2.3.4:1234")]), TRUSTED)
    assert source == "xff_malformed"


def test_恰好8跳仍然允许():
    hops = ", ".join(["10.0.0.{0}".format(i) for i in range(1, 9)])
    client, source = resolve_client(
        _request("127.0.0.1", [("x-forwarded-for", hops)]), TRUSTED)
    assert source == "xff" and client == "10.0.0.8"


def test_只有Forwarded头被拒():
    assert _get(_probe_app(), "127.0.0.1", {"Forwarded": "for=203.0.113.9"}).status_code == 403


def test_只有XRealIP头被拒():
    assert _get(_probe_app(), "127.0.0.1", {"X-Real-IP": "203.0.113.9"}).status_code == 403


# ---------------------------------------------------------------- 启动入口

def test_拒绝uvicorn_CLI启动(monkeypatch):
    import main

    monkeypatch.setattr(sys, "argv", ["uvicorn", "main:app", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit) as exc:
        main._guard_launch_mode()
    assert "python main.py" in str(exc.value)


def test_拒绝命令行host参数(monkeypatch):
    import main

    monkeypatch.setattr(sys, "argv", ["main.py", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit) as exc:
        main._guard_launch_mode()
    assert "CORE_HOST" in str(exc.value)


def test_拒绝命令行proxy_headers(monkeypatch):
    import main

    monkeypatch.setattr(sys, "argv", ["main.py", "--proxy-headers"])
    with pytest.raises(SystemExit):
        main._guard_launch_mode()


def test_正常启动参数放行(monkeypatch):
    import main

    monkeypatch.setattr(sys, "argv", ["main.py"])
    main._guard_launch_mode()      # 不应抛


# ---------------------------------------------------------------- 自审补充

def test_非可信peer带代理特征头判为不可信():
    """TRUSTED_PROXIES 配空时，本机反代的请求不能静默当直连。"""
    client, source = resolve_client(
        _request("127.0.0.1", [("via", "1.1 nginx")]), frozenset())
    assert (client, source) == ("127.0.0.1", "untrusted_proxy_hints")


def test_可信代理配空时代理请求被拒():
    app = FastAPI()

    @app.get("/api/v1/state")
    async def state():
        return {"ok": True}

    install_security(app, auth_mode=AUTH_MODE_LOOPBACK, extra_hosts=["core"],
                     trusted_proxies="")
    assert _get(app, "127.0.0.1", {"Via": "1.1 nginx"}).status_code == 403
    assert _get(app, "127.0.0.1").status_code == 200


def test_pytest参数不会误触启动守卫(monkeypatch):
    import main

    monkeypatch.setattr(sys, "argv",
                        ["pytest", "packages/core-engine/tests", "-q", "-k", "auth"])
    main._guard_launch_mode()      # 不应抛


def test_uvicorn_CLI在真实进程中被拒绝():
    """端到端锁：真起 uvicorn，确认进程非零退出且提示改用 python main.py。"""
    import os
    import subprocess

    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", "0"],
        cwd=os.path.join(src_dir, "src"),
        capture_output=True, text=True, timeout=90,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, combined[-500:]
    assert "python main.py" in combined, combined[-500:]
