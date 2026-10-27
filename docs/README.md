# FatFish-Whisper 文档索引

本目录存放**跨包、面向实现者**的规格与决策文档。包内（`packages/*/docs/`）只放该包自身的历史阶段记录，不作为现行规格依据。

## 权威规格

| 文档 | 内容 | 状态 |
|---|---|---|
| [specs/V5.0-core-spec.md](specs/V5.0-core-spec.md) | **唯一权威规格书**：设计哲学、三大死线、端到端数据流、47 功能点逐条落地、两条链路逐环节审查、8 个验收用例、四个里程碑 | 定稿候选 |
| [specs/incremental_discussions.md](specs/incremental_discussions.md) | 68 条增量（`I-01`~`I-68`）的讨论原文、代码现状证据、裁决变更记录 | 随规格书维护 |
| [specs/affinity-migration.md](specs/affinity-migration.md) | 好感度刻度统一（0~200）、四套刻度的收敛方案、`user_states` 复合主键迁移脚本与回滚 | 待实施 |
| [specs/filtered_out_log.md](specs/filtered_out_log.md) | 169 条对话收藏项的三级过滤记录，说明每条为何被保留或剔除 | 归档，不再更新 |
| [specs/diagrams/](specs/diagrams/README.md) | 规格书配图：Mermaid 源（`.mmd`）+ 已渲染 SVG/PNG，含重导出与校验脚本 | 与规格书同步 |

## 分工声明

- **`docs/`（本目录）**：跨包契约、架构决策、迁移方案、验收标准。多个包都要遵守的规则写在这里。
- **`packages/core-engine/docs/`**：仅该包自身的历史阶段记录（如 `PHASE3-PROMPT.md` 的防抖队列开发提示词、`PR1-AUTH-FIX.md` 的鉴权缺陷报告）。**不作为现行规格**，其中的缺陷已修，仅保留追溯价值。
- **`packages/adapter-qq-agent/`**：无独立 `docs/`。Adapter 的契约由 `docs/specs/V5.0-core-spec.md` §3.1 与 §5.2 定义。

## 权威性规则

1. 规格书与包内历史文档冲突 → **以规格书为准**。
2. 规格书与代码实际行为冲突 → **以规格书为准，并记为代码缺陷**（见规格书 §5 链路审查的 ⚠️ / ❌ 标注）。
3. 旧版本结论由 Git 历史承担追溯职能，working tree 只保留当前有效结论与变更说明。

## 快速导航

| 想了解 | 看哪里 |
|---|---|
| 这个项目到底是什么 | [规格书 §1](specs/V5.0-core-spec.md#1-项目概述与设计哲学) |
| 数据怎么流动 | [规格书 §2.2 端到端数据流向图](specs/V5.0-core-spec.md#22-端到端数据流向图) |
| 某个功能实现了没有 | [规格书 §3 核心模块详解](specs/V5.0-core-spec.md#3-核心模块详解)（逐条 ✅/⚠️/❌） |
| 哪些地方有缺陷 | [规格书 §5.3.3 P0 缺口清单](specs/V5.0-core-spec.md#533-p0-缺口清单阻断发布) |
| 怎么验收 | [规格书 §6 验收测试用例](specs/V5.0-core-spec.md#6-验收测试用例) |
| 先做什么后做什么 | [规格书 §7.1 交付节奏](specs/V5.0-core-spec.md#71-交付节奏四个里程碑) |
| 张力（SDT 三维）怎么处理 | [规格书 §4 张力状态归属](specs/V5.0-core-spec.md#4-张力状态归属独立条款) |
| 好感度上限到底是多少 | [affinity-migration.md §2](specs/affinity-migration.md#2-决议刻度定义) |
| 想看架构图和决策树（不读文字） | [specs/diagrams/README.md](specs/diagrams/README.md)（已渲染 PNG/SVG，可直接打开） |

## 基线

- 仓库：`FatFish-Whisper 0.1` @ `4386e46`（== `origin/main`）
- **测试口径（2026-10-27 修正，依据裁决 A-10 第三节）**：
  - 主干基线 `4386e46` 实跑 **338 passed**
  - 当前工作树实跑 **core-engine 367 + adapter 53 = 420 passed**
  - B 线（WorkBuddy `main-77ae86b6`）特有测试已做**模块级吸收**（CQ 清洗 / 防抖队列 / 时间块），未整线合并
  - 原「364 passed」是 B 线工作树的数字，误记于本基线，已更正
- 代码规模：`core-engine/src` 13 模块；`fatfish_adapter` 10 模块（含新增 `sanitize.py` / `debounce.py`）

> ⚠️ 两套 pytest **必须分开跑**：`core-engine/src/main.py` 与 `adapter-qq-agent/main.py` 同名，
> 同进程收集会 ImportError（实测 `13 failed / 8 errors`，是撞名不是真失败）。
