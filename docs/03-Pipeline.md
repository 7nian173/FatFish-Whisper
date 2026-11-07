# 03 · Pipeline

> FatFish-Whisper 情感动力学引擎 · 编排与对接
> 从宿主收到一句话，到返回两条消息体与一个延迟值，中间发生了什么。

---

## 1. 一条请求的完整生命周期

```
宿主
 │  MCPRequest(request_id, user_id, scope_id, message, persona_card, chat_mode, context)
 ▼
┌─────────────────────────────────────────────────────────────────┐
│  apps/composition/use_case.py :: EmotionUseCase.execute()        │
│                                                                   │
│  重试循环 (最多 TX_MAX_RETRIES = 3 轮)                            │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  with store.transaction() as tx:      ← BEGIN IMMEDIATE      │ │
│  │   步骤 1   读两个载体 / 空库冷启动                            │ │
│  │   步骤 2   时钟钳制                                           │ │
│  │   步骤 3   取算力档位                                         │ │
│  │   步骤 4   冷档消毒 或 惰性结算                               │ │
│  │   步骤 5   路由（hot 档试 Track B，超时/异常回落 Track A）    │ │
│  │   步骤 6   姿态仲裁 → 刺激合成 → 与路由增量合并               │ │
│  │   步骤 6b  业务限幅（三层 + 流水落账）                        │ │
│  │   步骤 7   冲突判定（对旧快照）                               │ │
│  │   步骤 8   激素叠加（全档位都做）                             │ │
│  │   步骤 9   时间衰减                                           │ │
│  │   步骤 10  写回冲突结论（无原地赋值）                         │ │
│  │   步骤 11  组装 system_head / tail_message                    │ │
│  │   步骤 12  计算打字延迟（喂尾部长度）                         │ │
│  │   步骤 13  落盘（双 CAS）                                     │ │
│  │        ├─ 成功 → tx.commit() → break                          │ │
│  │        └─ 失败 → tx.rollback() → 计数 +1 → 重开事务          │ │
│  │              └─ 末轮仍失败 → 写死信 + commit                  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  步骤 14  构造 MCPResponse                                        │
│  步骤 15  发遥测（emit_agent_run / emit_token_usage）             │
│  步骤 16  return                                                  │
└─────────────────────────────────────────────────────────────────┘
 │  MCPResponse(request_id, status, system_head, tail_message,
 │              state_snapshot, action_intent, typing_delay_ms)
 ▼
宿主 → LLM / 用户
```

---

## 2. 十六步逐条说明

以下行号对应 `sys-mcp-whisper/apps/composition/use_case.py`（335 行）。

### 步骤 1 · 读两个载体，空库冷启动

```python
snapshot, context = self._store.get(tx, request.scope_id)
if snapshot is None or context is None:
    snapshot = S1Snapshot(
        version=1, scope_id=request.scope_id,
        timestamp=request.context.current_time,
        last_decay_at=request.context.current_time,
        is_conflict=False,
        ipc=IPCState(affiliation=0.0, dominance=0.0,
                     relationship_stage="stranger", relationship_ceiling="lover"),
        traits={}, emotions={},
    )
    context = GlobalContext()
```

- 两个载体**必须在同一个事务里配对读出**。端口签名 `get(tx, scope_id) -> (S1Snapshot | None, GlobalContext | None)` 就是为此存在的：拆成两次读会出现"快照 v5 配上下文 v4"。
- `scope_id` 是**读键**，不是便利参数。`s1_snapshot` 每个会话一行，无 scope 的读（`LIMIT 1`）在出现第二个会话的那一刻就是读错行——2026-11-07 实测：库里有 `chat_A` 与 `chat_B` 时发一个 `chat_B` 请求，链路加载了 `chat_A`、跑完后 CAS 又恰好命中 `chat_A` 行并写回，`chat_B` 一行未动，retry 计数为 0、无死信、不报错。现已由 `tests/composition/test_scope_and_decay_anchor.py` 钉住。
- 空库初值：`S1Snapshot.version = 1`（契约下界），`GlobalContext.version = 0`（默认）。
- 初始 `traits` / `emotions` 为空字典——**不预置任何特质**。初始值的业务内容属于数据校准备范畴。
- `IPCState` 的初值是全零 + `stranger`，天花板 `lover`。

### 步骤 2 · 时钟钳制

```python
now: datetime = max(request.context.current_time, snapshot.last_decay_at)
```

- 这一行必须在任何引擎纯函数看到时间之前执行。
- 宿主时钟回拨时，`now` 被拉回 `last_decay_at`，`delta_t` 退化为 0（合法），而不是负数（`TimeDecayError`）。
- 这也是权威时钟字段的来源：`request.context.current_time` 优先，但不会早于 `last_decay_at`。

### 步骤 3 · 取算力档位

```python
tier = snapshot.compute_tier
```

