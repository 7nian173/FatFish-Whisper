"""HTTP 层安全加固（PR1：鉴权缺陷修复）。

四道防线，顺序不可调换
----------------------
1. **来源识别**：解析真实客户端；``request.client`` 缺失或非法一律拒绝。
2. **Host 校验**：防 DNS rebinding——浏览器里任意网页都能把域名解析到 127.0.0.1。
3. **鉴权模式**：
   - ``loopback``（默认）：只服务本机客户端，非回环一律 403。
   - ``token``：一切以 ``CORE_TOKEN`` 为准，回环也不例外。
4. **管理头**：``X-FFW-Admin: 1`` 防跨站读取（跨站带自定义头会触发预检，
   本服务无 CORS 头 → 浏览器拦截）。

历史缺陷（本次修复）
--------------------
旧版 ``token_ok`` 在 ``expected`` 为空时直接 ``return True``，导致
「非回环必须持令牌」恒不成立。现在拆出 :func:`token_ok_strict`：空令牌返回 **False**。

注意：本模块**不引入 CORS 中间件**。没有 CORS 头本身就是一道防线。
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import socket
from typing import Final, Iterable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("ffw.security")

#: 鉴权模式
AUTH_MODE_LOOPBACK: Final[str] = "loopback"
AUTH_MODE_TOKEN: Final[str] = "token"
AUTH_MODES: Final[tuple[str, ...]] = (AUTH_MODE_LOOPBACK, AUTH_MODE_TOKEN)

#: 允许的 Host 主机名（不含端口）。用 localhost / 回环字面量就够。
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: 不需要令牌就能访问的路径（探活）；loopback 模式下仍需回环来源
OPEN_PATHS: Final[tuple[str, ...]] = ("/api/v1/health",)

#: 管理请求头（防跨站读取）
ADMIN_HEADER: Final[str] = "x-ffw-admin"
ADMIN_HEADER_VALUE: Final[str] = "1"

#: 需要管理头的路径前缀（任何方法）
ADMIN_PATH_PREFIXES: Final[tuple[str, ...]] = ("/api/v1/admin",)

#: 写操作需要管理头的路径
PROTECTED_WRITE_PATHS: Final[tuple[str, ...]] = ("/api/v1/config",)

#: 令牌请求头候选（二选一）
TOKEN_HEADERS: Final[tuple[str, ...]] = ("x-core-token", "authorization")

#: 令牌最短长度
MIN_TOKEN_LENGTH: Final[int] = 16

#: X-Forwarded-For 的最大跳数（防超长头 DoS）
MAX_XFF_HOPS: Final[int] = 8

#: 出现任一「代理特征头」→ 说明前面很可能有代理（用于启发式审计）
PROXY_HINT_HEADERS: Final[tuple[str, ...]] = (
    "via", "x-forwarded-proto", "x-forwarded-host", "x-real-ip", "forwarded",
)

#: 默认可信代理（只信任本机反代）
DEFAULT_TRUSTED_PROXIES: Final[str] = "127.0.0.1,::1"


# ---------------------------------------------------------------- IP 归一化

def normalize_ip(value: object) -> Optional[str]:
    """把 IP 字符串归一化；非法返回 None。

    重点处理 **IPv4-mapped IPv6**：Windows 双栈下 ``request.client.host`` 可能是
    ``::ffff:127.0.0.1``，而 ``ipaddress.ip_address(...).is_loopback`` 对它是 **False**。
    不归一化会把本机请求误判成远程。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def is_loopback_ip(value: object) -> bool:
    normalized = normalize_ip(value)
    if normalized is None:
        return False
    return ipaddress.ip_address(normalized).is_loopback


def parse_trusted_proxies(raw: object) -> frozenset[str]:
    """把逗号分隔的代理地址解析成 IP 集合；任一条非法直接抛 ValueError。"""
    text = "" if raw is None else str(raw)
    result: set[str] = set()
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        normalized = normalize_ip(item)
        if normalized is None:
            raise ValueError("TRUSTED_PROXIES 含非法地址：{0!r}".format(item))
        result.add(normalized)
    return frozenset(result)


# ---------------------------------------------------------------- Host / 令牌

def _host_name(host_header: str) -> str:
    """从 Host 头里取出主机名（兼容 ``[::1]:8000`` / ``127.0.0.1:8000``）。"""
    value = (host_header or "").strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    return value.split(":")[0]


