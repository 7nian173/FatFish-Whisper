# 好感度刻度统一与数据迁移方案

> **配套文档**：[V5.0 核心开发规格书](V5.0-core-spec.md)
> **审查基线**：`FatFish-Whisper 0.1` @ `4386e46`
> **状态**：待实施（M1 里程碑第 3 项的前置）
> **编写目的**：把散落在 5 处的四套好感度刻度收敛成单一真源，并给出 `user_states` 主键改造的可回滚迁移脚本。

---

## 1. 问题：同一份数据，五个地方四种理解

### 1.1 实测清单

| # | 位置 | 代码 | 隐含刻度 |
|---|---|---|---|
| 1 | `models.py::UserState`（`affinity_raw` 列 docstring） | docstring：`affinity_raw: 好感度原始值，无上限（可以为负）` | **(-∞, +∞)** |
| 2 | `models.py::UserState`（`affinity_raw` 列定义） | `affinity_raw: Mapped[int] = mapped_column(Integer, default=DEFAULT_AFFINITY)`；`DEFAULT_AFFINITY = 0`（`models.py::DEFAULT_AFFINITY`） | 起点 0，无上限 |
| 3 | `engine.py::affinity_tier` | `<30` 疏离 / `<60` 普通 / `<90` 亲近 / `else` 极亲近（**返回中文**） | 分档边界 30/60/90 |
| 4 | `prompt_builder.py::AFFINITY_TIERS` | `AFFINITY_TIERS = (("distant",30),("normal",60),("close",90),("intimate",10**12))` | 分档边界 30/60/90（**返回英文**） |
| 5 | `prompt_builder.py::affinity_tier` | `affinity_tier(affinity_raw)` 独立实现，遍历 `AFFINITY_TIERS` | 同上，**第二份实现** |
| 6 | `engine.py::TrackB._format_relationship` | `_format_relationship()`：`clamp(affinity, 0, 100)` → 输出 `好感度 {0}/100` | **硬截断到 100** |
| 7 | `engine.py::EmotionEngine._actions` | `_actions()`：`if affinity >= 120 and level in (L0, L1)` → 触发 `poke` | **上限至少 200** |
| 8 | `engine.py::EmotionEngine._bump_affinity` | `_bump_affinity()`：`max(0, current + step)`，`step ∈ {-2,-1,0,+1,+2}` | 下限 0，无上限 |

### 1.2 由此产生的三个真实缺陷

**缺陷 A：`poke` 门槛与关系描述互相矛盾**

`_actions`（`engine.py::EmotionEngine._actions`）要求 `affinity >= 120` 才可能戳一戳，但 `_format_relationship`（`engine.py::TrackB._format_relationship`）把同一个值 `clamp(affinity, 0, 100)` 后告诉 LLM「好感度 100/100（极亲近）」。后果：LLM 看到的是"已经满了"，而系统内部还在继续涨到 120——**Track B 永远看不到真实距离**，关系演化的语义在 100 处断掉。

**缺陷 B：分档函数双份实现，返回类型不一致**

`engine.affinity_tier` 返回中文（`"极亲近"`），`prompt_builder.affinity_tier` 返回英文（`"intimate"`）。边界值相同（30/60/90），但**没有单一真源**——改一处不改另一处，就会出现"OS 认为是 close、关系描述认为是 亲近"的错位。更隐蔽的是 `AFFINITY_TIERS` 的最后一档上界是 `10**12`，而 `_format_relationship` 的上界是 `100`：**同一个函数在两个调用点对 `affinity=150` 给出不同档位**（`prompt_builder` 判 `intimate`，`engine._format_relationship` 先截到 100 再判 `intimate`——恰好一致，但纯属巧合）。

**缺陷 C：负数语义未定义**

`models.py::UserState` 说"可以为负"，但 `_bump_affinity` 用 `max(0, ...)` 保证不为负，`DEFAULT_AFFINITY = 0`。即：**docstring 承诺的能力，代码主动放弃了**。要么承认 0 是地板（改 docstring），要么真的允许负值（改 `_bump_affinity`）——当前状态是两者都没做。