- 档位**取自快照字段**，不在此处重算热度。理由是重算需要的 `msg_freq` / `mention_count` / `context_turns` 三个指标来自宿主上行，而这三个指标在本次请求到达时还没有被更新为"含本条消息"的值——在编排层重算会用上一轮的指标给这一轮定档。
- 档位的权威来源是 `snapshot.compute_tier`，由写入方在**上一轮**决定并落盘。

### 步骤 4 · 冷档消毒 / 惰性结算

```python
if tier == "cold":
    snapshot = sanitize_tier_state(snapshot)
    context_pulse = context.pulse          # 冷档不结算，脉冲原样传递
else:
    snapshot, context_pulse = settle_before_tier_upgrade(
        snapshot, context.pulse, now, tier
    )
```

两条分支：

| 档位 | 动作 | 理由 |
| --- | --- | --- |
| `cold` | `sanitize_tier_state`：强制 `is_conflict = False` | 冷档不该产出三段式提示词。若 `is_conflict` 残留上一轮的 `True`，装配器会吐出 `[拉扯]` 段 |
| 其他 | `settle_before_tier_upgrade` | 若快照是冷档而目标档位是 warm/hot，说明中间隔了一段冷期，需要对整段时间做一次一次性结算 |

**`context_pulse` 必须接手结算返回的 pulse**，不能丢弃。`settle` 结算后会把 `last_decay_at` 盖章为 `now`；若此时丢掉结算后的 pulse 而继续用旧的 `context.pulse`，后续步骤 9 的 `delta_t` 恒为 0，激素将**永久不再衰减**。这是一次静默的、不可观测的数据错误。

> 当前实现中 `tier` 恒等于 `snapshot.compute_tier`，因此 `settle` 的结算分支实际不可达，`context_pulse` 恒等于 `context.pulse`。但接线保持完整：一旦档位改为外部传入，这条路径立即生效且正确。

### 步骤 5 · 路由

```python
segments = routing.parse_speaker([MessageEnvelope(role="user", text=request.message)])
track_a = routing.route_track_a(segments, snapshot)

if tier == "hot":
    try:
        track_b = await asyncio.wait_for(
            routing.route_track_b(
                MessageEnvelope(role="user", text=request.message), snapshot
            ),
            timeout=_TRACK_B_TIMEOUT,          # 0.8 秒
        )
        track_a = track_b
    except (TimeoutError, Exception):
        track_a = track_a                      # 保持 Track A 的结果
```

三条规则：

- **只有 `hot` 档才试 Track B**（高成本语义判定）。`cold` / `warm` 直接使用 Track A 的词典规则结果。
- Track B 超时或抛任何异常时**静默回落到 Track A 的结果**——但 Track A 的结果本身可能带 `is_degraded` 标记，所以降级不是无痕的：`RoutingResult.is_degraded` 会一路传递到遥测。
- `_TRACK_B_TIMEOUT = 0.8` 秒。

### 步骤 6 · 姿态仲裁 → 刺激合成 → 合并

```python
arb = DictionaryArbiter().evaluate(request.message)
mapping = stimulus.to_delta({
    "agreeableness": arb["agreeableness"],
    "arousal": arb["arousal"],
})
merged = _merge_deltas(track_a.stimulus_delta, mapping)
```

- `DictionaryArbiter.evaluate` 返回三键 `{"agreeableness", "arousal", "confidence"}`；`to_delta` 的契约只收前两轴，`confidence` 是信息性的，不参与换算。
- `_merge_deltas` 做**同键相加**并剔除零值：

```python
merged = {k: track_a_delta.get(k, 0.0) + mapping.get(k, 0.0)
          for k in set(track_a_delta) | set(mapping)}
return {k: v for k, v in merged.items() if v != 0.0}
```

零值剔除放在合并之后，因为两个来源各自可能产出 `0.0`（如 `太好了` 的 `pulse.cortisol: 0.0`）。

实测合并示例（消息 `滚`，warm 档）：

| 来源 | 增量 |
| --- | --- |
| `track_a.stimulus_delta` | `{'ipc.affiliation': -1.5, 'pulse.cortisol': 2.0}` |
| `to_delta(arbiter)` | `{'pulse.cortisol': 8.4, 'ipc.affiliation': -4.05}` |
| **合并（同键相加）** | `{'ipc.affiliation': -5.55, 'pulse.cortisol': 10.4}` |

### 步骤 6b · 业务限幅

三层限幅作用在**刺激单位**上，而增量是**激素点数**，因此中间有一次量纲换算。

```python
limited: dict[str, float] = {}
for key, points in merged.items():
    if not key.startswith("pulse."):
        limited[key] = points                       # ipc.* 直通，量纲不同
        continue
    dimension = key.split(".", 1)[1]
    history, today_acc = self._store.load_stimulus(
        tx, snapshot.scope_id, dimension, now
    )
    records = [dynamics.StimulusRecord(at=at, delta=delta / STIMULUS_UNIT_POINTS)
               for at, delta in history]
    accumulator = dynamics.DayAccumulator(
        pos=today_acc[0] / STIMULUS_UNIT_POINTS,
        neg=today_acc[1] / STIMULUS_UNIT_POINTS,
    )
    applied_units = dynamics.apply_limit_cap(
        points / STIMULUS_UNIT_POINTS, records, now, accumulator
    )
    applied_points = applied_units * STIMULUS_UNIT_POINTS
    self._store.append_stimulus(tx, snapshot.scope_id, dimension, now, applied_points)
    if applied_points != 0.0:
        limited[key] = applied_points
self._store.prune_stimulus(tx, snapshot.scope_id, now)
merged = limited
```

