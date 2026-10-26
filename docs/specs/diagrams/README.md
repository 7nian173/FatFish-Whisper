# 规格书配图（diagrams）

本目录存放 `V5.0-core-spec.md` 里 Mermaid 图的**独立源文件**与**已渲染产物**，用途是让规格书的图既能被 GitHub 直接渲染，也能离线打开、贴进 PPT / 企微文档。

全部图统一用 **Mermaid**。原因见 §4。

## 1. 文件清单

| 文件 | 类型 | 对应章节 | 说明 |
|---|---|---|---|
| `fig2-1-architecture-overview.mmd` | Mermaid 源 | §2.1 | 架构总览：Adapter 探针层 / Core-Engine / 存储层（全局区 + 分房区） |
| `fig2-1-architecture-overview.svg` | 矢量图 | §2.1 | 16 节点 / 18 边，任意放大不失真 |
| `fig2-1-architecture-overview.png` | 位图 | §2.1 | 1568 × 1804，2 倍缩放 |
| `fig2-2-2-emotion-state-machine.mmd` | Mermaid 源 | §2.2.2 | 情绪值演化状态机（衰减 → 习惯化 → 平滑 → 性格调制 → 夹取 → 分级 → 落库） |
| `fig2-2-2-emotion-state-machine.svg` | 矢量图 | §2.2.2 | 11 节点 / 10 边，任意放大不失真 |
| `fig2-2-2-emotion-state-machine.png` | 位图 | §2.2.2 | 924 × 3188，2 倍缩放，可直接插入文档 |
| `fig2-2-3-decision-tree.mmd` | Mermaid 源 | §2.2.3 | 判定决策树（判重 → 白名单 → Track A → 三分支 → Track B → noise 早退 → 审计） |
| `fig2-2-3-decision-tree.svg` | 矢量图 | §2.2.3 | 21 节点 / 25 边 |
| `fig2-2-3-decision-tree.png` | 位图 | §2.2.3 | 1568 × 4176，2 倍缩放 |
| `extract_mermaid.py` | 工具 | — | 从规格书里抽取 Mermaid 代码块，重写 `.mmd` |
| `verify_renders.py` | 工具 | — | 结构化校验渲染产物（见 §3） |

> `fig2-1-architecture-overview.mmd` 是**独立维护**的源文件，不由 `extract_mermaid.py` 从规格书抽取（规格书 §2.1 正文保留 ASCII 版供终端 `cat` 阅读）。其余两张图由规格书代码块抽取生成。

## 2. 如何重新导出

前置：全局已装 `@mermaid-js/mermaid-cli@12.0.0`，命令为 `mmdc`（本机路径 `/f/npm-global/mmdc`）。若换机器需先 `npm i -g @mermaid-js/mermaid-cli`。

```bash
cd "docs/specs/diagrams"

# 1) 规格书改了 Mermaid 源 → 先重新抽块（会覆盖 §2.2.2 / §2.2.3 的 .mmd）
python extract_mermaid.py

# 2) 出矢量图（推荐，体积小、可缩放、可搜索文字）
mmdc -i fig2-1-architecture-overview.mmd   -o fig2-1-architecture-overview.svg   -b white
mmdc -i fig2-2-2-emotion-state-machine.mmd -o fig2-2-2-emotion-state-machine.svg -b white
mmdc -i fig2-2-3-decision-tree.mmd         -o fig2-2-3-decision-tree.svg         -b white

# 3) 出位图（-s 2 = 2 倍缩放，保证文字清晰）
mmdc -i fig2-1-architecture-overview.mmd   -o fig2-1-architecture-overview.png   -b white -s 2
mmdc -i fig2-2-2-emotion-state-machine.mmd -o fig2-2-2-emotion-state-machine.png -b white -s 2
mmdc -i fig2-2-3-decision-tree.mmd         -o fig2-2-3-decision-tree.png         -b white -s 2

# 4) 校验
python verify_renders.py
```

参数说明：`-b white` 指定白底（默认透明，贴进深色主题文档会看不清）；`-s 2` 是缩放倍数，只对 PNG 生效。

## 3. `verify_renders.py` 校验什么

不依赖视觉模型，做 5 项可机器验证的检查：

1. **SVG 合法性** —— 能否作为 XML 解析、根元素是否为 `<svg>`、是否有 `viewBox`。
2. **中文标签完整性** —— 每张图预置一组必须出现的关键词（如架构图的 `存储层 · 全局区`、`UserAffinity`；状态机的 `二次夹取`、`CRISIS≤15`；决策树的 `白名单短路`、`五档打分`），逐个检查是否出现在渲染后的文字里。**这条能抓住「Mermaid 语法通过但中文被吞」的静默失败。**
3. **乱码检测** —— 扫描 U+FFFD（替换符）与 U+25A1（豆腐块）。
4. **节点/边计数** —— 统计 `<g class="node">` 与连线 path，防止「渲染成功但内容缺失」。
5. **PNG 尺寸** —— 校验 PNG 魔数与宽高，小于 300×300 判失败。

> ⚠️ **实现注意**：mermaid-cli 12 输出的 SVG 把标签文字放在 `<foreignObject>` 内的 HTML `<span>/<div>` 里，**不是** `<text>/<tspan>`。只解析后者会得到「0 个文字节点」，误报全部缺失。脚本对两条路径都做了兜底。

> ⚠️ **语法注意**：Mermaid 节点标签里**不能出现裸双引号**。例如 `intent_type=="noise"` 会让 parser 在 token 化阶段报 `Expecting 'SQE', ... got 'STR'`，整个图渲染失败。改写为 `intent_type 等于 noise` 即可。

当前基线（2026-10-27 实测）：

```
fig2-1-architecture-overview    nodes=16 edges~=18  all 10 expected labels present  png 1568x1804
fig2-2-2-emotion-state-machine  nodes=11 edges~=10  all 8 expected labels present   png 924x3188
fig2-2-3-decision-tree          nodes=21 edges~=25  all 9 expected labels present   png 1568x4176
FAILURES: 0
```

## 4. 为什么全部用 Mermaid

早期方案曾计划用 draw.io 画架构总览图（`.drawio` XML），**该方案已废弃**，理由：

| | Mermaid | draw.io |
|---|---|---|
| 维护成本 | 低，纯文本 diff 友好 | 高，XML 冗长，几乎无法 code review |
| GitHub 渲染 | 原生支持 | 不支持，必须先导出图片 |
| 本机导出能力 | `mmdc` 已装，SVG/PNG 一条命令 | **本机无 draw.io CLI**，`.drawio` 生成了也导不出位图 |
| 版本控制 | 改一行 diff 一行 | 改一个节点，diff 出一大段坐标变更 |

结论：**架构图也统一用 Mermaid**。它牺牲了一部分手工排版能力（自动布局在节点多时会交叉），但换来了可 diff、可原生渲染、无需外部 CLI 三项工程收益。

两条链路审查（规格书 §5）里的 ASCII 图保留不动——ASCII 存在的意义是让终端里 `cat` 也能读，不额外画矢量图。
