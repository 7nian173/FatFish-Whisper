# impl-notes · P0 第一批：SQLite WAL + `chat_scope` 三项分房

对应任务：`task-4`（coder）。审查基线：`docs/specs/V5.0-core-spec.md`、`docs/specs/affinity-migration.md`、`docs/specs/incremental_discussions.md`（三份均**未修改**）。

裁决遵循：**I-26「锚点全局、情绪分房、理由分房」**。SDT 三维张力是 P2，本期**零实现**（全仓 `tension|autonomy|competence|relatedness|张力` 关键词零命中，已 grep 验证）。

---

## 1. 最终验证

```
venv/Scripts/python.exe -m pytest -q
386 passed in 17.23s
```

基线是 `364 passed in 19.42s`。**364 条既有用例全部保留且全部通过**，新增 22 条（`tests/test_chat_scope.py`）。没有新增 `skip` / `xfail` / `-k` 排除，没有降低任何断言强度。

## 2. `git diff --stat`

```
 .../adapter-qq-agent/fatfish_adapter/models.py     |  19 +-
 packages/adapter-qq-agent/tests/test_adapter.py    |  10 +-
 packages/core-engine/src/auditor.py                |  64 +++--
 packages/core-engine/src/engine.py                 | 288 ++++++++++++++++-----
 packages/core-engine/src/main.py                   |  33 +++
 packages/core-engine/src/memory_router.py          | 250 +++++++++++++-----
 packages/core-engine/src/models.py                 | 203 ++++++++++++++-
 packages/core-engine/tests/_helpers.py             |  59 ++++-
 packages/core-engine/tests/test_auditor_ttl.py     |  10 +-
 packages/core-engine/tests/test_engine.py          |  17 +-
 10 files changed, 776 insertions(+), 177 deletions(-)
```

新增文件（未跟踪）：`packages/core-engine/tests/test_chat_scope.py`（22 条用例，约 500 行）。

工作树变更全部保留，未执行 `git add` / `git commit` / `git push`。

---

## 3. 逐文件改动

### 3.1 `packages/core-engine/src/models.py`（+192 / −11）

| 项 | 内容 |
| --- | --- |
| 新增常量 | `DEFAULT_CHAT_SCOPE = ""`、`SCOPE_GROUP_PREFIX = "g:"`、`SCOPE_PRIVATE_PREFIX = "p:"` |
| `UserState` | 主键改为 `(user_id, chat_scope)`；**删除 `affinity_raw` 列**；`as_dict()` 含 `chat_scope`；`__repr__` 带 scope |
| 新增 `UserAffinity` | `__tablename__ = "user_affinity"`，`user_id` 单主键 + `affinity_raw`(default 0) + `updated_at`。**没有 `chat_scope` 列**——这是 I-26「锚点全局」在表结构上的落实 |
| `BotMood` | 原样保留（`id=1` 单行整体心情，全局） |
| `MessageCount` | 原样保留（全局按用户不分房；注释写明活跃度闸门问的是「这个人今天还来不来」，与「在哪个群」无关） |
| `ensure_schema()` | 原样保留（通用 `ADD COLUMN`，不加 `DEFAULT` / `NOT NULL`） |
| 新增 `migrate_chat_scope(bind)` | 见 §4 迁移 |

`chat_scope` 默认 `""` 而不是 `p:<user_id>` 的决定，写在 `DEFAULT_CHAT_SCOPE` 的注释里：分房之前的行既可能来自群聊也可能来自私聊，猜成私聊会让老数据被新逻辑误读（本该只在群里出现的情绪跑到私聊里）。**不做静默猜测。**

### 3.2 `packages/core-engine/src/engine.py`（+226 / −62）

**WAL / PRAGMA**

新增模块级函数 `install_sqlite_pragmas(engine)`，内部用 `event.listens_for(engine, "connect")` 注册 `_set_sqlite_pragmas(dbapi_connection, _connection_record)`，依次执行：

```
PRAGMA journal_mode=WAL
PRAGMA synchronous=NORMAL
PRAGMA busy_timeout=5000
PRAGMA foreign_keys=ON
```

注册时机在 `Base.metadata.create_all()` **之前**——监听器只对注册之后建立的连接生效，而 `create_all` 自己就会取一条连接。放在后面等于第一条连接裸奔。