三步换算（`STIMULUS_UNIT_POINTS = 8.0`，1 单位 = 8 点数）：

```
点数 ÷ 8  →  刺激单位  →  apply_limit_cap  →  × 8  →  点数
```

五条不可更改的性质：

| # | 性质 | 违反后果 |
| --- | --- | --- |
| 1 | 只有 `pulse.*` 经限幅 | `ipc.*` 是关系裁决载体，量纲 ≠ 刺激单位 |
| 2 | 流水记录**限幅后**的值 | 记请求值会让窗口按两倍速填满，与真实效果脱节 |
| 3 | 追加必须在事务内、且在 `save()` 之前 | 事务外写入会与主状态不同步 |
| 4 | 限幅后为 0.0 的维度不进 `merged` | 零增量进 `HormoneDeltas` 无意义，且会污染白名单校验的意义 |
| 5 | `prune_stimulus` 在循环之后 | 先删再读会漏掉刚过窗口下界的行 |

`apply_limit_cap` 的三层（全部作用在刺激单位上）：

```
① 单次夹紧     x ∈ [-1.0, 1.0]
② 两小时滑窗   now-2h < at <= now 之和，上限 ±3.0
③ 自然日累计   当日之和，上限 ±4.0
```

`prune_stimulus` 的保留边界取 `min(day_start, window_start)`：**不能只用日边界**——跨零点后 `now - 2h` 会落到前一天，只按日边界清理会误删仍在窗口内的行。

实测表：

| 消息 | 原始点数 | 单位 | 限幅后 | 生效点数 |
| --- | --- | --- | --- | --- |
| `滚`（cortisol） | 10.4 | 1.300 | 1.000 | **8.00** |
| `我好喜欢你`（dopamine） | 4.2 | 0.525 | 0.525 | **4.20** |
| 连续 4 次 `滚`（cortisol） | 10.4 × 4 | 1.3 / 2.6 / 3.9 / 5.2 | 1.0 / 1.0 / 1.0 / 1.0→窗口饱和 | **8.0 / 8.0 / 8.0 / 0.0** |

### 步骤 7 · 冲突判定（对旧快照）

```python
verdict = judge.is_conflict(snapshot, deltas)
```

- 必须在 `update_hormones` **之前**执行。冲突描述的是"这句话本身带来的矛盾"，先叠加再判定会让判定结果被存量污染。
- 判定只看增量方向，与快照内容无关：函数签名接收 `curr: S1Snapshot`，但函数体不读取它的任何字段。

### 步骤 8 · 激素叠加（全档位）

```python
snapshot2, pulse2 = dynamics.update_hormones(snapshot, context_pulse, deltas)
```

- **冷档也叠加**，不按档位门控。冷档意味着"算力低"，不意味着"情绪不变化"。
- 使用 `context_pulse`（步骤 4 结算后的脉冲），**不是** `context.pulse`。
- 内部只做物理钳制：激素 `[0, 100]`、IPC `[-100, 100]`。业务限幅已在步骤 6b 完成。

### 步骤 9 · 时间衰减

```python
snapshot3, pulse3 = dynamics.apply_time_decay(
    snapshot2, pulse2, delta_t=now - snapshot.last_decay_at
)
```

- `delta_t` 相对**结算后**的 `snapshot.last_decay_at`。若步骤 4 发生了结算，`last_decay_at` 已被盖章为 `now`，于是 `delta_t == 0`，衰减是空操作——**这个 0 是正确且刻意的**：那段时间已在结算里消费掉了。
- 若改用结算前的值，同一段时间会被扣两次。
- 一次调用同时推进快照与脉冲两个载体（签名返回元组），避免"调用方只结算其中一个"。

### 步骤 10 · 写回冲突结论

```python
snapshot3 = S1Snapshot.model_validate(
    snapshot3.model_dump() | {"is_conflict": verdict.conflicted}
)
```

- **禁止 `snapshot3.is_conflict = ...` 原地赋值**：`S1Snapshot` 的 `frozen=False`，属性赋值会绕过 Pydantic 校验。
- **禁止 `model_copy(update=...)`**：该方法不触发字段校验，是 `check_model_copy.py` 门禁的拦截对象。
- 唯一合法写法是 `model_validate(model_dump() | {...})` 往返。

### 步骤 11 · 组装两条消息体

```python
system_head, tail_message = assembler.assemble_messages(
    snapshot3,
    pure=(request.chat_mode == "pure"),
    pulse=pulse3,
    persona_card=request.persona_card,
)
```

