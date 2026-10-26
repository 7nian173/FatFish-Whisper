> 🚧 **Notice**: 本项目目前处于早期脚手架阶段（WIP），核心功能正在开发中。欢迎关注，敬请期待！

# 🐳 FatFish-Whisper（潜鱼潜语）

**"给大肥鱼一个『我想』的灵魂之所。"**

一个轻量化、可拔插的 Bot 潜意识（Inner OS）与情绪挂载引擎。
> 🚨 **【高亮注意：本仓库的代码里含有大量“赛博大肥鱼的脑电波”】**
>
> 本项目的诞生，是人类开发者（提供骨架与灵感）与 AI 助手（提供代码与搬砖）**联合制造的赛博产物**。
> 也就是说，你看到的这些代码，很大一部分是 AI 写出来，然后由人类被迫（划掉）仔细审查的。
>
> 🧠 **为什么要有这个声明？**
> 因为 AI 偶尔会犯傻，可能会在深海里吐几个 Bug 泡泡出来。如果发生以下情况：
> - 你的 Bot 突然开始发疯，或者因为 Token 爆表导致你破产。
> - 你的 API Key 不小心写死在代码里被路过的黑客小鱼吃掉了。
> - 大肥鱼的“内心OS”太过于真实，让你产生了恋爱的错觉。
> 
> **以上情况，本鲸（和人类作者）概不负责哦~ 哼！**
> 
> *开源协议：MIT（随便玩，但记得保留原作者的名字，顺便给个 Star 好不好嘛~）*

