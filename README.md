> 🚧 **Notice**: 本项目目前处于早期脚手架阶段（WIP），核心功能正在开发中。欢迎关注，敬请期待！

# 🐳 FatFish-Whisper（潜鱼潜语）

**"给大肥鱼一个『我想』的灵魂之所。"**

一个轻量化、可拔插的 Bot 潜意识（Inner OS）与情绪挂载引擎。

![status](https://img.shields.io/badge/status-WIP%20%2F%20scaffolding-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 📖 这是什么

把情绪判定移交给本地情绪机（Core-Engine），依据各项状态数据生成内心 OS，
再把建议动作还给宿主大模型。**引擎只管"情绪与内心独白"，不抢大模型的嘴。**

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
    │       ├── engine.py    # 双轨判定 / 平滑更新 / 防抖
    │       ├── memory.py    # 本地轻量记忆 + 外部记忆挂载点
    │       ├── models/
    │       │   └── db_models.py
    │       └── web/
    │           ├── index.html   # 单页管理面板 (Vue3 + Tailwind CDN)
    │           └── static/
    └── adapter-qq-agent/    # 宿主 Bot 侧插件
        ├── manifest.json
        └── main.py
```

## 🔌 核心接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/chat` | Adapter 调用：传入 `user_id` + `message`，返回动作数组与内心 OS |
| GET | `/api/v1/state` | 状态看板：所有用户当前情绪值 / 好感度 / 等级 |
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

- **Track A（代码轨，毫秒级）**：正负词典 + 冲突标记（但是/其实/不过）+ 否定翻转 + 强度信号（感叹号、叠字），先给一个粗分与置信度。
- **Track B（LLM 轨，按需）**：仅当 Track A 置信度不足、检测到语义冲突、或文本过长时才调用外部 LLM，结果写入 diskcache 避免重复计费。

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
- [ ] 内心 OS 生成的 Prompt 模板体系（`os_builder`）
- [ ] 记忆路由：宿主原生记忆 / Mem0 适配
- [ ] Adapter 多宿主适配（NapCat / Lagrange / NoneBot2）
- [ ] 管理后台图表化（情绪曲线、好感度趋势）
- [ ] 单元测试与 CI

## ❓ 常见问题

**Q：启动报 `ModuleNotFoundError`？**
先跑一遍 `install.bat`，确认 `venv` 已创建成功。

**Q：后台能打开但回复没有情绪？**
`.env` 里的 `LLM_API_KEY` 还没填，Track B 会静默降级到代码轨。

**Q：`app.db` 需要提交吗？**
不需要，也**不应该**。它是运行期生成的本地数据，已在 `.gitignore` 中排除。

## ⚠️ 与原始脚手架的差异（有意为之）

原始文档里存在两版目录树与互相矛盾的骨架代码（`emotion.py` 用 Redis，但开发任务要求
"摒弃 Docker 和 Redis，改用纯 Python 和 SQLite"）。本次落地按**较新的"调整后"目录树**实现，
并做了如下收敛：

1. **未创建 `docker-compose.yml` / `redis`** —— 按文档要求转为 Windows 桌面版（SQLite + diskcache）。
2. **未创建 `docs/`** —— "调整后"目录树里已无该目录；如需要 `api-schema.md` / `memory-protocol.md` 可随时补。
3. **保留 `models/db_models.py`** —— 调整树未列出，但第 7 节提供了表结构骨架，引擎需要它落库。
4. **`requirements.txt` 使用下限版本而非硬钉死** —— 硬钉 `pydantic==2.5.2` 在本机 Python 3.14 上无预编译轮子、会触发源码编译失败。
5. **防抖改为 asyncio 队列 + diskcache** —— 单机场景下无需 Redis 分布式锁。

## 🤝 参与贡献

项目处于早期阶段，接口随时可能变动。有想法欢迎开 Issue 讨论，暂时不建议直接提大改动 PR。

## 📄 License

[MIT](LICENSE) © 2026 FatFish-Whisper Contributors