产出两条**彼此独立**的文本：

| 字段 | 决定因素 | 稳定性 |
| --- | --- | --- |
| `system_head` | 仅 `persona_card` | 同一 persona 内逐字节恒定 → 可进 KV Cache |
| `tail_message` | `compute_tier` / `is_conflict` / `chat_mode` / `traits` / `emotions` / `pulse` | 每次都变 |

`pure=True`（`chat_mode == "pure"`）与 `pure=False`（`roleplay`）的差异**只体现在尾部**：

- 头部完全不受影响；
- `roleplay` 在尾部追加第二份封印行；
- `pure` 时刻意不做长度裁剪，`roleplay` 才受 `MAX_INJECTION_BODY_CHARS = 50` 约束。

实测（`persona_card="你是傲娇的猫。"` → head 恒 35；`pulse` 全 50）：

| 档位 | 冲突 | 模式 | `traits` | head | tail |
| --- | --- | --- | --- | --- | --- |
| warm | 否 | pure | 空 | 35 | 40 |
| warm | 是 | pure | 空 | 35 | 61 |
| warm | 否 | roleplay | 空 | 35 | 68 |
| warm | 是 | roleplay | 空 | 35 | 89 |
| warm | 否 | pure | `{tsundere: 85 unlocked}` | 35 | 49 |
| warm | 是 | pure | `{tsundere: 85 unlocked}` | 35 | 70 |
| warm | 否 | roleplay | `{tsundere: 85 unlocked}` | 35 | 77 |
| warm | 是 | roleplay | `{tsundere: 85 unlocked}` | 35 | 82 |
| cold | 否 | pure | 任意 | 35 | 36 |
| cold | 否 | roleplay | 任意 | 35 | 64 |

`traits` 非空时 `[主场]` 换成该特质的专属描写，因此尾部长度随特质内容变化——表中两组差值就是这一段。

### 步骤 12 · 打字延迟

```python
delay_ms = delay.calculate_typing_delay(
    pulse3, snapshot3.is_conflict, user_msg_len=len(tail_message)
)
```

公式：

```
base   = 300 + 20 · min(len, 100)
factor = 1.0
       → 0.2  若 dopamine < 40 且 cortisol > 80        （暴怒：说得又快又冲）
       → 2.0  若 oxytocin > 70 且 45 ≤ cortisol ≤ 70    （害羞：迟疑）
       → 1.0  其他
factor *= 1.3  若 is_conflict
result  = clamp(base · factor, 500, 5000)
```

三条要点：

1. **喂的是尾部长度，不是用户消息长度**。尾巴是系统将要"说"的那段话。喂用户消息长度会让延迟与"要输出多长"脱钩。
2. 尾部若被裁剪过，喂的必须是**裁剪后**的长度。
3. `is_conflict` 用步骤 10 写回后的值（`snapshot3.is_conflict`），不是 `verdict.conflicted`——两者在本实现里相同，但走快照字段保证"提示词与延迟基于同一份状态"。

实测：平静 → 1100 ms；害羞（oxy 80, cor 60）→ 2200 ms；暴怒（dop 30, cor 90）→ 500 ms；冲突基线 → 1430 ms。

### 步骤 13 · 落盘（双 CAS + 重试）

```python
snapshot_final = S1Snapshot.model_validate(
    snapshot3.model_dump()
    | {
        "version": snapshot3.version + 1,
        "timestamp": now,
        "last_decay_at": now,
    }
)
context_with_pulse = GlobalContext.model_validate(
    context.model_dump() | {"pulse": pulse3.model_dump()}
)
context2 = commit.commit_delta(context_with_pulse, GlobalDelta(tension=0.0))
ok = self._store.save(
    tx, snapshot_final,
    snapshot.version,        # 期望的旧快照版本
    context2,
    context.version,         # 期望的旧上下文版本
)
if ok:
    tx.commit()
    self.pending_retry_count = 0
    break
tx.rollback()
self.pending_retry_count += 1
if attempt == self._max_retries - 1:
    self._store.record_dead_letter(
        tx, request.request_id, context.version, snapshot.version,
        "optimistic-lock-exhausted", failed_at=now,
    )
    tx.commit()
```

四段必须按序：

| 段 | 动作 | 理由 |
| --- | --- | --- |
| ① | `snapshot_final.version = snapshot3.version + 1`，并把 `timestamp` / `last_decay_at` 一起推进到 `now` | 快照版本由编排层递增，适配器不代劳；时间锚点同理——引擎是 clock-free 的，只有编排层知道现在是几点。不回填锚点会让下一轮从原锚点重衰减整段（三小时三次请求被算成 1h+2h+3h） |
| ② | 把 `pulse3` 写回 `GlobalContext.pulse`，再 `commit_delta` | 激素落在 `GlobalContext.pulse` 上，只能通过 context 落库；`commit_delta` 只管 tension/version，不会重复提交激素 |
| ③ | `save(...)` 传**两个期望版本** | 双 CAS：两个载体都匹配才写入 |
| ④ | 成功 → `tx.commit()`；失败 → `tx.rollback()` 后重开事务 | 已回滚的事务不能复用 |