**迁移调用**

```python
install_sqlite_pragmas(self.db_engine)
Base.metadata.create_all(self.db_engine)
self.migrations = ensure_schema(self.db_engine)
self.migrations.extend(migrate_chat_scope(self.db_engine))
```

**分房透传**（`chat_scope: str = DEFAULT_CHAT_SCOPE` 贯穿全链路）

- `_get_or_create(user_id, chat_scope)` / `_write_emotion(..., chat_scope)`：`session.get(UserState, (user_id, scope))` **全部改元组**
- `_acquire_state_lock` / `_release_state_lock`：真实分区才拼后缀（见下方决策 D1）
- `_habituation_alpha(user_id, target, risk, chat_scope)`：`self._habit` 键改 `(user_id, chat_scope)`
- `update_emotion(..., chat_scope)` / `handle_message(..., chat_scope)` / `_whitelisted_payload` / `_track_b_once` / `_evaluate_track_b_locked` / `process(..., chat_scope)`：全透传
- `Debouncer` 的合并键改为 `user_id + "|" + scope`（避免同一个人的两个群的消息被合并成一次）
- `IngressDeduper._key` 文本退化分支改 `_digest(user_id, chat_scope, text)[:32]`
- `debug` 三个出口（whitelist / noise / 主链路）全部带 `chat_scope`

**好感度拆表**

- `_read_affinity(user_id)` / `_write_affinity(user_id, affinity)`：读写 `UserAffinity` 表，**签名不含 `chat_scope`**（签名层面杜绝分房）
- `_bump_affinity(user_id, emotion)`：改从 `_read_affinity` 取当前值
- `list_states()`：额外一次 `select(UserAffinity)` 查全表拼 dict（避免 N+1），输出每行带 `chat_scope` 与 `affinity`

**防抖归属**

`DEBOUNCE_WINDOW_MS` 支持：有该 env 则 `ms/1000`，否则回落 `DEBOUNCE_SECONDS`（默认 1.2s），传给 `Debouncer(window_seconds)`。默认 `500`。

### 3.3 `packages/core-engine/src/auditor.py`（+45 / −19）

- `self._stimuli: dict[tuple[str, str], _Stimulus]`（键 = `(user_id, chat_scope)`）
- `_bump(user_id, polarity, chat_scope="")`
- `audit(..., *, allow_unlock=True, chat_scope="")`
- `reset(user_id=None, chat_scope=None)` 三态：
  - `reset()` → 清空全部
  - `reset("u")` → 删该用户**所有分区**（保持老语义，既有调用点行为不变）
  - `reset("u", "g:1")` → 只删该分区
- `prune()`：stale 与 overflow 两条路径全部按 tuple key 收集和删除。**不能按 `user_id` 去重**——同一个人的多个分区会被漏删或误删。
- `snapshot(user_id, chat_scope="")`

### 3.4 `packages/core-engine/src/memory_router.py`（+188 / −62）

- `_connect()` 加同 4 条 PRAGMA（裸 `sqlite3` 用 `conn.execute`，与 SQLAlchemy 的 `event.listens_for` 分开设）
- `memories` 表：DDL 加 `chat_scope TEXT NOT NULL DEFAULT ''`；`PRAGMA table_info(memories)` 判列存在后幂等 `ALTER TABLE memories ADD COLUMN chat_scope TEXT NOT NULL DEFAULT ''`；新增 `idx_mem_user_scope (user_id, chat_scope, created_at)`
- `IMemoryProvider.retrieve/store`、`MemoryItem`、`_store_sync`、`_evict_lru`、`_retrieve_sync`、`count`、`digest`、`HostNativeProvider`、`ExternalPluginProvider`、`MemoryRouter.*` 全部加 `chat_scope` 并透传
- `count(user_id, chat_scope=None)`：`None` = 跨分区汇总（看板视角），传值 = 该分区计数
- `_evict_lru` 按**分区**计数淘汰：上限是「每分区 200 条」，一个群灌满不会挤掉另一个群的记忆

### 3.5 `packages/core-engine/src/main.py`（+33 / −0）

