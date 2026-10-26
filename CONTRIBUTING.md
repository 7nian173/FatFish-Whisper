# 协作规范：分支、提交、代码合并与冲突解决

> 本文是 FatFish-Whisper（潜鱼潜语）的**合并宪法**。
> 目的只有一个：**让 `main` 永远处于「可启动、可测试、可交付」状态**。
> 任何与本文冲突的个人习惯，以本文为准；本文没覆盖的情况，以「不破坏 main 可运行性」为最高准则裁决。

---

## 0. 三条不可谈判的底线

| # | 底线 | 违反后果 |
|---|---|---|
| 1 | **禁止直接向 `main` 推送**（含仓库所有者） | 绕过 CI 的提交一律 `git revert` |
| 2 | **禁止 `push --force` 到 `main` / 已开放的 PR 分支** | 历史被改写即视为事故 |
| 3 | **禁止把密钥、数据库、缓存提交入库** | 见 `.gitignore`，泄露即需轮换 Key |

第 3 条的明确清单（本项目高频踩坑项）：

- `packages/core-engine/.env`（真 Key）→ 只允许改 `.env.example`
- `*.db` / `*.db-wal` / `*.db-shm`（含用户情绪数据、`memory.db`）
- `packages/core-engine/.cache/`（diskcache 目录）
- `packages/core-engine/persona.txt`、`packages/core-engine/backups/`（用户本地人设与演化备份）
- `__pycache__/`、`.pytest_cache/`、`venv/`

提交前自检：

```bash
git status --short          # 眼睛过一遍，任何 .db / .env / .cache 出现即停手
git diff --cached --stat
```

---

## 1. 分支模型

只保留**一条长期分支 + 短生命周期工作分支**，不搞 develop/release 多线并行（项目体量撑不住）。

```
main                 ← 唯一长期分支，受保护，永远可运行
 └── feat/<scope>-<slug>     新功能
 └── fix/<scope>-<slug>      缺陷修复
 └── docs/<slug>             纯文档
 └── refactor/<scope>-<slug> 重构（不改外部行为）
 └── chore/<slug>            构建、CI、依赖
```

规则：

- `scope` 用包名简写：`core`（core-engine）、`adapter`（adapter-qq-agent）、`ci`、`repo`。
- 例：`feat/core-memory-ttl`、`fix/adapter-recall-cooldown`。
- **一个分支只做一件事**。功能 + 顺手重构 + 格式化混在一起的分支，评审直接打回。
- 分支存活期建议 **≤ 7 天**。超过 5 天未合并，必须主动 `rebase main` 一次，把冲突扼杀在早期。
- 合并后**立即删除**远端分支，避免"僵尸分支"制造重复冲突。

---

## 2. 提交规范（Conventional Commits）

```
<type>(<scope>): <中文简述，不超过 50 字>

<可选正文：为什么这么改，而不是改了什么>

<可选脚注：Refs #123 / BREAKING CHANGE: ...>
```

允许的 `type`：`feat` `fix` `docs` `refactor` `test` `chore` `ci` `perf` `revert`

要求：

1. **标题说「做了什么」，正文说「为什么」**。`update code`、`fix bug`、`修改` 这类标题一律不合法。
2. 一个提交 = 一个逻辑变更。`git add -p` 是你的朋友。
3. 破坏性变更（接口字段增删、DB 表结构不兼容变更、`/api/v1/*` 语义变化）**必须**在脚注写 `BREAKING CHANGE:` 并在 PR 描述里单列迁移说明。
4. 提交信息里**不要**写"由 AI 生成"以外的无信息量内容；本项目欢迎 AI 参与，但人类必须为提交负责（见 README 声明）。

---

## 3. 合并准入清单（PR 自检）

PR 打开前逐条打勾，缺一条不予合并：

- [ ] `main` 已合入本分支（`git rebase origin/main` 或 merge），**无冲突残留**
- [ ] CI 全绿：`import-check`（3.10/3.11/3.12 + Windows 3.12）、`api-smoke`
- [ ] 两套测试本地通过：
  - `cd packages/core-engine && pytest`
  - `cd packages/adapter-qq-agent && pytest`
- [ ] 新增/修改的行为**有对应测试**（纯文档 PR 除外）
- [ ] 未提交 `.env` / `*.db` / `.cache/` / `persona.txt` / `backups/`
- [ ] 对外接口或环境变量有变化时，同步更新 `.env.example` 与 README
- [ ] 提交历史干净：无 `WIP`、无 `tmp`、无 `fix typo` 连环提交（合并前 squash）
- [ ] PR 描述写清：**改了什么 / 为什么 / 怎么验证 / 风险点**