失败语义：

- 失败**不抛异常**，而是让循环继续。`pending_retry_count` 是实例内存态，仅用于观测（`/healthz` 会读它）。
- 三次都失败：写死信（`failure_reason = "optimistic-lock-exhausted"`，带 `request_id` 与两个载体的旧版本号），然后 `tx.commit()` 提交这条死信记录，**返回正常响应**。
- 也就是说：**死信路径下用户仍会收到回复**，只是状态没有推进。响应里的 `state_snapshot` 是 `snapshot_final`（版本已 +1，但未落库）。

### 步骤 14 · 构造响应

```python
response = MCPResponse(
    request_id=request.request_id,
    status="success",
    system_head=system_head,
    tail_message=tail_message,
    state_snapshot=snapshot_final,
    action_intent=ActionIntent(emotion="neutral", sticker_tag="none"),
    typing_delay_ms=delay_ms,
)
```

`action_intent` 当前是固定的结构占位（`neutral` / `none`），动作意图的业务化属于产品设计范畴。

### 步骤 15 · 遥测

```python
self._observability.emit_agent_run({
    "request_id": request.request_id,
    "route_id": track_a.route_id,
    "is_degraded": track_a.is_degraded,
    "tier": tier,
})
self._observability.emit_token_usage({"request_id": request.request_id})
```

两个 payload 都是最小骨架，且**必须携带 `request_id`**——这是全链路关联的唯一键。

### 步骤 16 · 返回

---

## 3. 强事务落盘

### 3.1 事务边界

| 位置 | 行为 |
| --- | --- |
| `SqliteStateStore.transaction()` | `BEGIN IMMEDIATE`，返回 `_Tx` 上下文管理器 |
| `_Tx.__exit__` | 未 `commit()` 则 `rollback()` |
| `save()` 内部 | `SAVEPOINT ffw_save_state` 包裹双 CAS；失败 `ROLLBACK TO` 该保存点，不影响外层事务 |
| 端口层 | 永不 commit、永不重试 |

`BEGIN IMMEDIATE` 而非 `BEGIN`：立即获取写锁，把"写冲突"暴露在事务开头而不是提交时，避免长时间持锁后在 commit 阶段才失败。

**事务不支持嵌套**。在同一个 `with store.transaction() as tx:` 内部再调用一次 `transaction()` 会抛 `sqlite3.OperationalError: cannot start a transaction within a transaction`。需要读数据时必须用当前 `tx`。

### 3.2 双 CAS

```sql
-- 快照
UPDATE s1_snapshot SET version = ?, payload = ?
WHERE scope_id = ? AND version = ?          -- 期望旧版本
-- rowcount == 0 即失败

-- 全局上下文
UPDATE global_context SET version = ?, payload = ?
WHERE id = 1 AND version = ?                -- 期望旧版本
```

两条都成功才算 `save()` 返回 `True`；任一条 `rowcount == 0` 则整笔失败，`SAVEPOINT` 回滚。

CAS 保证的是"读到的版本没被别人改过"。没有 CAS 时，两个并发请求各自读到 v5、各自算完、各自写 v6，后写者静默覆盖先写者。

### 3.3 重试预算

```
TX_MAX_RETRIES = 3
```

- 预算归编排层所有（`EmotionUseCase.__init__(max_retries=TX_MAX_RETRIES)`）。
- 每次重试**从步骤 1 重放**，不是从失败点续跑——因为之前的计算基于已过期的快照。
- 重试之间不 sleep：CAS 失败通常意味着并发窗口已被别人填上，立刻重读更可能成功。

### 3.4 死信

| 列 | 内容 |
| --- | --- |
| `seq` | 自增主键 |
| `request_id` | 失败的请求 |
| `context_version` | 失败时的期望上下文版本 |
| `snapshot_version` | 失败时的期望快照版本 |
| `failure_reason` | 当前恒为 `"optimistic-lock-exhausted"` |
| `failed_at` | 失败时刻（用钳制后的 `now`） |

死信表是**追加型**的，没有消费方读取后删除的流程——它是诊断用的最后现场。

---

## 4. 限幅流水（`stimulus_ledger`）

### 4.1 一张表回答两个问题

两小时窗口与日累计都是"同一批行的不同下界求和"，因此只有一个载体：

```sql
-- 窗口（左开右闭）
SELECT at, delta FROM stimulus_ledger
WHERE scope_id = ? AND dimension = ? AND at > ? AND at <= ? ORDER BY at

-- 日累计（正负各一）
SELECT COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0.0),
       COALESCE(SUM(CASE WHEN delta < 0 THEN delta END), 0.0)
FROM stimulus_ledger
WHERE scope_id = ? AND dimension = ? AND at >= ? AND at <= ?
```

两个载体就是两个真相源：窗口说"还可以加 0.5"，日累计说"还可以加 1.0"，两个数会在某次事务失败后永久分歧。

