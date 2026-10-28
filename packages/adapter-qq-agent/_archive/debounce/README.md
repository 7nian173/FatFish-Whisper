# _archive/debounce — B 线防抖方案封存目录

## 结论

**F-19 裁决（2026-10-27，选项 1）**：维持 P0-11（I-64）「Core 是防抖唯一权威」，封存本目录内的
Adapter 侧防抖方案（`debounce.py` / `test_debounce.py`）。封存 = 移动 + README，非删除；
文件原样保留，生产链路零改动，`models.py:83` 的 `"debounce": True` 未动。

裁定依据：《F-08-F-19-裁决建议书.md》§2.3 选项 1 + 计划员裁定（人类已解除逐项审核授权，
计划员决策即可）。台账见 `01-企划室/04-裁决台账.md`（追加行）与
`01-企划室/06-技术债与问题登记.md` F-19 节。

## 否决原因（选项 2 / 选项 3）

选项 2/3（把防抖搬回 Adapter / 两层合并）存在**入口判重结构性缺口**：合并后只上报第一条
`platform_message_id`（本目录 `debounce.py:44-45`、`:52`），第 2..N 条 id 到不了 Core
`main.py:420-422` 判重层——合并窗口一过，宿主重推的消息会被当新消息处理（必然而非「可能」）。

其余理由（建议书 §2.3）：选项 2/3 需改 11 处文档结论；幂等基建（chat_scope 合并键 /
batch_id→event_key / 单飞）只有 Core 同时掌握，搬 Adapter 等于拆成两半。

## 重启条件

若将来重启此方案，必须先解决**批量 id 透传**：`to_payload()` 带 `platform_message_ids`
列表 + Core 逐条判重，消除上述入口判重缺口后再议。

## 文件清单

- `debounce.py`：Adapter 侧防抖实现（196 行），DebounceQueue + merge_events。
- `test_debounce.py`：对应用例（222 行，10 条用例），已随封存移出测试套件。

测试口径：adapter 测试套件由 53 → 43 passed（test_debounce.py 10 条移出）。

## 跑测试须知（易踩坑）

`fatfish_adapter` 未安装进 venv site-packages，只有 adapter 目录在 `sys.path` 时才可导入。

`tests/test_no_receipt.py` 是唯一**没有**写 sys.path shim 的测试文件：

- 从 `packages/adapter-qq-agent/` 目录跑 → 正常；
- 从仓库根单独跑该文件 → `ModuleNotFoundError: No module named 'fatfish_adapter'`。

从仓库根跑整套却不报错，是因为其他测试文件（如 `test_adapter.py:17-18`）的 shim 按字母序先执行、
把目录插进了 `sys.path`——属于**顺序巧合**，不是该文件本身具备独立性。后续若再单跑它，
务必先切到 adapter 目录。

——完——