def host_allowed(host_header: str, extra_hosts: Iterable[str] = ()) -> bool:
    name = _host_name(host_header)
    if not name:
        return False
    allowed = set(LOOPBACK_HOSTS)
    allowed.update(str(h).strip().lower() for h in extra_hosts if str(h).strip())
    return name in allowed


def client_is_loopback(client_host: object) -> bool:
    """兼容旧调用：localhost 视为回环，其余走 IP 判定。"""
    text = "" if client_host is None else str(client_host).strip()
    if text == "localhost":
        return True
    return is_loopback_ip(text)


def _extract_token(request: Request) -> str:
    for header in TOKEN_HEADERS:
        raw = request.headers.get(header, "")
        if not raw:
            continue
        if header == "authorization" and raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return raw.strip()
    return ""


def token_ok_strict(request: Request, expected: str) -> bool:
    """严格令牌校验：**未配置令牌一律 False**（修掉旧版的短路放行）。

    注意：``hmac.compare_digest`` 对**非 ASCII 的 str** 会抛 ``TypeError``。
    Starlette 用 latin-1 解码头，客户端发 ``Authorization: Bearer 令`` 这类值
    就能把服务打成 500。所以统一编码成 bytes 再比较，任何异常都视为不匹配。
    """
    if not expected:
        return False
    got = _extract_token(request)
    if not got:
        return False
    try:
        return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


# ---------------------------------------------------------------- 客户端解析

def _has_proxy_hints(request: Request) -> bool:
    return any(request.headers.get(h) for h in PROXY_HINT_HEADERS)


def resolve_client(request: Request, trusted: frozenset[str]) -> tuple[str, str]:
    """解析真实客户端，返回 ``(ip, source)``。

    XFF 只从**右往左**取：最左端由客户端控制、可任意伪造。
    仅当直连 peer 本身可信时，才采信它追加的 XFF。
    """
    raw_peer = request.client.host if request.client else None
    if raw_peer is None:
        return "", "client_missing"
    peer = normalize_ip(raw_peer)
    if peer is None:
        return "", "peer_invalid"

    if peer not in trusted:
        # 非可信来源：XFF 一律忽略。
        # 但若带代理特征头，说明前面确实有代理而它没被列入 TRUSTED_PROXIES
        # （典型：有人把 CORE_TRUSTED_PROXIES 设成空）。此时不能静默当直连。
        if _has_proxy_hints(request):
            return peer, "untrusted_proxy_hints"
        return peer, "peer"

    values = request.headers.getlist("x-forwarded-for")
    if len(values) > 1:
        return "", "xff_duplicated"  # 重复头只出现在异常/恶意请求里

    raw = values[0] if values else ""
    if not raw.strip():
        if is_loopback_ip(peer):
            # 本机直连（adapter / 插件 / 本机脚本）。
            # 但若带代理特征头，说明前面**确实**有代理而它没设 XFF —— 这时不能当本机放行，
            # 否则远程用户会以 127.0.0.1 的身份进来（fail-closed）。
            if _has_proxy_hints(request):
                return peer, "proxy_hints_no_xff"
            return peer, "peer_loopback_no_xff"
        return peer, "proxy_without_xff"

    hops = [h.strip() for h in raw.split(",") if h.strip()]
    if not hops:
        return "", "xff_malformed"      # 只有逗号/空白，视为畸形
    if len(hops) > MAX_XFF_HOPS:
        return "", "xff_too_long"
    normalized = [normalize_ip(h) for h in hops]
    if any(n is None for n in normalized):
        return "", "xff_malformed"

    for hop in reversed(normalized):
        if hop not in trusted:
            return hop, "xff"

    # 所有 hop 都可信：要么反代把本机客户端记成了 127.0.0.1，要么反代用
    # `$http_x_forwarded_for` 透传了客户端自带的头（攻击者可伪造）。
    # 无法区分，只能告警并回落到 peer。
    logger.warning(
        "X-Forwarded-For 全部为可信地址（%s）：请确认反代使用 "
        "$proxy_add_x_forwarded_for 而不是 $http_x_forwarded_for", normalized,
    )
    return peer, "xff_all_trusted"


# ---------------------------------------------------------------- 启动校验