---

## 2. 决议：刻度定义

### 2.1 唯一真源

```
好感度 α ∈ [0, 200]，整数，地板 0，天花板 200。
默认值 DEFAULT_AFFINITY = 0。
```

**为什么上限是 200 而不是 100**：

1. `_actions` 的 `poke` 门槛已经是 120（`engine.py::EmotionEngine._actions`），这是代码里已有的、经过测试的事实。
2. 0~100 的刻度在"长期关系"场景下会很快饱和：`_bump_affinity` 每次最多 +2，50 次高质量互动就到顶了。真实用户与 Bot 的互动周期是数月，100 太窄。
3. 200 的刻度让"极亲近"（≥90）之后还有 110 分的成长空间，`poke` 门槛 120 正好落在"极亲近但还没到底"的区间——语义上说得通：**关系到位了才会主动戳你，但还没到无话不谈**。

**为什么地板是 0 而不是负值**：

负好感度在拟人语义上对应"讨厌"，但本项目的三大死线之一就是**防记仇**（I-04 负面原因白名单）。允许 α < 0 等于给"记仇"开了一条数据通道——即使 Track B 的白名单拦住了情绪扣分，`_bump_affinity` 仍可能在 `emotion <= 30` 时把 α 推到负数。**地板 0 是防记仇的第二道代码级防线**。因此 `models.py::UserState` 的 docstring 必须改为"无上限，下限 0"，而不是放宽代码去迎合 docstring。

### 2.2 分档边界（唯一一份）

| 档位 | 英文键 | 区间 | 中文名 | 语义 |
|---|---|---|---|---|
| 0 | `distant` | `[0, 30)` | 疏离 | 刚认识，客气 |
| 1 | `normal` | `[30, 60)` | 普通 | 熟了一点，会开玩笑 |
| 2 | `close` | `[60, 90)` | 亲近 | 会主动关心 |
| 3 | `intimate` | `[90, 200]` | 极亲近 | 会撒娇、会戳你 |

**边界值不变**（30/60/90），与现有 `AFFINITY_TIERS`（`prompt_builder.py::AFFINITY_TIERS`）和 `affinity_tier`（`engine.py::affinity_tier`）完全一致——**这次改造不迁移任何已有数据的分档结果**，只消除重复实现。

### 2.3 单一真源落点

新建 `packages/core-engine/src/affinity.py`：

```python
"""好感度刻度的唯一真源。

所有模块（engine / prompt_builder / 未来的 admin 面板）必须从这里导入，
不得自行定义边界常量或重复实现分档函数。
"""
from typing import Final

AFFINITY_MIN: Final[int] = 0
AFFINITY_MAX: Final[int] = 200
DEFAULT_AFFINITY: Final[int] = 0

#: (英文键, 中文名, 上界开区间)
AFFINITY_TIERS: Final[tuple[tuple[str, str, int], ...]] = (
    ("distant",  "疏离",   30),
    ("normal",   "普通",   60),
    ("close",    "亲近",   90),
    ("intimate", "极亲近", 201),   # 闭区间上界 +1，保持 "< upper" 的比较方式
)


def clamp_affinity(value: int) -> int:
    """夹到 [AFFINITY_MIN, AFFINITY_MAX]。非法输入回落 DEFAULT_AFFINITY。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AFFINITY
    return max(AFFINITY_MIN, min(AFFINITY_MAX, v))


def affinity_tier_key(affinity_raw: int) -> str:
    """返回英文键：distant / normal / close / intimate。"""
    v = clamp_affinity(affinity_raw)
    for key, _zh, upper in AFFINITY_TIERS:
        if v < upper:
            return key
    return "intimate"


def affinity_tier_label(affinity_raw: int) -> str:
    """返回中文名：疏离 / 普通 / 亲近 / 极亲近。"""
    v = clamp_affinity(affinity_raw)
    for _key, zh, upper in AFFINITY_TIERS:
        if v < upper:
            return zh
    return "极亲近"
```

**改造点**：