### 4.2 时间归一化

所有写入前都经 `to_utc_naive()`：naive 输入视为 UTC，aware 输入转 UTC 后去掉 tzinfo。

理由：SQLite 按**字典序**比较文本。只有格式统一、时区统一，字典序才等价于时间序。混入 aware 时间会让 `"2026-11-07T12:00:00+08:00"` 与 `"2026-11-07T12:00:00"` 排到错误位置。

### 4.3 自然日与保留边界

```python
day_start_utc(now)        # 按 now 自身 tzinfo 取当日 00:00，再转 naive UTC
window_start_utc(now)     # now - 2h，转 naive UTC
retention_cutoff_utc(now) # min(day_start_utc, window_start_utc)
```

`retention_cutoff_utc` **取 min 而不是 day_start**：跨零点后 `now - 2h` 落进前一天，只按日边界清理会把仍在窗口内的行删掉，导致窗口上限失效。

实测：`now = 2026-11-07 01:00 +08:00` → `day_start = 2026-11-06T16:00Z`，`window_start = 2026-11-06T15:00Z`，`cutoff = 15:00Z`（min，不是日边界）。

### 4.4 流水记录限幅后的值

`append_stimulus(tx, scope_id, dimension, at, applied_points)` 存的是**实际生效**的点数。

若记录请求值，窗口会按"请求了多少"填满，而激素实际只变了"限幅后"那么多——之后每一轮都会以为窗口已满，把后续刺激全部压成 0，形成越限后永久静音。

---

## 5. MCP 接口对接规范

### 5.1 端点

| 方法 | 路径 | 用途 | 鉴权 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 健康检查 + 状态摘要 | **无** |

### 5.2 请求 → 引擎的映射

宿主侧通常只有一个"发送一句话"的动作。推荐的对接顺序：

```
1. 宿主填 MCPRequestDTO（8 字段，时间摊平）
2. to_domain(dto) → MCPRequest
3. await usecase.execute(request) → MCPResponse
4. from_domain(response) → MCPResponseDTO
5. 宿主把 DTO 转成自己的响应格式
```

**逐字段映射的强制要求**：`to_domain` / `from_domain` 必须逐字段手写，禁止 `MCPRequest(**dto.model_dump())`。

原因：`model_dump()` 会把 DTO 上将来新增的字段一并灌进 domain 模型。domain 模型是 `extra="forbid"`，所以灌进来会报错——但报错暴露的是"契约不一致"，而不是"DTO 多了一个字段"这个真实原因；更糟的是若某天 domain 恰好有这个字段，多出来的值会被静默接受。

### 5.3 宿主必须遵守的五条

| # | 规则 | 违反后果 |
| --- | --- | --- |
| 1 | `request_id` 每次请求唯一，并在全链路透传 | 日志与死信无法关联到具体请求 |
| 2 | `scope_id` 与 `user_id` 语义稳定（同一会话/同一人必须同值） | 状态被切碎，好感度按错误的键累积 |
| 3 | `current_time` 用单调来源（服务端时钟），不要用客户端上报时间 | 时钟回拨会触发钳制，`delta_t` 长期为 0，激素不衰减 |
| 4 | `last_interaction_at` 与 `current_time` 使用**同一种** tzinfo 约定 | naive 与 aware 混用在 `max()` 处抛 `TypeError` |
| 5 | `persona_card` 传 `""` 而不是 `None` | `persona_card` 是必填 `str`，`None` 触发 `ValidationError` |

第 5 条尤其容易踩：JSON `null` 到 Python 是 `None`，不会自动变成空串。

### 5.4 响应如何使用

| 字段 | 宿主用法 |
| --- | --- |
| `system_head` | 作为提示词**第一部分**（system / 首条消息）。同一 persona 内可缓存，逐字节稳定 |
| `tail_message` | 作为提示词**第二部分**，紧接在对话流末尾。**不要与 `system_head` 拼接后缓存** |
| `typing_delay_ms` | 按此延迟后再展示回复（拟人化节奏） |
| `state_snapshot` | 只读观测用；宿主**不得**改写后回传 |
| `action_intent` | 动作/贴纸意图（当前恒为 `neutral` / `none`） |

`tail_message` 自带标签行 `【潜意识感知】：`，宿主**不要**再加自己的引导语——标签行是动态内容的引导语，必须与后续内容处于同一消息体。

### 5.5 缓存策略

| 对象 | 可缓存 | 键 |
| --- | --- | --- |
| `system_head` | ✅ 命中率 1.0（同一 persona 内） | `persona_card` 的哈希 |
| `tail_message` | ❌ 每轮都变 | — |
| `state_snapshot` | ❌ | — |
| `typing_delay_ms` | ❌ | — |

头部与尾部必须**物理分离**：把两者拼成一条消息再缓存，等于每轮都 miss，因为尾部每次都变。

### 5.6 错误与降级