- `ChatRequest.chat_scope: Optional[str] = None`
- 新增模块级 `derive_chat_scope(user_id, group_id=None, explicit=None)`——**全仓唯一派生点**：
  1. `explicit` 非空 → 原样使用
  2. `group_id.strip()` 非空 → `"g:" + group`
  3. 否则 → `"p:" + str(user_id)`

  空串 / 纯空白 `group_id` **不得**推成 `"g:"`——那会把所有群号缺失的用户挤进同一个假群，属于静默的数据污染。下游一律接收派生结果，禁止自拼。
- `chat_endpoint` 计算一次并透传给 `engine.handle_message`；响应体回显 `chat_scope`。

### 3.6 `packages/adapter-qq-agent/fatfish_adapter/models.py`（+16 / −3）

- `Target.chat_scope` property 复用 `key`（`"g:"+group_id` / `"p:"+user_id`，格式与 Core 一致）
- `to_payload()` 增加 `"chat_scope"`，并把 `debounce` 改为 `True`（防抖归属 Core，Adapter 只声明合并意图）

### 3.7 测试辅助 `packages/core-engine/tests/_helpers.py`（+48 / −11）

- `seed_state(..., chat_scope="")` 同时写两张表（情绪进 `user_states` 按 scope，好感度进 `user_affinity` 全局）
- `get_state(engine, user_id, chat_scope="")` 返回 `{chat_scope, emotion, level, affinity, last_update}`，`affinity` 来自全局表

---

## 4. 老库迁移

### 4.1 迁移触发条件

`migrate_chat_scope(bind)` 反射 `user_states` 的列：

- 表不存在 → 空操作返回
- `无 affinity_raw` 且 `有 chat_scope` → 视为已是目标结构，只补 `idx_user_states_scope` 索引，返回 `[]`（幂等保证）
- 其余 → 执行迁移

### 4.2 迁移步骤（实测输出，8 步）

```
1. ALTER TABLE user_states ADD COLUMN chat_scope VARCHAR
2. INSERT OR IGNORE INTO user_affinity (user_id, affinity_raw, updated_at)
     SELECT user_id, COALESCE(affinity_raw, 0), COALESCE(last_update, CURRENT_TIMESTAMP)
     FROM user_states
3. CREATE TABLE user_states__new (... PRIMARY KEY (user_id, chat_scope))
4. INSERT INTO user_states__new (...)
     SELECT user_id, COALESCE(chat_scope, ''), COALESCE(emotion_current, 50),
            COALESCE(emotion_level, 'L0'), COALESCE(last_update, CURRENT_TIMESTAMP),
            last_event_key
     FROM user_states
5. DROP TABLE user_states
6. ALTER TABLE user_states__new RENAME TO user_states
7. CREATE INDEX IF NOT EXISTS ix_user_states_user_id ON user_states(user_id)
8. CREATE INDEX IF NOT EXISTS idx_user_states_scope ON user_states(chat_scope)
```

**为什么先 `ALTER TABLE` 再加第 1 步**：SQLite 的 `ALTER TABLE ADD COLUMN` 允许带常量 `DEFAULT` 的 `NOT NULL`，但 `ensure_schema` 的通用分支刻意不加 `DEFAULT`。所以 `chat_scope` 列由迁移自己补（带 `DEFAULT ''`），第 4 步再用 `COALESCE(chat_scope, '')` 兜底。

**为什么第 2 步失败就直接 return**：好感度搬迁是拆表的前置条件。它失败还继续 `DROP TABLE` 会直接丢数据。宁可迁移中止（下次启动重试），也不能让用户好感度归零。

**为什么第 3-6 步失败要 `DROP TABLE IF EXISTS user_states__new`**：SQLite 没有事务性 DDL 的完整保证，半成品 `_new` 表留着会让下次迁移撞名。失败即清理。

### 4.3 老数据语义

老数据 `chat_scope = ''`，**不与 `p:<user_id>` 合并**。实测（脚本已失效，结论由 [`review-chat-scope.md`](review-chat-scope.md) §2.1 的 `migrate_chat_scope` 独立复现验证）：

