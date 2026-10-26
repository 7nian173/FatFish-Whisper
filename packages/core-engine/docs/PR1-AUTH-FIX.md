# PR1 · HTTP 鉴权缺陷修复（报告 + 发布说明）

范围：`packages/core-engine/src/security.py`、`src/main.py`、`tests/test_auth_pr1.py`、
部署文档（README / `.env.example`）。不含 PR2（X-Forwarded-For 信任），不改产品功能与数据模型。

## 1. 原始缺陷

| # | 缺陷 | 后果 |
| --- | --- | --- |
| A | `token_ok` 在 `expected` 为空时直接 `return True` | 「非回环必须持令牌」恒不成立，鉴权规则被短路 |
| B | `request.client.host` 未处理 IPv4-mapped IPv6（`::ffff:127.0.0.1`） | Windows 双栈下**本机 Adapter 被误判为远程**而 403 |
| C | 绑定 `0.0.0.0` / `::` 且 `AUTH_MODE=loopback` 仍能启动 | 服务暴露到局域网，但语义是「只服务本机」 |
| D | 启动校验用 `assert` | `python -O` 会把断言整条抹掉，保护消失 |

修复：拆出 `token_ok_strict`（空令牌必 False）、`normalize_ip` 归一化、
`verify_bind_policy` 在**模块导入期** fail-fast（`SystemExit`，不用 `assert`）。

## 2. 复审（REVISE）补充项

| # | 要求 | 实现 |
| --- | --- | --- |
| 1 | `CORE_TOKEN` 已配、未显式设 `AUTH_MODE` 时不得静默忽略令牌 | `install_security` 启动即 `logger.warning`；摘要新增 `token_ignored` / `auth_mode_implicit`。区分两种文案：未显式设置（默认 loopback）与显式 `AUTH_MODE=loopback`。**本机 Adapter 行为保持不变**（仍是 loopback 语义，令牌不生效），只增加可见告警。 |
| 2 | 非 ASCII 令牌头不得让 `compare_digest` 抛 `TypeError` / 返回 500 | `token_ok_strict` 统一 `encode("utf-8")` 后比较，任何编码异常按「不匹配」处理 → 401，不 500。 |
| 3 | 非法 `AUTH_MODE` 必须显式报错，不得静默退回 loopback | `install_security` 与 `verify_bind_policy` 都抛 `SystemExit`；`main.py` 的 `verify_bind_policy` 在**模块级**执行，覆盖 `python main.py`、`uvicorn main:app`、`import main` 三条路径。 |
| 4 | 部署文档说明 `CORE_EXTRA_HOSTS`；token 模式反代同时注入两个头 | README 新增「部署与安全配置」章节（变量表 + Nginx 反代示例，示例里同时注入 `Authorization` 与 `X-FFW-Admin`）；`.env.example` 新增对应键；补「外部 Host 未列入白名单 → 400」测试。 |

## 3. 发布说明（升级注意）

- **不兼容变化**：`AUTH_MODE` 取值非法时服务**拒绝启动**（原来是静默按 loopback 处理）。
  升级前请确认部署脚本里的 `AUTH_MODE` 只写 `loopback` / `token`。
- **不兼容变化**：`AUTH_MODE=token` 但 `CORE_TOKEN` 为空或短于 16 字符 → 拒绝启动。
- **不兼容变化**：Host 校验默认只放行 `127.0.0.1` / `localhost` / `::1`。
  通过域名或反向代理访问的部署，**必须**配置 `CORE_EXTRA_HOSTS=<对外域名>`，否则全部请求 400。
- **新增告警（非错误）**：配了 `CORE_TOKEN` 却没开 token 模式时，启动日志出现
  「令牌不会生效」。只想本机自用可以忽略；要让令牌生效请设 `AUTH_MODE=token`。
- **行为修复**：本机 Adapter（Windows 双栈 `::ffff:127.0.0.1`）不再被误杀；
  token 模式下 `/admin`、`/api/v1/admin/*`、`POST /api/v1/config` 需要
  `X-FFW-Admin: 1`（token 模式下由反代注入）。
- 无需数据迁移，无回滚动作；如需回退，还原 `security.py` / `main.py` 两个文件即可
  （文档与测试为增量，可保留）。

## 4. 验证方式

```bash
# core-engine
python -m pytest packages/core-engine/tests -q
# 只跑鉴权用例
python -m pytest packages/core-engine/tests/test_auth_pr1.py -q

# adapter
python -m pytest packages/adapter-qq-agent/tests -q
```

覆盖点：IP 归一化、`token_ok_strict` 空令牌、loopback 放行/拒绝、管理头矩阵、
启动 fail-fast、令牌被忽略告警、非 ASCII 令牌（单元 + ASGI 层）、
导入期非法 `AUTH_MODE`、外部 Host 未白名单。

> 结果：本轮执行环境的 Bash 工具被拒（无交互审批通道），**测试未运行**；
> 上述命令与结果待审批方或下次派工时补齐（见 `inbox/1/submit.md`）。