| 文件 | 原实现 | 改为 |
|---|---|---|
| `engine.py::affinity_tier` | `affinity_tier()` 中文 | 删除，改调 `affinity.affinity_tier_label()` |
| `prompt_builder.py::AFFINITY_TIERS` | `AFFINITY_TIERS` | 删除，改从 `affinity` 导入 |
| `prompt_builder.py::affinity_tier` | `affinity_tier()` 英文 | 删除，改调 `affinity.affinity_tier_key()` |
| `models.py::DEFAULT_AFFINITY` | `DEFAULT_AFFINITY = 0` | 改为 `from .affinity import DEFAULT_AFFINITY` |
| `engine.py::TrackB._format_relationship` | `clamp(affinity, 0, 100)` | 改为 `clamp_affinity(affinity)`，文案改 `好感度 {0}/200` |

**保留的兼容别名**（避免一次性改爆所有调用点）：

```python
# prompt_builder.py 末尾
from .affinity import affinity_tier_key as affinity_tier  # 向后兼容
```

**注意**：`engine.py` 里的 `affinity_tier` 返回中文、`prompt_builder.py` 里的返回英文，**两者不能共用同一个别名**。改造时必须逐个调用点确认语义：

- `engine.py::TrackB._format_relationship` `_format_relationship` → 用中文（`affinity_tier_label`）
- `prompt_builder.py::PromptBuilder.build_inner_os` `build_inner_os` → 用英文（`affinity_tier_key`，因为它要索引 `INNER_OS_TABLE`）

---

## 3. 迁移：`user_states` 复合主键改造

### 3.1 为什么这个迁移和好感度绑在一起

好感度本身**不分房**（I-26 裁决：`UserAffinity` 全局）。但 `affinity_raw` 目前是 `user_states` 表的一列（`models.py::UserState`），而 `user_states` 即将改成 `(user_id, chat_scope)` 复合主键（I-25）。如果不一起处理，会出现：

> 同一个用户在两个群里各有一行 `user_states`，每行都有一份 `affinity_raw` → 好感度被意外分房 → 违反 I-26。

**因此本迁移必须同时完成两件事**：给 `user_states` 加 `chat_scope` 主键，**并把 `affinity_raw` 从 `user_states` 拆出去**到独立的全局表。

### 3.2 目标表结构

```sql
-- 全局锚点：好感度不分房
CREATE TABLE user_affinity (
    user_id       TEXT PRIMARY KEY,
    affinity_raw  INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

-- 分房状态：情绪按会话隔离
CREATE TABLE user_states (
    user_id         TEXT NOT NULL,
    chat_scope      TEXT NOT NULL,          -- 'g:<group_id>' 或 'p:<user_id>'
    emotion_current INTEGER NOT NULL DEFAULT 50,
    emotion_level   TEXT    NOT NULL DEFAULT 'L0',
    last_update     TEXT    NOT NULL,
    last_event_key  TEXT,
    arrival_ts      INTEGER,                -- 新增：乱序防御（I-27）
    PRIMARY KEY (user_id, chat_scope)
);
CREATE INDEX idx_user_states_scope ON user_states(chat_scope);
```

### 3.3 迁移脚本（`scripts/migrate_affinity_scope.py`）