```
old-1 state:  {'chat_scope': '', 'emotion': 73, 'level': 'L1', 'affinity': 42, ...}
old-1 affinity: 42                      # 拆表后完整保留
process("old-1", ..., chat_scope="p:old-1") -> emotion 55, chat_scope 'p:old-1'
老分区仍是 73: 73                        # 私聊推进没有污染老分区
私聊分区独立: 55
restart migrations: []                  # 二次启动是空操作
after restart old-1 affinity: 42        # 幂等，好感度不重复搬
```

---

## 5. 被更新的既有断言（逐条给理由）

只改**因设计变更而失效的实现细节断言**，没有放宽任何行为断言。

### 5.1 `tests/test_engine.py::test_models_表结构`（1 处）

| | |
| --- | --- |
| 原来断言 | `UserState` 列集合 = `{user_id, emotion_current, emotion_level, affinity_raw, last_update, last_event_key}`，且 `affinity_raw.default.arg == 0` |
| 为什么现在不对 | 复合主键改造后 `user_states` 必须新增 `chat_scope`；`affinity_raw` 必须搬出本表，否则同一用户每群一行 → 好感度被动分房，违反 I-26 |
| 改成 | 列集合 = `{user_id, chat_scope, emotion_current, emotion_level, last_update, last_event_key}`；新增 `list(primary_key.columns.keys()) == ["user_id", "chat_scope"]`；新增 `UserAffinity` 列集合 `{user_id, affinity_raw, updated_at}` + 显式断言 `"chat_scope" not in affinity_columns` |

### 5.2 `tests/test_auditor_ttl.py`（3 处）

| | |
| --- | --- |
| 原来断言 | `assert "u-a" in aud._stimuli`（`_stimuli` 的键是 `user_id` 字符串） |
| 为什么现在不对 | `_stimuli` 的键改成 `(user_id, chat_scope)` 元组，`"u-a"` 永远不会是键 |
| 改成 | `assert ("u-a", "") in aud._stimuli`；容量淘汰那两条同理改 `("user-0000", "")` / `("user-%04d" % STIMULUS_CACHE_MAX, "")` |

淘汰**行为**的断言（`pruned == 1`、`len == STIMULUS_CACHE_MAX`、最旧被删）全部原样保留，只改了取键方式。

### 5.3 `packages/adapter-qq-agent/tests/test_adapter.py`（2 处）

| | |
| --- | --- |
| 原来断言 | `payload["debounce"] is False`，注释「Adapter 已做防抖，不应让 Core 再合并」 |
| 为什么现在不对 | 防抖归属裁决（规格 P0-11）判给 Core：`Debouncer` 是唯一权威，Adapter 传 `debounce=True`。原断言与裁决**方向相反** |
| 改成 | `payload["debounce"] is True` + 注释写明归属；同时新增 `payload["chat_scope"] == "g:88888"` |
| 另一条 | `test_私聊不带group_id` 新增 `payload["chat_scope"] == "p:10001"` |

---

## 6. 新增验收 `tests/test_chat_scope.py`（22 条）

| 组 | 用例 | 验的是什么 |
| --- | --- | --- |
| WAL | `test_引擎库开启WAL` / `test_记忆库开启WAL` / `test_引擎连接启用busy_timeout与foreign_keys` | 两个库都真的处于 `wal`；`busy_timeout=5000`、`foreign_keys=1` |
| 情绪分房 | `test_同一用户两个群情绪互相隔离`（TC-05）/ `test_两个群各自独立演化` / `test_分房后行数按分区增长` / `test_老库空串分区不被猜成私聊` | G1 推高时 G2 保持 50；两群能往相反方向走；三个分区三行；老数据不被当成私聊 |
| 理由分房 | `test_刺激计数按分区隔离`（TC-05：G1=3 / G2=1）/ `test_一个分区解禁不影响另一个分区` / `test_reset只清指定分区` / `test_习惯化窗口按分区隔离` | 计数不跨群累加；A 群攒满不影响 B 群；`reset` 三态；alpha 降档不跨群 |
| 记忆分房 | `test_记忆按分区物理隔离`（TC-05）/ `test_同一句话在不同分区各存一条` / `test_记忆LRU淘汰按分区计数` / `test_完整链路不跨群注入记忆` | G1 内容在 G2 检索为空；同句不同群各一条；淘汰不跨群；`handle_message` 走通后 `debug.memories == 0` |
| 锚点全局 | `test_好感度跨分区共享` / `test_好感度读写不接受chat_scope` / `test_BotMood全局单行` | 好感度跨群一致且被推高；`_read_affinity(u, scope)` 直接 `TypeError`；`BotMood` 始终只有一行 |
| 派生点唯一 | `test_派生规则四种输入` / `test_接口层派生并透传` | 显式/群号/空串/None 四种输入；`/api/v1/chat` 派生后透传并回显，两个分区两行 |
| 迁移幂等 | `test_老库迁移幂等且好感度不丢` / `test_新库迁移是空操作` | 主键真改成复合、数据真搬迁、好感度不丢、第二次 `migrate_chat_scope` 返回 `[]` |