| 场景 | 引擎行为 | 宿主应做 |
| --- | --- | --- |
| Track B 超时（> 0.8s）或异常 | 回落 Track A，`is_degraded` 可能为 `True` | 正常处理，不要重试 |
| 乐观锁连续失败 3 次 | 写死信，**仍返回正常响应** | 正常展示，状态未推进 |
| `/healthz` 返回 `database.connected = false` | — | 视为存储故障，但**不要**因 5xx 判断（端点是恒 200） |
| `delta_t` 为负（时钟回拨） | 已被钳制为 0，不会抛 | 修正时钟来源 |
| 未知 `compute_tier` | `should_call_track_b` 抛 `ValueError` | 不应发生；说明快照被写坏 |

---

## 6. 端到端实测

### 6.1 三回合真实调用

真 `SqliteStateStore` + 真 `EmotionUseCase`，`persona_card="你是傲娇的猫。"`，`chat_mode="pure"`，冷启动空库：

> 注：本表为特定参数（特定人设、pure 模式、固定时间）下的推演实例，实际流转数值随输入动态变化。

| 回合 | 消息 | 快照版本 | 档位 | 冲突 | `ipc.affiliation` | `typing_delay_ms` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `太棒了` | 2 | warm | False | 0.000 | 1100 |
| 2 | `滚` | 3 | warm | False | −5.550 | 1100 |
| 3 | `但是我不确定` | 4 | warm | False | −5.550 | 1100 |

回合结束后：

```
GlobalContext.version = 3
pulse = dopamine=50.0, cortisol=58.0, oxytocin=50.0, delayed_impact={}
stimulus_ledger = [('g1', 'cortisol', '2026-11-07T12:00:00', 8.0)]
dead_letter_count = 0
```

注意三点：

1. `滚` 的原始刺激是 `pulse.cortisol: 10.4` 点，**落库生效 8.0 点**（限幅），ledger 也记 8.0——限幅链路端到端生效。
2. ledger **只有 cortisol 一行**，`ipc.affiliation` 零行——`ipc.*` 不经限幅，不写流水。
3. `太棒了` 词典零命中，但快照版本仍然 +1 并落库（每轮都推进版本）。

遥测 payloads：

```python
[
 {'request_id': 'r0', 'route_id': 'route_a_aedadc61', 'is_degraded': False, 'tier': 'warm'},
 {'request_id': 'r1', 'route_id': 'route_a_5f92b9cd', 'is_degraded': False, 'tier': 'warm'},
 {'request_id': 'r2', 'route_id': 'route_a_abffc9d4', 'is_degraded': False, 'tier': 'warm'},
]
```

### 6.2 响应样例

```python
system_head  = '你是傲娇的猫。\n【输出格式封印】：绝对不要使用任何括号或星号描写动作。'
tail_message = '【潜意识感知】：\n[主场]：熟悉的熟悉感漫上来。\n[旁边杵着]：空气安静了两秒。'
```

### 6.3 `/healthz`

空库：

```json
{"version": 0, "pulse": null, "pulse_source": "unavailable",
 "database": {"connected": true, "detail": null},
 "queue_backlog": {"dead_letter_count": 0, "pending_retry_count": 0, "legacy_queue_pending": 0}}
```

跑过一轮且 `pending_retry_count = 2`：

```json
{"version": 1,
 "pulse": {"dopamine": 50.0, "cortisol": 58.0, "oxytocin": 50.0, "delayed_impact": {}},
 "pulse_source": "global_context",
 "database": {"connected": true, "detail": null},
 "queue_backlog": {"dead_letter_count": 0, "pending_retry_count": 2, "legacy_queue_pending": 0}}
```

库文件不存在：

```json
{"version": 0, "pulse": null, "pulse_source": "unavailable",
 "database": {"connected": false, "detail": "OperationalError: unable to open database file"},
 "queue_backlog": {"dead_letter_count": 0, "pending_retry_count": 2, "legacy_queue_pending": 0}}
```

第三种情形**不创建数据库文件**（`mode=ro` 的行为），因此"库不存在"能如实报告为 `connected = false`，而不是被顺手建出一个空库、然后报 `connected = true`。

---

## 7. 健康检查的实现约束

`create_healthz_app(store, usecase) -> FastAPI` 不接受额外参数，handler 内部自己开连接。

### 7.1 为什么不能复用 `store`

`SqliteStateStore.connect()` 用默认的 `sqlite3.connect(self._path, isolation_level=None)`，即 `check_same_thread=True`。ASGI 的请求处理跑在工作线程上，因此从 handler 里调 `store.load()` / `store.get(tx, scope_id)` / `store.dead_letter_count()` 会抛：

```
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread.
```

`def` 与 `async def` handler 都一样——差别只是线程号。

### 7.2 正确做法

每次请求新建一个**只读**连接：

```python
sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=1.0)
```

四条性质：