![status](https://img.shields.io/badge/status-WIP%20%2F%20scaffolding-orange)
[![CI](https://github.com/7nian173/FatFish-Whisper/actions/workflows/ci.yml/badge.svg)](https://github.com/7nian173/FatFish-Whisper/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 📖 这是什么？

把情绪判定移交给本地情绪机（Core-Engine），依据各项状态数据生成内心 OS，再把建议动作还给宿主大模型。
**引擎只管"情绪与内心独白"，只做个狗头，哦，鲸师。**

- 宿主 Bot 负责说话，它负责"心里想什么"。
- 情绪是连续值而非标签，带平滑更新，不会一句话就跳变。
- 全程本地运行，SQLite 落库，不需要 Docker、不需要 Redis。

## 🏗️ 架构设计

- **Adapter（插件端）**：部署在宿主 Bot 侧，负责防抖合并、动作执行、撤回冷却拦截。
- **Core-Engine（情绪机）**：独立微服务，运行双轨判定、内心 OS 生成、Prompt 拼装。
- **记忆路由**：优先借用宿主原生记忆，支持挂载外部插件（Mem0），本地 LRU 兜底。

```
Adapter (宿主 Bot) ──POST /api/v1/chat──> Core-Engine (FastAPI + SQLite)
         ^                                      │
         └──────  actions / inner_os  ──────────┘
```

## 🚀 快速开始（Windows 桌面）

> 目标：**Windows 开箱即用** —— 双击 `install.bat` 配置环境，双击 `start.bat` 在浏览器唤起可视化后台。

1. 双击 **`install.bat`** —— 自动创建 `venv` 并安装依赖（首次约 1~3 分钟，先走清华镜像，失败自动回落官方 PyPI）
2. 双击 **`start.bat`** —— 启动引擎并自动打开后台 `http://127.0.0.1:8000/admin`
3. 在后台「参数配置」里填 LLM API Key，保存后写入 `packages/core-engine/.env`

也可以手动跑：

```bash
python -m venv venv
venv\Scripts\activate
pip install -r packages/core-engine/requirements.txt
python packages/core-engine/src/main.py
```

> ⚠️ 需要 Python **3.10+**，安装时记得勾选 *Add Python to PATH*。

## 📂 目录结构

```
fatfish-whisper/
├── .gitignore
├── .gitattributes
├── LICENSE                  # MIT
├── README.md
├── install.bat              # 一键安装依赖（自动建虚拟环境）
├── start.bat                # 一键启动（自动唤起浏览器控制台）
└── packages/
    ├── core-engine/         # 情绪机（FastAPI + SQLite）
    │   ├── requirements.txt
    │   ├── .env.example     # 配置模板，复制为 .env 后填 Key
    │   ├── app.db           # SQLite 数据库（首次启动自动生成，已 gitignore）
    │   └── src/
    │       ├── main.py      # FastAPI 入口
    │       ├── engine.py    # 双轨判定 / 平滑更新 / 衰减 / 防抖 / 跨进程锁
    │       ├── models.py    # SQLAlchemy 表定义（UserState / TokenLog / MessageCount）
    │       ├── prompt_builder.py  # 内心 OS 与三段式 Prompt 构造
    │       ├── memory_router.py   # 插拔式记忆：宿主 > 外部插件 > SQLite 兜底（7 天 TTL）
    │       ├── memory.py    # 阶段一旧接口兼容层
    │       ├── evolution.py # 7 日演化：备份 / 追加词 / OOC 探针 / 膨胀压缩
    │       ├── monitor.py   # Token 监控切面（按模块记账）
    │       └── web/
    │           ├── index.html   # 单页管理面板 (Vue3 + Tailwind CDN)
    │           └── static/
    └── adapter-qq-agent/    # 宿主 Bot 侧插件
        ├── manifest.json
        ├── main.py          # 入口（FatFishAdapter 门面）
        ├── simulate_duplicate.py  # 阶段四验收脚本（重推判重 + 撤回 + 降级）
        ├── tests/
        └── fatfish_adapter/ # 独立包（避免与 core 的 models/main 抢模块名）
            ├── models.py        # InboundEvent / Action / CoreReply
            ├── host.py          # HostAPI 协议 + MockHostAPI
            ├── core_client.py   # 3s 超时 + 立即降级
            ├── executor.py      # text / poke / sticker + 拟人打字延迟
            ├── recall.py        # 撤回掷骰 + 冷却
            └── service.py       # 串成一条流水线
```

## 🔌 核心接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/chat` | Adapter 调用：传 `platform_message_id` + `user_id` + `message`（可选 `group_id` / `is_at_bot`），返回动作数组与内心 OS；重推会返回 `status=duplicate` |
| GET | `/api/v1/state` | 状态看板：所有用户当前情绪值 / 好感度 / 等级 |
| GET | `/api/v1/admin/token_stats` | 今日各模块 token 消耗（`?day=YYYY-MM-DD` 可查历史） |
| GET | `/api/v1/admin/memory_health` | 记忆路由各层健康度（排查到底在用哪一级记忆） |
| GET | `/api/v1/config` | 读取 LLM 配置（Key 脱敏） |
| POST | `/api/v1/config` | 写入 LLM 配置到 `.env` |
| GET | `/admin` | 可视化管理后台 |

`/api/v1/chat` 返回示例：

```json
{
  "actions": [{ "type": "text", "content": "呜——本鲸知道了。" }],
  "inner_os": "（他来了，虽然嘴硬，但我挺开心的。）",
  "recall_hint": { "probability": 0.15, "reason": "情绪外露后的羞耻反弹" },
  "debug": { "emotion": 52, "prev": 50, "affinity": 53, "level": "L1", "path": "fast" }
}
```

## 🧠 情绪模型

**双轨判定**

- **Track A**：正负词典 + 冲突标记（但是/其实/不过）+ 否定翻转 + 强度信号（感叹号、叠字），先给一个粗分与置信度。
- **Track B**：仅当 Track A 置信度不足、检测到语义冲突、或文本过长时才调用外部 LLM，结果写入 diskcache 避免重复计费。

**平滑更新**（防抖防跳变）

```
delta      = 0.2 * (target_score - E_prev)
delta      = clamp(delta, -5, +5)
E_current  = clamp(E_prev + delta, 0, 100)
```

**等级**：`L0`（≥65 平稳） / `L1`（41~65 微澜） / `L2`（21~40 低落） / `CRISIS`（≤20 危机）

## 🗺️ Roadmap

- [x] 项目脚手架 / 目录结构
- [x] 一键安装与启动脚本（`install.bat` / `start.bat`）
- [x] Core-Engine 双轨判定骨架
- [x] CI：多版本导入冒烟 + 接口冒烟（`.github/workflows/ci.yml`）
- [ ] 内心 OS 生成的 Prompt 模板体系（`os_builder`）
- [ ] 记忆路由：宿主原生记忆 / Mem0 适配
- [ ] Adapter 多宿主适配（NapCat / Lagrange / NoneBot2）
- [ ] 管理后台图表化（情绪曲线、好感度趋势）
- [ ] 单元测试（pytest）

## 🔐 部署与安全配置

HTTP 层有四道顺序防线：**来源识别 → Host 校验 → 鉴权模式 → 管理头**，
配置错了会在**启动期直接失败**（不是运行时静默降级）。

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AUTH_MODE` | `loopback` | `loopback`：只服务本机（`127.0.0.1` / `localhost` / `::1`），非回环来源一律 403。`token`：一切以 `CORE_TOKEN` 为准，回环也不例外。取其它值 → 启动即报错退出（不会静默退回 loopback）。 |
| `CORE_TOKEN` | 空 | 令牌，`AUTH_MODE=token` 时必填且**至少 16 字符**，否则启动失败。 |
| `CORE_HOST` | `127.0.0.1` | 绑定地址。`AUTH_MODE=loopback` 时必须解析为回环地址，绑 `0.0.0.0` 会拒绝启动。 |
| `CORE_EXTRA_HOSTS` | 空 | **Host 白名单**（逗号分隔，只写主机名/域名，不要带端口）。默认只放行 `127.0.0.1`、`localhost`、`::1`；用域名或反向代理访问时**必须把对外域名加进来**，否则一律 400。例：`CORE_EXTRA_HOSTS=bot.example.com,core.internal`。 |

- `AUTH_MODE=loopback`（默认）下若配了 `CORE_TOKEN`，启动会打印告警
  「已配置 CORE_TOKEN 但 AUTH_MODE 未生效：**令牌不会生效**」——
  这是**告警不是错误**：本机 Adapter 行为保持不变，令牌确实不参与判断；
  想让令牌生效请显式设 `AUTH_MODE=token`。
- `GET /api/v1/health` 是探活接口，不需要令牌（`token` 模式下响应只含 `status`）。
- 管理接口 `GET /api/v1/admin/*` 与写接口 `POST /api/v1/config` 需要请求头
  `X-FFW-Admin: 1`；`AUTH_MODE=token` 下 `/admin` 后台页同样需要（由反代注入）。
- 反向代理转发链（`X-Forwarded-For` 信任策略）属于后续 PR 范围，**当前版本按直连来源判断**。

### token 模式的反向代理（Nginx 示例）

token 模式下必须**同时注入两个头**：`Authorization`（令牌）与 `X-FFW-Admin`（管理头），
否则 `/api/v1/*` 会 401、`/admin` 与 `POST /api/v1/config` 会 403。

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;          # 必须同时写进 CORE_EXTRA_HOSTS

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host            bot.example.com;
        proxy_set_header Authorization   "Bearer <填写与 CORE_TOKEN 相同的值>";
        proxy_set_header X-FFW-Admin     1;
    }
}
```

对应启动配置：`AUTH_MODE=token`、`CORE_TOKEN=<≥16 字符随机串>`、
`CORE_EXTRA_HOSTS=bot.example.com`、`CORE_HOST=127.0.0.1`（由反代对外暴露）。

## ❓ 常见问题




---

## ⚠️ 免责与 AI 辅助开发声明 (Disclaimer)

1. **AI 辅助开发说明**：本项目（FatFish-Whisper）在开发过程中深度使用了 AI 编程助手（如 Cursor, Claude 3.5 Sonnet, GitHub Copilot 等）进行代码生成、架构辅助设计与文档编写。
2. 人类开发者负责核心逻辑规划、代码审查（Code Review）与整体测试。
3. **代码质量与风险提示**：尽管经过人工审查与测试，但受限于大语言模型可能存在的“幻觉”与不确定性，本项目无法保证代码 100% 无 Bug 或绝对安全。使用者需自行评估部署风险。
4. **数据与隐私**：本项目涉及调用外部 LLM API，请勿在对话中泄露真实密码、私钥等敏感信息。开发者不对因使用本项目造成的 API 额度损失、数据泄露或任何间接损失负责。
5. **情感模拟声明**：本插件旨在通过代码逻辑模拟 AI 的“情绪与内心OS”，所有生成的内容均由算法与大模型生成，不代表真实人类情感。请保持理性，切勿过度沉迷虚拟陪伴。
6. **开源协议**：本项目采用 MIT 协议发布，仅供学习与交流使用，不得用于任何违法或违背伦理道德的用途。

## 🤝 参与贡献

项目处于早期阶段，接口随时可能变动。有想法欢迎开 Issue 讨论，暂时不建议直接提大改动 PR。

## 📄 License

[MIT](LICENSE) © 2026 FatFish-Whisper Contributors