**写这些用例时踩到的两个坑（记录，避免误判为缺陷）**：

1. `auditor.audit` 的计数用例必须传 `allow_unlock=False`。解禁是**概率性**的（到次数后按 traits 掷骰），掷中就 `reset` 清零——不关掉这条随机支路，计数断言会随机红。
2. 好感度微调（`_bump_affinity`）是 `process` 主链路的第 4 步，只调 `update_emotion` 不会碰它。所以「好感度跨分区共享」这条走 `process` 而不是 `update_emotion`。

---

## 7. 设计决策与理由（供 reviewer 判断）

**D1 · 状态锁 key 的空 scope 处理**
`lock:state:<user_id>` 是既有形状（老库、老调用方、运维排查命令、既有并发用例都按它占位）。空 scope 继续沿用该形状，只有真实分区（`g:` / `p:`）才拼 `|scope` 后缀。理由：给空 scope 也拼后缀是无意义的改动，还会让运维手册里的锁排查命令失效。

**D2 · `_habit` 按 scope 分房**
习惯化直接调制情绪 alpha，属于情绪机的一部分。不分房的话，在 A 群连夸三次会让 B 群第一次说话就被降档——这正是「情绪分房」要避免的串味。

**D3 · `reset(user_id)` 保留「清该用户所有分区」语义**
带一个参数时清全部，是为了让既有调用点（老代码、运维脚本）行为不变；需要精确清理时必须显式传 `chat_scope`。三态语义在 docstring 里写清了。

**D4 · `MessageCount` 不分房**
活跃度闸门问的是「这个人今天还来不来」，不是「在这个群还来不来」。分房会让同一用户在多个群各计一次，把闸门误开。

**D5 · 空串 `group_id` 落到 `p:<user_id>`**
不推成 `"g:"`。`"g:"` 是个无意义的假群号，会把所有「群号缺失」的用户挤进同一个分区，造成静默的数据污染。

---

## 8. 遗留问题

1. **`docs/specs/_review/` 目录在本批之前不存在**，本次新建。`docs/` 整体未被 git 跟踪（`?? docs/`），所以 impl-notes 与审查基线一样不在版本库里。
2. **老分区 `''` 的清理策略未定**。当前老数据永久留在 `''` 分区，新流量一律进 `g:`/`p:`，因此老分区实际上会自然冻结、随 TTL 衰减但不会被删除。是否需要在某个版本后把 `''` 分区归档或合并，属于产品决策，本批不做。
3. **`ExternalPluginProvider` 的 `chat_scope` 透传是「尽力而为」**：POST body 里带上，但外部插件是否遵守取决于插件实现。`HostNativeProvider` 用 `try/except TypeError` 回退到无 scope 调用，保证老宿主不炸。
4. **`test_chat_scope.py` 里 `test_记忆LRU淘汰按分区计数` 直接改 `local.max_items = 3`**，依赖 `LocalSQLiteProvider` 实例属性可写。若将来该字段变成构造参数需同步调整。
5. **SDT 三维张力（P2）未实现**，按裁决不应在本期出现；已 grep 验证零命中。

---

## 9. 未做的事

- 未 `git add` / `git commit` / `git push`（按要求保持工作树变更供 reviewer 看 `git diff`）。
- 未修改三份审查基线文档。
- 未实现 SDT 张力（P2，明确不在本期范围）。
- 未改动 `evolution.py`：`_all_users()` 用 `select(UserState.user_id)` 去重，复合主键后仍正确，无需改。