| 性质 | 说明 |
| --- | --- |
| `mode=ro` | 永不创建文件；库不存在时报 `OperationalError: unable to open database file` |
| `uri=True` | 启用 `file:` URI 语法 |
| `timeout=1.0` | 锁等待上限，避免健康检查被写事务拖住 |
| 用完即 `close()` | `finally` 中关闭；连接建立失败则不会进入 `finally`（直接 return 故障态） |

### 7.3 读什么

只读 `global_context` 的一张单行记录：

```python
row = conn.execute(
    "SELECT version, payload FROM global_context WHERE id = 1"
).fetchone()
```

`version` 与 `pulse` **必须来自同一行**。把 `s1_snapshot.version` 配 `global_context.pulse`，会让一个字段背后有两个真相源，且两者在事务间隙可能不一致。

`payload` 用 `GlobalContext.model_validate_json(payload)` 解析，而不是 `json.loads`——前者会校验字段完整性，后者会把一份损坏的 payload 当成合法 dict 传出去。

死信计数单独查 `dead_letter_deltas`，失败时降级为 0 并记录 detail。

### 7.4 恒 200

端点**从不用 5xx**。用 5xx 表达"库坏了"会与"进程死了"混淆，两者运维动作完全不同。故障由 `database.connected = false` + `database.detail` 表达。

`detail` 截断到 `DETAIL_MAX_CHARS = 200`，格式为 `"<ExcType>: <message>"`。

---

## 8. 部署与运行

### 8.1 依赖

```toml
[project]
dependencies = ["pydantic>=2.0"]
requires-python = ">=3.11"

[project.optional-dependencies]
web = ["fastapi>=0.162.0", "uvicorn>=0.54.0"]

[dependency-groups]
dev = ["pytest>=8.0", "ruff==0.17.3", "httpx>=0.28"]
```

`web` 是**可选依赖组**，核心纯净层只允许 `pydantic`。门禁保证只有 `apps/` 能解析 `web` 组，`packages/` 一律不能。

### 8.2 启动

```bash
uv sync --extra web
uv run uvicorn --factory apps.entrypoints.healthz:create_healthz_app ...
```

`create_healthz_app(store, usecase)` 需要先构造好 `SqliteStateStore` 与 `EmotionUseCase`（组合根职责）。当前交付不含组合根，宿主需自行装配：

```python
store = SqliteStateStore(db_path)
store.connect()
usecase = EmotionUseCase(store, observability_impl)
app = create_healthz_app(store, usecase)
```

### 8.3 数据库位置

`FATFHISH_HORMONES_DB` 之类的环境变量由宿主管辖，引擎不读环境变量。SQLite 文件路径必须落在宿主有写权限的目录，且**不要用系统临时目录**——某些解释器环境下临时目录不可写，会得到 `Permission denied` 与 `unable to open database file`。

### 8.4 已知的装配注意事项

| 事项 | 说明 |
| --- | --- |
| `store.connect()` 必须先于任何 UseCase 调用 | 未 `connect()` 时后续 `transaction()` 会因 `_conn is None` 失败 |
| `store.close()` 要在进程退出前调用 | Windows 下未关闭的 SQLite 文件句柄会让临时目录清理抛 `PermissionError: [WinError 32]` |
| `transaction()` 不可嵌套 | 需要在事务内读数据时，用当前 `tx` 传给 `get` / `load_stimulus` |
| 一个 `use_case` 实例对应一个 `pending_retry_count` | 多实例部署时该值互不共享（实例内存态，不落库） |

---

## 9. 验证清单

```bash
cd "E:/AI Projects/FatFish-Whisper"

# 全量测试
python -m pytest -q                                  # 853 passed

# 五道门禁
python scripts/check_imports.py                      # rc=0
python scripts/check_composition.py                  # rc=0
python scripts/check_model_copy.py                   # rc=0
python scripts/check_demo_runner.py                  # rc=0
bash   scripts/check_path.sh                         # rc=0（唯一 shell 门禁）

# 代码风格
python -m ruff check .                               # All checks passed
```

端到端自检（真 SQLite + 真 UseCase）：

```python
import asyncio
from datetime import datetime
from packages.core.domain.contracts import MCPRequest, MCPRequestContext
from apps.adapters.outbound.sqlite_state_store import SqliteStateStore
from apps.composition.use_case import EmotionUseCase

store = SqliteStateStore("_check.db"); store.connect()
uc = EmotionUseCase(store, observability_impl)
now = datetime(2026, 11, 7, 12, 0, 0)

resp = asyncio.run(uc.execute(MCPRequest(
    request_id="r0", user_id="u1", scope_id="g1",
    message="滚", persona_card="你是傲娇的猫。", chat_mode="pure",
    context=MCPRequestContext(last_interaction_at=now, current_time=now),
)))
assert resp.state_snapshot.version == 2
assert resp.typing_delay_ms == 1100
assert resp.system_head.startswith("你是傲娇的猫。\n【输出格式封印】：")
store.close()
```

预期：`rollout` 完成，`stimulus_ledger` 出现一条 `cortisol` 生效值 `8.0`，`global_context.pulse.cortisol == 58.0`。
