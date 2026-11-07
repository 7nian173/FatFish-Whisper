# 🐳 FatFish-Whisper（潜鱼潜语）

##目前在P3开发阶段，敬请期待

> "给大肥鱼一个『我想』的灵魂之所。"

基于 MCP 协议的旁路情感计算中间件（Sidecar）。它不接管消息收发，只负责内分泌计算、内心戏注入与状态门控。

宿主 Bot 负责说话，它负责"心里想什么"。把情绪判定移交给本地引擎，依据各项状态数据生成内心 OS，再把带有情绪惯性的提示词还给宿主大模型。

## ✨ 核心特性

- **六边形架构**：Core 层（`packages/core`）为绝对纯净的 Pydantic V2 + 纯函数，零 IO 依赖。
- **状态分离**：采用 `GlobalContext`（全局三层激素池）与 `S1Snapshot`（局部关系与情绪快照）物理隔离。
- **动态算力门控**：根据热度（`Effective_Heat`）与人设天花板系数，自动调节计算深度。
- **强一致落盘**：由编排层通过 SQLite 事务（双 CAS 乐观锁）保障全局与局部状态的原子性提交。

## 🚀 快速开始

需要 **Python ≥ 3.11**；依赖管理使用 [uv](https://github.com/astral-sh/uv)。

```bash
# 1. 安装依赖（--locked 保证与 uv.lock 逐字节一致）
#    web 测试所需的 fastapi / httpx / uvicorn 已在根的 dev 组里，
#    一次 sync 装齐，CI 与本地走同一条命令。
uv sync --locked

# 2. 运行全量测试（当前基线 853 passed）
uv run python -m pytest sys-mcp-whisper/tests -q

# 3. 运行四道架构门禁
uv run python scripts/check_imports.py      # 依赖方向：core 不得越权 import
uv run python scripts/check_composition.py  # 旧模块隔离
uv run python scripts/check_model_copy.py   # 禁止 model_copy(update=...)
uv run python scripts/check_demo_runner.py  # 端到端可跑
```

## 📂 目录结构

```text
.
├── .github/workflows/ci.yml   # CI：sync → ruff → 四门禁 → pytest
├── sys-mcp-whisper/           # 主工程（uv workspace 成员）
│   ├── packages/
│   │   ├── core/              # 纯逻辑核心，零 IO 零时钟
│   │   │   ├── domain/        # Pydantic 契约层
│   │   │   ├── engine/        # 纯函数引擎
│   │   │   └── ports/         # 6 条 Protocol
│   │   └── legacy_bridge/     # 旧模块的纯函数桥（唯一允许接触旧库的位置）
│   ├── apps/                  # 外围适配层
│   │   ├── adapters/          # inbound（DTO）/ outbound（SQLite）
│   │   ├── composition/       # 十六步编排与事务重试
│   │   └── entrypoints/       # /healthz
│   ├── tests/                 # 全量测试（853 条）
│   └── pyproject.toml         # 成员声明：dependencies 只有 pydantic
├── scripts/                   # 四道架构门禁
├── docs/                      # 架构与契约文档
├── pyproject.toml             # uv workspace 根配置 + dev 依赖组
└── uv.lock                    # 唯一锁文件（工作区模式下 uv 只认根锁）
```

## 📚 文档矩阵

| 文档 | 内容 |
| --- | --- |
| [01 · 项目总纲与架构设计](docs/01-Architecture.md) | 分层、三层激素池、动力学引擎、持久化与门禁 |
| [02 · 核心数据契约与 API 定义](docs/02-Contracts.md) | Pydantic 契约、端口签名、DDL、数值常量 |
| [03 · 编排流转与强事务规范](docs/03-Pipeline.md) | 十六步链路、双 CAS 与死信、限幅流水、MCP 对接 |

## 📄 License

[MIT](LICENSE) © 2026 7nian173