```python
"""user_states 复合主键 + 好感度拆表迁移。

用法：
    python scripts/migrate_affinity_scope.py --db app.db --dry-run
    python scripts/migrate_affinity_scope.py --db app.db --apply
    python scripts/migrate_affinity_scope.py --db app.db --rollback

设计约束：
    1. --dry-run 只读，打印将要执行的全部 SQL 与受影响行数，不改任何字节。
    2. --apply 前自动创建 app.db.bak-YYYYMMDDHHMMSS。
    3. --rollback 从 .bak 恢复（要求 .bak 存在且校验通过）。
    4. 全部操作在单个事务内；任何一步失败整体回滚。
    5. 老数据的 chat_scope 填 'g:legacy'——不能猜，不能填 NULL。
"""
import argparse
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

LEGACY_SCOPE = "g:legacy"


def _columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info({0})".format(table))}


def plan(conn):
    """返回 (步骤列表, 受影响行数)。纯只读。"""
    steps = []
    cols = _columns(conn, "user_states")
    has_scope = "chat_scope" in cols
    has_affinity = "affinity_raw" in cols
    rows = conn.execute("SELECT COUNT(*) FROM user_states").fetchone()[0]

    if not has_affinity and has_scope:
        return ["[SKIP] 已是目标结构"], 0

    steps.append("CREATE TABLE user_affinity (user_id TEXT PRIMARY KEY, "
                 "affinity_raw INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)")
    if has_affinity:
        steps.append("INSERT INTO user_affinity(user_id, affinity_raw, updated_at) "
                     "SELECT user_id, COALESCE(affinity_raw, 0), "
                     "COALESCE(last_update, datetime('now')) FROM user_states")
    steps.append("CREATE TABLE user_states_new (...复合主键...)")
    if has_scope:
        steps.append("INSERT INTO user_states_new SELECT user_id, chat_scope, "
                     "emotion_current, emotion_level, last_update, last_event_key, NULL "
                     "FROM user_states")
    else:
        steps.append("INSERT INTO user_states_new SELECT user_id, '{0}', "
                     "emotion_current, emotion_level, last_update, last_event_key, NULL "
                     "FROM user_states".format(LEGACY_SCOPE))
    steps.append("DROP TABLE user_states")
    steps.append("ALTER TABLE user_states_new RENAME TO user_states")
    steps.append("CREATE INDEX idx_user_states_scope ON user_states(chat_scope)")
    return steps, rows


def apply(conn, steps):
    with conn:                       # 单事务
        for sql in steps:
            if sql.startswith("["):
                continue
            conn.execute(sql)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        sys.exit("db not found: {0}".format(db))

    if args.rollback:
        baks = sorted(db.parent.glob(db.name + ".bak-*"))
        if not baks:
            sys.exit("no backup found")
        shutil.copy2(baks[-1], db)
        print("rolled back from {0}".format(baks[-1].name))
        return

    conn = sqlite3.connect(str(db))
    steps, rows = plan(conn)
    print("affected rows: {0}".format(rows))
    for s in steps:
        print("  " + s)

    if args.dry_run:
        print("[dry-run] nothing written")
        conn.close()
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    bak = db.with_name(db.name + ".bak-" + stamp)
    shutil.copy2(db, bak)
    print("backup: {0}".format(bak.name))

    try:
        apply(conn, steps)
        print("[ok] migrated")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
```

### 3.4 执行顺序（不可调换）

```
1. cp app.db app.db.manual-backup         # 人工再备份一份
2. python scripts/migrate_affinity_scope.py --db app.db --dry-run
   → 人工核对：受影响行数 == 现有用户数；SQL 无意外
3. python scripts/migrate_affinity_scope.py --db app.db --apply
4. python -m pytest -q packages/core-engine/tests    # 364 应全绿
5. 手工验证：
   curl -s localhost:8000/api/v1/state | jq '.users[0]'
   → 应看到 chat_scope 字段；emotion 值与原库一致
6. 观察 24 小时，无异常则删除 app.db.manual-backup
```

### 3.5 回滚触发条件

出现以下任一情况立即 `--rollback`：

- `pytest` 出现**任何**新增失败（不只是 affinity 相关）
- `/api/v1/state` 返回的 `emotion` 与迁移前快照不一致
- 日志出现 `no such column: chat_scope` 或 `no such table: user_affinity`
- 迁移耗时超过 30 秒（说明表异常大，需重新评估方案）

### 3.6 兼容期代码适配

迁移后，代码里所有 `row.affinity_raw` 的读取点必须改：