PR 描述模板见 `.github/PULL_REQUEST_TEMPLATE.md`。

---

## 4. 合并策略（按分支类型选，不许自由发挥）

| 分支类型 | 合并方式 | 理由 |
|---|---|---|
| `feat/*`、`fix/*`、`refactor/*`、`chore/*`、`ci/*` | **Squash and merge** | 工作分支的碎提交对 `main` 无价值，压成一个语义完整的提交 |
| `docs/*` | Squash and merge | 同上 |
| 发布节点（打 tag 前） | **Merge commit**（`--no-ff`） | 保留发布边界，方便整段回滚 |
| 紧急修复（hotfix） | Squash and merge，随后立即补测试 PR | 先止血，后补证据 |
| 回滚 | `git revert <sha>`，**禁止** `reset` 已推送的历史 | 保留事故痕迹 |

执行细节：

- Squash 后的提交标题 = PR 标题，必须符合第 2 节格式（GitHub 默认会拼接 PR 内所有提交信息，**手动改成单行规范标题**）。
- 合并前必须先把工作分支 rebase 到最新 `main`：`git fetch origin && git rebase origin/main`。
- **禁止**把 `main` 反向 merge 进工作分支后再合回 `main`（会造成无意义的合并节点与重复冲突）。

---

## 5. 冲突解决规范

### 5.1 先分清冲突类型