def _resolved_addresses(host: str, port: int = 0) -> set[str]:
    """解析主机名到 IP 集合；解析失败抛 OSError。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: set[str] = set()
    for info in infos:
        normalized = normalize_ip(info[4][0])
        if normalized:
            out.add(normalized)
    return out


def verify_bind_policy(
    host: str,
    mode: str,
    token: str,
    *,
    port: int = 0,
    resolver=_resolved_addresses,
) -> None:
    """启动期策略校验。不满足即 **抛 SystemExit**（fail-fast）。

    ⚠️ 这里刻意不用 ``assert``：``python -O`` 会把 assert 整条抹掉，保护随之消失。
    """
    host = (host or "").strip() or "127.0.0.1"
    mode = (mode or AUTH_MODE_LOOPBACK).strip().lower()

    if mode not in AUTH_MODES:
        raise SystemExit(
            "[FATAL] AUTH_MODE={0!r} 非法，可选：{1}".format(mode, " / ".join(AUTH_MODES))
        )

    if mode == AUTH_MODE_TOKEN:
        if not token:
            raise SystemExit(
                "[FATAL] AUTH_MODE=token 但未配置 CORE_TOKEN。"
                "请设置环境变量 CORE_TOKEN（≥{0} 字符）后重试。".format(MIN_TOKEN_LENGTH)
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise SystemExit(
                "[FATAL] CORE_TOKEN 过短（{0} < {1}），请使用更长的随机串。".format(
                    len(token), MIN_TOKEN_LENGTH
                )
            )
        return

    # loopback 模式：绑定地址必须是回环
    literal = normalize_ip(host)
    if literal is not None:
        if not ipaddress.ip_address(literal).is_loopback:
            raise SystemExit(
                "[FATAL] AUTH_MODE=loopback 但 CORE_HOST={0} 非回环。\n"
                "        本机自用请设 CORE_HOST=127.0.0.1；\n"
                "        需要对外提供服务请设 AUTH_MODE=token 并配置 CORE_TOKEN。".format(host)
            )
        return

    # 主机名：解析后逐一检查
    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise SystemExit(
            "[FATAL] 无法解析 CORE_HOST={0}：{1}".format(host, exc)
        ) from exc
    if not addresses:
        raise SystemExit("[FATAL] CORE_HOST={0} 未解析出任何地址".format(host))
    bad = sorted(a for a in addresses if not ipaddress.ip_address(a).is_loopback)
    if bad:
        raise SystemExit(
            "[FATAL] AUTH_MODE=loopback 但 CORE_HOST={0} 解析到非回环地址 {1}。\n"
            "        本机自用请设 CORE_HOST=127.0.0.1；\n"
            "        需要对外提供服务请设 AUTH_MODE=token 并配置 CORE_TOKEN。".format(host, bad)
        )


# ---------------------------------------------------------------- 中间件

def _needs_admin_header(path: str, method: str, mode: str) -> bool:
    """按规则矩阵判断是否需要 ``X-FFW-Admin``。"""
    if path.startswith("/static"):
        return False                      # 子资源（<script>/<link>）加不了自定义头
    if path.startswith(ADMIN_PATH_PREFIXES):
        return True                       # 管理接口：GET 也要
    if path in PROTECTED_WRITE_PATHS and method in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    if path.startswith("/admin") and mode == AUTH_MODE_TOKEN:
        return True                       # token 模式下的后台页（由反代注入）
    return False


def install_security(
    app,
    *,
    bound_host: str = "127.0.0.1",
    token: Optional[str] = None,
    extra_hosts: Iterable[str] = (),
    auth_mode: Optional[str] = None,
    trusted_proxies: Optional[str] = None,
) -> dict:
    """给 FastAPI 应用装上来源识别 / Host 校验 / 鉴权 / 管理头。"""
    expected = (token if token is not None else os.environ.get("CORE_TOKEN", "")).strip()
    extra = tuple(str(h).strip().lower() for h in extra_hosts if str(h).strip())
    bound = (bound_host or "127.0.0.1").strip()
    # raw 保留「是否显式配置」的信息：None / 空串 = 调用方没给、环境变量也没有
    raw_mode = auth_mode if auth_mode is not None else os.environ.get("AUTH_MODE")
    mode = (raw_mode or AUTH_MODE_LOOPBACK).strip().lower()

    # 可信代理：解析失败直接拒绝启动，不静默忽略
    trusted_raw = (trusted_proxies if trusted_proxies is not None
                   else os.environ.get("CORE_TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES))
    try:
        trusted = parse_trusted_proxies(trusted_raw)
    except ValueError as exc:
        raise SystemExit("[FATAL] CORE_TRUSTED_PROXIES 非法：{0}".format(exc)) from exc

    # 非法模式**不能静默降级**：否则配错 AUTH_MODE 会悄悄退回 loopback 语义。
    # 与 verify_bind_policy 保持同一策略（两边都拒绝）。
    if mode not in AUTH_MODES:
        raise SystemExit(
            "[FATAL] AUTH_MODE={0!r} 非法，可选：{1}".format(mode, " / ".join(AUTH_MODES))
        )

    # 是否走了默认值（调用方没传、环境变量里也没设）
    mode_implicit = raw_mode is None or not str(raw_mode).strip()

    # 配了令牌却没开 token 模式 = 令牌被静默忽略（安全降级），必须显式告警
    token_ignored = bool(expected) and mode == AUTH_MODE_LOOPBACK
    if token_ignored:
        if mode_implicit:
            logger.warning(
                "已配置 CORE_TOKEN 但未显式设置 AUTH_MODE（当前默认 %s）："
                "**令牌不会生效**，请求只按来源地址判断。"
                "如需令牌鉴权请设 AUTH_MODE=token；如确实只服务本机 Adapter，可忽略本提示。",
                AUTH_MODE_LOOPBACK,
            )
        else:
            logger.warning(
                "已配置 CORE_TOKEN 但 AUTH_MODE=%s：**令牌不会生效**。"
                "如需令牌鉴权请改设 AUTH_MODE=token；如确实只服务本机，可忽略本提示。",
                AUTH_MODE_LOOPBACK,
            )

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        # 1) 来源识别（含 XFF 右起解析）
        client, source = resolve_client(request, trusted)
        if source in ("client_missing", "peer_invalid"):
            return JSONResponse({"detail": "无法识别请求来源"}, status_code=403)
        if source in ("xff_malformed", "xff_too_long", "xff_duplicated"):
            return JSONResponse({"detail": "X-Forwarded-For 非法"}, status_code=400)
        if source == "proxy_without_xff":
            # 非回环可信代理却漏设 XFF：记为部署错误，但后续仍按非回环拒绝
            logger.warning("可信代理未携带 X-Forwarded-For（peer=%s）", client)
        if source in ("proxy_hints_no_xff", "untrusted_proxy_hints") and mode == AUTH_MODE_LOOPBACK:
            # peer 是回环但请求带代理特征头却没 XFF：无法确认真实客户端，
            # 一律拒绝（fail-closed），否则远程用户会以 127.0.0.1 身份进来。
            return JSONResponse(
                {"detail": "检测到代理特征头但无法确认真实客户端（缺少 X-Forwarded-For "
                           "或代理未列入 CORE_TRUSTED_PROXIES），拒绝访问"},
                status_code=403,
            )

        # 2) Host 校验：防 DNS rebinding
        if not host_allowed(request.headers.get("host", ""), extra):
            return JSONResponse({"detail": "Host 不在允许列表内"}, status_code=400)

        path = request.url.path

        # 3) 鉴权模式
        if mode == AUTH_MODE_TOKEN:
            if path not in OPEN_PATHS and not token_ok_strict(request, expected):
                return JSONResponse({"detail": "缺少或错误的令牌"}, status_code=401)
        else:
            if not is_loopback_ip(client):
                return JSONResponse(
                    {"detail": "仅允许本机访问（AUTH_MODE=loopback）"}, status_code=403
                )

        # 4) 管理头：防跨站读取
        if _needs_admin_header(path, request.method, mode):
            if request.headers.get(ADMIN_HEADER, "") != ADMIN_HEADER_VALUE:
                return JSONResponse(
                    {"detail": "该接口需要 %s: %s 请求头" % (ADMIN_HEADER, ADMIN_HEADER_VALUE)},
                    status_code=403,
                )

        return await call_next(request)

    summary = {
        "auth_mode": mode,
        "auth_mode_implicit": mode_implicit,
        "host_check": True,
        "allowed_hosts": sorted(LOOPBACK_HOSTS | set(extra)),
        "token_required": bool(expected) and mode == AUTH_MODE_TOKEN,
        "token_ignored": token_ignored,
        "bound_host": bound,
        "admin_header_paths": list(ADMIN_PATH_PREFIXES) + list(PROTECTED_WRITE_PATHS),
        "trusted_proxies": sorted(trusted),
    }
    logger.info("安全策略已启用：%s", summary)
    return summary