| 原位置 | 原写法 | 改为 |
|---|---|---|
| `engine.py::EmotionEngine._get_or_create` | `row.affinity_raw`（`_get_or_create` 返回值） | `self._read_affinity(user_id)` 单独查 `user_affinity` |
| `engine.py::EmotionEngine.list_states` | 同上 | 同上 |
| `engine.py::EmotionEngine._build_result` | `"affinity_raw": int(snapshot["affinity"])` | 保持（`snapshot` 由 `_read_affinity` 组装） |
| `engine.py::EmotionEngine._write_affinity` | `affinity_raw=int(affinity)` 写 `UserState` | 改 `_write_affinity` 写 `user_affinity` 表 |
| `models.py::UserState` | `UserState.affinity_raw` 列 | 删除该列 |

**新增两个方法**（`engine.py`）：

```python
def _read_affinity(self, user_id: str) -> int:
    with self._session() as s:
        row = s.get(UserAffinity, str(user_id))
        return int(row.affinity_raw) if row else DEFAULT_AFFINITY

def _write_affinity(self, user_id: str, affinity: int) -> None:
    value = clamp_affinity(affinity)
    with self._session() as s:
        row = s.get(UserAffinity, str(user_id))
        if row is None:
            s.add(UserAffinity(user_id=str(user_id), affinity_raw=value,
                               updated_at=utcnow()))
        else:
            row.affinity_raw = value
            row.updated_at = utcnow()
```

**关键**：`_read_affinity` / `_write_affinity` 不再接收 `chat_scope` 参数——这是刻意的，**签名层面就杜绝了好感度被分房**。

---

## 4. 验收判据

| # | 判据 | 验证方式 |
|---|---|---|
| 1 | 全仓库只有一处定义 `AFFINITY_TIERS` | `grep -rn "AFFINITY_TIERS" packages/` 只命中 `affinity.py` |
| 2 | 全仓库只有一处实现分档函数 | `grep -rn "def affinity_tier" packages/` 只命中 `affinity.py` |
| 3 | `_format_relationship` 不再截断到 100 | `grep -n "clamp(affinity, 0, 100)" engine.py` → 零命中 |
| 4 | 好感度文案显示 `/200` | 请求 Track B 触发路径，检查 Prompt 含 `好感度 120/200` |
| 5 | α=199 时再 +2 不越界 | 单测：`clamp_affinity(201) == 200` |
| 6 | α=0 时再 -2 不越界 | 单测：`clamp_affinity(-2) == 0` |
| 7 | 好感度不随 `chat_scope` 分裂 | 同一用户在两个 scope 各互动一次，`user_affinity` 只有一行 |
| 8 | 迁移幂等 | 连续跑两次 `--apply`，第二次输出 `[SKIP] 已是目标结构` |
| 9 | 迁移可回滚 | `--rollback` 后 `PRAGMA table_info(user_states)` 恢复原列 |
| 10 | 364 测试全绿 | `pytest -q` |

---

## 5. 风险与不做的部分

| 风险 | 缓解 |
|---|---|
| 迁移期间服务在写库 → 数据不一致 | 迁移前**必须停服**；脚本不提供在线迁移模式 |
| `g:legacy` 这个 scope 值污染后续逻辑 | 迁移后 24 小时内跑一次 `SELECT COUNT(*) FROM user_states WHERE chat_scope='g:legacy'`；这些行会在用户下次互动时被真实 scope 的新行取代，老行由 TTL 清理 |
| `poke` 门槛 120 与新的 `/200` 文案不一致 | 门槛**不改**（120 在 200 刻度下语义更清晰："极亲近过半"），只改文案分母 |
| 有人把 `affinity_tier` 的中英两个版本混用 | 删掉所有裸 `affinity_tier` 名字，只保留 `affinity_tier_key` / `affinity_tier_label` 两个明确命名的函数 |

**本期明确不做**：

- 好感度的**衰减**（当前 `_bump_affinity` 只增不减除非情绪低）。长期不互动是否掉好感，属于关系动力学设计，留给 P2 的 I-66「关系记忆账本」一起考虑。
- 好感度的**分维度**（信任/依赖/亲密度）。当前单值足够，分维会重蹈"四套刻度"的覆辙。

---

**文档结束**

> 版本：V1.0｜基线：`4386e46`｜配套：[V5.0 核心开发规格书](V5.0-core-spec.md)