| 类型 | 典型场景 | 处理方式 |
|---|---|---|
| **机械冲突** | 同一行被两人改、import 顺序、格式化 | 逐块人工取舍，见 5.3 |
| **语义冲突**（最危险） | 两边都"没冲突标记"但逻辑互斥（例如一方改了 `event_key` 生成规则，另一方在调用处缓存了旧 key） | 必须**读懂两边意图**后重写，见 5.4 |
| **生成物冲突** | `*.db`、`.cache/`、`__pycache__`、`.pytest_cache` | **不解决，直接剔除**：`git rm --cached` 后加进 `.gitignore` |
| **配置冲突** | `.env.example`、`ci.yml`、`requirements.txt` | **取并集**，绝不二选一 |
| **文档冲突** | README、docs/*.md | 以事实为准重写段落，不要拼接两段 |

### 5.2 谁来解决

- **后合入者负责解决**（"谁 rebase 谁背锅"）。先合入者的改动视为既成事实。
- 冲突涉及**双方共同设计**（例如 `engine.py` 状态机、`event_key` 幂等规则）时，**先拉原作者对齐**，再动手改。不要单方面决定。
- 解决者必须在 PR 里**显式列出**自己做了哪些冲突取舍，评审重点看这部分。

### 5.3 标准解决流程（机械冲突）

```bash
git fetch origin
git rebase origin/main          # 工作分支 rebase，而不是把 main merge 进来

# 逐个文件解决
git status                      # 看 both modified
# 编辑文件，删除 <<<<<<< ======= >>>>>>> 标记

git add <file>
git rebase --continue

# 中途想放弃，回到 rebase 前
git rebase --abort
```

**硬性约束：**

1. **禁止** `git checkout --ours <file>` / `--theirs <file>` 整文件覆盖。整文件覆盖等于删除对方的工作，是冲突解决里最严重的错误。
2. **禁止**用「删掉对方的代码」来消除冲突标记。若确实要删除对方改动，必须在 PR 描述里单独说明理由并 @ 对方确认。
3. 冲突标记必须**全部清除**，包括遗留的 `=======` 残行。提交前 grep 自检：
   ```bash
   git diff --check            # 检查空白与冲突标记
   grep -rn '^<<<<<<<\|^=======$\|^>>>>>>>' packages/ || echo "clean"
   ```
4. 每解决完**一个文件**就 `git add`，不要攒到最后一起加，否则 `--continue` 失败时无法定位。

### 5.4 语义冲突的处理（重点）

冲突标记消失 ≠ 冲突解决。以下情况必须做**交叉验证**：

1. **双方都新增了函数/类**：检查是否有同名不同义的定义被静默覆盖。
2. **一方改了签名、另一方改了调用点**：跑全量测试 + 手工走一遍调用链。
3. **DB 表结构变更**：确认 `ensure_schema` 的 `ADD COLUMN` 迁移对**老库**仍生效（本项目老库迁移是硬需求，历史上有真实用户数据）。
4. **`prompt_builder` 相关**：无记忆时输出必须与既有版本**逐字一致**，改动后必须用测试锁定，不接受"看起来一样"。
5. **`event_key` / 幂等相关**：任何触及 `batch_id`、`last_event_key`、`IngressDeduper` 的合并，必须验证两个场景都不退化：
   - 场景 A：同窗口重推 → 只扣一次
   - 场景 B：跨 flush 连发 → **两条都算**（绝不吞消息）

### 5.5 本项目高危文件清单（改这些必须双人确认）

| 文件 | 为什么危险 |
|---|---|
| `packages/core-engine/src/engine.py` | 双轨判定 + 状态机的唯一真相，语义冲突重灾区 |
| `packages/core-engine/src/models.py` | 表结构；**必须是单文件模块**，一旦出现同名 `models/` 目录会遮蔽它 |
| `packages/core-engine/src/prompt_builder.py` | 静态前缀 / 动态尾部的边界，改动会污染缓存命中与逐字一致性 |
| `packages/core-engine/src/memory_router.py` | 降级链顺序决定可用性 |
| `packages/core-engine/src/evolution.py` | 演化写人设卡，写脏不可逆 |
| `packages/adapter-qq-agent/fatfish_adapter/service.py` | 宿主注入 `core=` 时 `native_llm` 补挂逻辑，漏了会让降级永远走预设话术 |
| `.github/workflows/ci.yml` | 改坏了等于关掉护栏 |
| `.env.example` | 配置契约，改字段要同步 README 与代码默认值 |

**同名模块陷阱**：`packages/core-engine/src/` 与 `packages/adapter-qq-agent/fatfish_adapter/` 都有 `models.py` / `main.py`。合并时若出现"导入到了另一边的表定义"，先查 `sys.path` 顺序，不要改 import 语句硬凑。

### 5.6 冲突升级路径

| 情况 | 动作 |
|---|---|
| 单文件冲突 > 100 行，或涉及 > 5 个文件 | 停下来，先和对方对齐设计，必要时**关掉当前 PR 重开小 PR** |
| 30 分钟内无法判定取舍 | 拉原作者进 PR 讨论，不许自己硬猜 |
| 冲突涉及第 5.5 节高危文件 | 必须双人 review，解决者不能自己批准自己的 PR |
| 反复 rebase 反复冲突（同一处第三次） | 说明设计有重叠，**拆分职责**而不是继续叠补丁 |

### 5.7 冲突解决后的验证（不可跳过）

```bash
# 1. 语法 / 导入
python -m compileall -q packages

# 2. 两套测试
cd packages/core-engine && pytest
cd packages/adapter-qq-agent && pytest

# 3. 冒烟：引擎能起、接口有返回
#    本地起 main.py 后
curl -s http://127.0.0.1:8000/api/v1/health
curl -s -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id":"merge-check","message":"今天有点累，但还行吧"}'
```

三项全过才算解决完成。**测试没跑就 `--continue` 的 PR，评审可以直接要求重做。**

---

## 6. 回滚与事故处理

1. **优先 `revert`，绝不 `reset`**：`git revert <sha>` 生成一个可追溯的反向提交。
2. 合并后 CI 变红，**15 分钟内无法修复就回滚**，修好再重开 PR。不要在 `main` 上"边修边跑"。
3. 回滚 PR 同样走第 2 节提交规范，标题 `revert: <原提交标题>`，正文附原提交 sha 与失败原因。
4. 涉及密钥泄露：立刻轮换 Key → 再清理历史（历史清理需全仓库通知，且必须由所有者执行）。

---

## 7. 速查表

```bash
# 开工
git switch main && git pull --ff-only
git switch -c feat/core-xxx

# 日常
git add -p && git commit -m "feat(core): ..."

# 追平主干（工作分支，不是 main）
git fetch origin && git rebase origin/main

# 冲突时
git status                 # both modified 列表
# ... 手工编辑 ...
git add <file> && git rebase --continue

# 放弃本次 rebase
git rebase --abort

# 提交前自检
git diff --check
git status --short

# 推送（首次）
git push -u origin feat/core-xxx
```

---

_本文档随项目演进；修改本规范本身请走 `docs/*` 分支，并在 PR 描述里说明改动动机。_
