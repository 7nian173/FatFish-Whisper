# 01 · Architecture

> FatFish-Whisper 情感动力学引擎 · 项目总纲
> 一套把"这句话让角色产生了什么情绪"变成可计算、可落库、可回放的确定性系统。

---

## 1. 这个系统在做什么

给定一轮用户输入与一份角色卡，系统输出两段东西：

- **一段稳定不变的人设封印**（`system_head`）——可以缓存，逐字节不变；
- **一段每轮都变的当下感知**（`tail_message`）——由情绪状态生成的三段式提示词片段；
- 以及一个**拟人化的打字延迟**（`typing_delay_ms`）。

中间发生的事是：把一句话解析成刺激增量，叠加到一组有惯性、有上限、会随时间衰减的情绪变量上，再用这些变量去驱动"用词、语气、停顿"。

整个系统的价值主张是**确定性**：同样的输入序列 + 同样的时间推进，必然得到同样的输出。这条性质由"引擎层全纯函数 + 时钟由外部注入 + 无全局状态"三条同时保证。

---

## 2. 六边形架构分层

```
                        ┌──────────────────────────────┐
                        │      宿主 / 外部系统          │
                        └──────────────┬───────────────┘
                                       │
                       ┌───────────────▼────────────────┐
                       │  apps/entrypoints/healthz.py   │  ← 入站适配器
                       │  apps/adapters/inbound/dto.py  │     (MCPRequestDTO → MCPRequest)
                       └───────────────┬────────────────┘
                                       │
        ┌──────────────────────────────▼──────────────────────────────┐
        │                 apps/composition/use_case.py                 │  ← 编排层
        │                 十六步链路 · 事务重试 · 死信                  │
        └───────┬───────────────────────────────────┬─────────────────┘
                │                                   │
    ┌───────────▼─────────────┐        ┌────────────▼──────────────────┐
    │  packages/core/ports/    │        │  apps/adapters/outbound/      │  ← 出站适配器
    │  6 条 Protocol           │◄───────│  sqlite_state_store.py        │
    │  GlobalStatePort         │ 实现   │  stimulus_ledger.py           │
    │  StateRepositoryPort     │        │  schema.py                    │
    │  ObservabilityPort       │        └───────────────────────────────┘
    │  AuditPort               │
    │  ConsolidationPort       │
    │  Transaction             │
    └───────────┬──────────────┘
                │
    ┌───────────▼──────────────────────────────────────┐
    │           packages/core/engine/                   │  ← 纯函数引擎
    │  heat · router · dynamics · judge · stimulus      │     零 IO · 零时钟
    │  assembler · delay · commit · routing             │
    └───────────┬──────────────────────────────────────┘
                │
    ┌───────────▼──────────────────────────────────────┐
    │           packages/core/domain/                   │  ← 契约层
    │  S1Snapshot · GlobalContext · PulseState ·        │     纯 Pydantic
    │  IPCState · TraitState · GlobalDelta · config     │     零依赖
    └───────────────────────────────────────────────────┘

    packages/legacy_bridge/  ← 防腐层（旧库仅通过此处进出）
```

### 2.1 依赖方向是单向的

```
domain  ←  engine  ←  ports
                  ↑
              composition  ←  entrypoints
                  ↓
              adapters
```

严格规则：

| 层 | 允许 import | 禁止 import |
| --- | --- | --- |
| `packages/core/domain` | 显式白名单 7 项：`__future__` / `dataclasses` / `datetime` / `json` / `pydantic` / `types` / `typing` | engine、ports、adapters、任何 IO 库 |
| `packages/core/engine` | 显式白名单 6 项：`__future__` / `dataclasses` / `datetime` / `math` / `pydantic` / `typing` + domain | adapters、ports、sqlite3、任何 IO / 时钟 / 随机 |
| `packages/core/ports` | 仅 `typing` + domain 类型（**只作为类型标注**） | engine、adapters |
| `apps/adapters/*` | domain + ports + 标准库 IO | **engine**（见 §2.2）、其他 adapter |
| `apps/composition/` | domain + engine + ports + adapters | stdlib IO（落盘走 adapter） |
| `apps/entrypoints/` | domain + ports + 框架（fastapi） | engine |

这些禁令不是文档约定，而是由 `scripts/check_imports.py` 在 CI 中强制执行的。两层的白名单是显式枚举（domain 7 项 / engine 6 项），不在表内一律失败——不是禁掉几个危险库，而是只准这些。任何一行违反都会让构建失败。

### 2.2 为什么 adapter 不能 import engine

这条禁令有一个非显然的后果，值得单独说明。

`stimulus_ledger.py` 需要知道"两小时窗口"这个量，而这个常量定义在 `packages/core/engine/dynamics.py` 里。因为 adapter 不能 import engine，它只能**重复声明**一份：

```python
# apps/adapters/outbound/stimulus_ledger.py
TWO_HOUR_WINDOW: timedelta = timedelta(hours=2)   # 与 engine 的值必须一致
```

两份值若漂移，会导致"限幅窗口按 2h 算，但流水清理按另一个时间算"，静默产生错误。防守方式是双向钉值测试：测试同时断言两处的值相等，任一侧改动都会红。

同样的约束导致 `make_record()` 返回裸 `tuple[datetime, float]` 而不是 engine 的 `StimulusRecord` dataclass——adapter 不能引用那个类型，转换责任落在编排层。

### 2.3 端口存在的意义

`packages/core/ports/` 六个 Protocol 全是"方法体为 `...`"的抽象，**没有默认实现**。

| Protocol | 方法 | 为什么是这个形状 |
| --- | --- | --- |
| `GlobalStatePort` | `get(tx, scope_id) -> (S1Snapshot \| None, GlobalContext \| None)` / `save(tx, snapshot, expected_snapshot_version, context, expected_context_version) -> bool` | `get` 一次返回**配对**的两个载体；拆成两个方法就会允许"快照 v5 配上下文 v4"这种撕裂组合。`scope_id` 是**读键**不是便利参数——库里每个会话一行，无 scope 的读在出现第二个会话的那一刻就是读错行 |
| `Transaction` | `commit()` / `rollback()` | 把事务能力显式建模成类型，而不是藏在 adapter 内部 |
| `StateRepositoryPort` | `load() -> S1Snapshot \| None` / `save_if_version_match(snapshot, expected_version) -> bool` | 只服务快照单载体场景 |
| `ObservabilityPort` | `emit_agent_run(payload)` / `emit_token_usage(payload)` / `check_token_budget(estimated_tokens) -> bool` | 三个方向：链路、成本、预算 |
| `AuditPort` | `counterfactual_audit(events, candidate_param) -> Mapping` | 反事实审计：同一批事件换一组参数重放 |
| `ConsolidationPort` | `offline_dream(events)` / `replay_rumination(events)` | 离线巩固，当前未接线 |

四条由类型签名本身表达的纪律：

1. **`tx` 是第一个参数**。这不是风格问题——"禁止隐式连接"被写进了签名，任何实现都无法偷偷开新连接。
2. **`get` 必须带 `scope_id`**。`s1_snapshot` 每个会话一行，读的时候不给 scope 就是让实现去猜。猜错的代价是静默的：2026-11-07 实测过无 scope 的 `LIMIT 1` 会在两个会话并存时读错行、再把结果 CAS 写回同一行，retry 计数 0、无死信。
3. **`save` 永远返回 `bool`，永不抛冲突异常**。CAS 失败是**业务常态**（并发），不是错误。用异常表达会让调用方写成 `try/except` 然后吃掉真正的程序错误。
4. **端口永不 commit、永不重试**。两者都是编排层的职责。适配器一旦自己 commit，编排层的"失败则整体回滚"就失效。

---

## 3. 三层激素池模型

系统把"角色此刻的情绪处境"拆成三个时间尺度不同的层：

```
┌─────────────────────────────────────────────────────────────┐
│  Baseline（基线层）  ·  月级  ·  GlobalContext.baseline      │
│  角色是谁：persona ∈ {cold, warm, hot}                       │
│  派生 persona_heat_modifier ∈ {0.4, 1.0, 1.5}                │
│  —— 决定"这个人天生多容易被调动"                             │
└─────────────────────────────────────────────────────────────┘
                            ×
┌─────────────────────────────────────────────────────────────┐
│  Pulse（脉冲层）  ·  小时级  ·  GlobalContext.pulse          │
│  dopamine / cortisol / oxytocin ∈ [0, 100]，默认 50          │
│  delayed_impact: dict[str, float] —— 延迟生效的增量          │
│  —— 决定"此刻身体什么状态"                                   │
└─────────────────────────────────────────────────────────────┘
                            +
┌─────────────────────────────────────────────────────────────┐
│  Tension（张力层）  ·  分钟级  ·  GlobalContext.tension      │
│  value ∈ [0, 100]，默认 0                                     │
│  —— 累积的未化解矛盾，≥80 时绕过人设系数直接拉到 hot          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  IPC（局部层）  ·  会话级  ·  S1Snapshot.ipc                 │
│  affiliation ∈ [-100, 100] / dominance ∈ [-100, 100]         │
│  relationship_stage / relationship_ceiling（字符串）         │
│  —— 决定"我们之间现在是什么关系"（局部，不跨会话融合）        │
└─────────────────────────────────────────────────────────────┘
```

### 3.1 为什么把激素和 IPC 拆到两个对象

| 维度 | Pulse | IPC |
| --- | --- | --- |
| 生命周期 | 跨会话、跨角色存活 | 绑定单个会话/关系 |
| 载体 | `GlobalContext.pulse` | `S1Snapshot.ipc` |
| 跨域融合 | **不融合** | `ipc.affiliation` 按权重融合 |
| 衰减率 | 每小时 1.0（三激素相同） | affiliation 每小时 0.5，dominance 不衰减 |

拆开的直接原因：激素是**生理**的，不该因为"换了个对话场景"就重置；好感度是**关系**的，跨场景融合才有意义（同一个用户在私聊里亲近，在群里也该显得亲近，但权重不同）。

`GlobalContext` 只有四个字段（`version` / `baseline` / `tension` / `pulse`），**没有 `ipc`**。这是结构事实，不是遗漏。

### 3.2 四层如何合成一个档位

```
① 内核热度（纯整数运算，无浮点误差）
   norm_msg     = min(msg_freq × 10, 100)
   norm_mention = min(mention_count × 25, 100)
   norm_turn    = min(context_turns × 5, 100)
   heat = (4·norm_msg + 5·norm_mention + 1·norm_turn) // 10

   权重含义：被点名(5) > 说话频率(4) > 对话轮次(1)
   实测：heat(5, 1, 2) = 33.0 → warm
         heat(10, 4, 20) = 100.0 → hot

② 动态热度（叠加时间与关系维度）
   dynamic = heat + 0.1·min(max(0, resident_days), 30) + 0.1·max(0, ipc_affiliation)

③ 人设上限（Baseline 层的乘法）
   effective = dynamic × persona_heat_modifier
   实测：dynamic = 106.0
         cold(0.4) → 42.4  → warm
         warm(1.0) → 106.0 → hot
         hot(1.5)  → 159.0 → hot

④ 张力绕过（Tension 层的硬覆盖）
   若 tension >= 80.0：effective = max(effective, 100.0)，tier = "hot"
   实测：tension = 79.9 → 42.4 warm
         tension = 80.0 → 100.0 hot   ← 绕过人设系数
```

**档位阈值**（判据是 `effective`，不是未加系数的 `heat`）：

| 区间 | 档位 | 含义 |
| --- | --- | --- |
| `effective < 10` | `cold` | 旁观，不参与 |
| `10 ≤ effective < 50` | `warm` | 常规参与 |
| `effective ≥ 50` | `hot` | 深度卷入，启用第二路语义判定 |

边界实测：`9.99 → cold` / `10.0 → warm` / `49.99 → warm` / `50.0 → hot`。

注意档位判定用的是 `effective`（已含人设系数），而 `calculate_heat` 返回的是 `heat`（未含任何系数）——两个函数不互相调用，是刻意分层。

### 3.3 四个设计后果

**① 冷档不可能靠"刷屏"变热。** `norm_msg` 单独封顶 40（`min(c×10, 100)` 的封顶是 100，但权重只有 4/10，即最多贡献 40 分）。一个不发任何有信息量的话、只反复说话的会话，最高只能到 warm。只有被点名（权重 5，单次 4 次 @ 即 100×5/10 = 50）能单独推到 hot。

**② 冷档的人设系数是乘法上限，不是加性折扣。** `cold` persona 的 0.4 乘在 106.0 上得 42.4，落在 warm。这意味着"一个冷酷的角色再热也到不了 hot"——除非 tension 撕裂绕过。

**③ Tension 是唯一的绕过通道。** 这是有意的：一个平时冷淡的角色在巨大矛盾面前也会失控。绕过路径硬编码在 `calculate_effective_heat` 里，且阈值是命名常量而非魔数。

**④ 档位是快照字段，不是每轮重算的结果。** `S1Snapshot.compute_tier` 是持久化字段。编排层读它，不重算——重算需要的三个指标在本次请求到达时还没被更新为"含本条消息"的值。

---

## 4. 动力学引擎的核心设计

引擎层（`packages/core/engine/`）九条规则，全部是纯函数：不读时钟、不读环境变量、不碰 IO、不做随机。

### 4.1 时间由外部注入，引擎从不取 `now()`

```python
def apply_time_decay(
    local_snapshot: S1Snapshot,
    global_pulse: PulseState,
    delta_t: timedelta,
    rates: DecayRates = DEFAULT_RATES,
) -> tuple[S1Snapshot, PulseState]: ...
```

- 时间以 `delta_t` 参数传入，不是从系统时钟读。
- `delta_t < 0` 抛 `TimeDecayError`，而不是静默取绝对值——时钟回拨必须暴露。
- **时钟钳制发生在编排层**：`now = max(request.context.current_time, snapshot.last_decay_at)`。

这条设计让整个引擎可以用任意虚构时间点测试，也让"回放三个月前的对话"成为可能。

### 4.2 衰减：线性、按维度、不推进锚点

```python
# 向目标收敛，永不超过目标（两个方向都收敛，绝不背离）
converge(value, target, step) = value + sign(target-value) · min(step, |target-value|)

hormone(t)     = converge(hormone(0), 50.0, 1.0 · hours)     # 目标是基线 50
affiliation(t) = converge(affiliation(0), 0.0, 0.5 · hours)  # 目标是 0
dominance(t)   = dominance(0)                                # 不衰减
```

实测（起点 dopamine=100 / cortisol=80 / oxytocin=60 / affiliation=80 / dominance=20）：

| 经过 | dopamine | cortisol | oxytocin | affiliation | dominance |
| --- | --- | --- | --- | --- | --- |
| 1 小时 | 99.0 | 79.0 | 59.0 | 79.5 | 20.0 |
| 24 小时 | 76.0 | 56.0 | 50.0 | 68.0 | 20.0 |
| 72 小时 | 50.0 | 50.0 | 50.0 | 44.0 | 20.0 |

三个细节：

- **收敛目标不是 0 而是基线 50.0**（`HORMONE_BASELINE`）。`100 → 50` 需要 50 小时，`80 → 50` 需要 30 小时，`60 → 50` 需要 10 小时——所以 24 小时后三个值分别是 76 / 56 / 50，72 小时后就都是 50.0。**情绪回落到"中性"而不是"归零"**，这是生物学直觉与工程实现的交点。
- **收敛是从两侧逼近，不是单向减去**。低于基线的值（如 cortisol=30）会**向上**走回 50，同样用 `min(step, distance)` 停住。写成 `value - step` 会让低值继续下坠到 0。
- **`apply_time_decay` 自身不推进 `last_decay_at`**。它是 clock-free 的纯函数，只把锚点盖成**入参** `snapshot.timestamp`；把锚点真正推到 `now` 是**编排层**的职责（步骤 13 一并写回 `timestamp` / `last_decay_at`）。这一分工是防"重复扣同一段时间"的关键：引擎不知道现在是几点，所以不能替调用方决定锚点该落在哪。
  - 2026-11-07 审查发现的历史缺陷：编排层当时只回填了 `version`，锚点原地不动，于是每轮都从原锚点重衰减整段——三小时三次请求被算成 1h+2h+3h。该缺陷已修，并由 `tests/composition/test_scope_and_decay_anchor.py` 钉住。

### 4.3 钳制：物理边界与业务限幅是两件事

| 层 | 位置 | 边界 | 单位 |
| --- | --- | --- | --- |
| 物理钳制 | `dynamics.update_hormones` | 激素 `[0, 100]` / IPC `[-100, 100]` | 激素点数 |
| 业务限幅 | `dynamics.apply_limit_cap` ← 编排层调用 | 单次 ±1 / 2h ±3 / 日 ±4 | **刺激单位** |

两者**绝不混用**。业务限幅的三层常量（1.0 / 3.0 / 4.0）限定的是"一个刺激单位"的量级，而增量是"激素点数"。生产链路里 `滚` 一句话能产生 10.4 点，是 1.3 个刺激单位——如果把点数直接喂给 `apply_limit_cap`，每次都会被压成 ±1，三句话的效果全部趋同。

因此中间必须有一次量纲换算（编排层负责）：

```
点数 ÷ STIMULUS_UNIT_POINTS(8.0) → 刺激单位 → apply_limit_cap → × 8.0 → 点数
```

`STIMULUS_UNIT_POINTS = 8.0` 的取值依据是"当前词典中最大单轴增量"，属于待压测校准的占位值。

三层限幅的顺序不可换：

```
① 单次夹紧   x ∈ [-1, 1]
② 两小时滑窗 now-2h < at <= now 的已生效之和，上限 ±3
③ 自然日累计 当日已生效之和，上限 ±4
```

每一层的"剩余可加量"是上一层的结果，顺序换了结果就不同（先按日夹紧再按窗夹紧，与先窗后日，在跨零点时不等价）。窗口是不对称区间 `(start, end]`——左开是为了让"恰好两小时前"的那一条过期，右闭是为了让"此刻"计入。

### 4.4 叠加与衰减的顺序：先叠加，后衰减

```python
snapshot2, pulse2 = update_hormones(snapshot, pulse, deltas)          # 步骤 8
snapshot3, pulse3 = apply_time_decay(snapshot2, pulse2, delta_t=…)    # 步骤 9
```

理由：本轮刺激发生在本轮时间窗的**末端**。如果先衰减再叠加，本轮刺激会被按整段时间衰减一次——"刚刚说的话"会立刻折旧。

例外是**惰性结算**：当快照处于 cold 而档位升到 warm/hot 时，中间那段冷期需要一次性结算，此时先结算（含衰减）再叠加。结算会把 `last_decay_at` 盖章为 `now`，于是后续的 `delta_t` 变为 0，衰减成为空操作。

`delta_t == 0` 是**正确且刻意**的，不是 bug。把它"修"成非零会导致同一段时间被扣两次。

### 4.5 冲突判定是增量的纯函数

```python
def is_conflict(curr: S1Snapshot, deltas: HormoneDeltas, config=...) -> ConflictVerdict: ...
```

- 只看**增量方向**，不读 `curr` 的任何字段（签名保留是为了将来的迟滞判定，当前实现未使用）。
- 默认两条规则，按 `priority` 排序，命中第一条即返回：
  - `dopamine_up_and_cortisol_up`（priority 10）——同时开心与紧张，是"强颜欢笑"的特征
  - `affiliation_up_and_dominance_down`（priority 20）——亲近感上升同时主导权下降
- `epsilon = 0.05` 是"方向"判定的死区。
- 必须在 `update_hormones` **之前**执行：冲突描述的是"这句话本身"，先叠加会让判定被存量污染。

实测：`{}` → False；`{'pulse.cortisol': 5.0}` → False；`{'pulse.dopamine': 5.0, 'pulse.cortisol': 5.0}` → True。

### 4.6 delta 白名单只有一个来源

```python
DELTA_ELIGIBLE_PATHS = frozenset({
    'pulse.dopamine', 'pulse.cortisol', 'pulse.oxytocin',
    'ipc.affiliation', 'ipc.dominance',
})
```

五条点分路径。`GlobalDelta` 与 `HormoneDeltas` 两个模型的 validator **引用同一个常量**，物理上只有一份定义。

不包含的路径及原因：

| 路径 | 为什么不能增量 |
| --- | --- |
| `pulse.delayed_impact` | 是 dict 的子结构，`+ 1.0` 会抛 `TypeError` |
| `timestamp` / `last_decay_at` / `last_active_at` | 是时间锚点，不能求和 |
| `traits` / `emotions` | 是结构化集合，不是标量 |
| `scope_id` / `version` | 标识与乐观锁，不是状态 |

裸名（`dopamine` 而非 `pulse.dopamine`）**彻底废弃**，不做兼容层。参数错了就报 `ValueError`，消息里带出键名。

### 4.7 刺激合成：姿态 → 点数

`to_delta(arb: dict[str, float]) -> dict[str, float]` 是词典姿态到激素点数的桥：

| 输入 | 输出 |
| --- | --- |
| `agreeableness > 0` | `dopamine = ag×8.0`, `oxytocin = ag×6.0`, `affiliation = ag×6.0` |
| `agreeableness < 0` | `cortisol = abs(ag)×8.0`, `affiliation = ag×6.0`（负值） |
| `agreeableness == 0` | 不产正向也不产负向 |
| `arousal > 0.3` | 追加 `cortisol = (ar - 0.3)×10.0` |

四个实现细节：

- `arousal > 0.3` 是**严格大于**，`ar == 0.3` 时**不触发**。边界值有独立测试。
- 结果 `round(x, 3)`，防止浮点尾差进白名单校验。
- 缺键直接 `KeyError`，不做 `.get(default)` ——静默补 0 会让"传错了字段名"变成"这段话说得很平静"。
- 零值剔除**不在本函数内**，属编排层职责（与 `_merge_deltas` 合并后的清理是同一件事）。

实测：

| 消息 | arbiter (ag, ar) | 输出 |
| --- | --- | --- |
| `滚` | (−0.675, 0.600) | `{pulse.cortisol: 8.4, ipc.affiliation: −4.05}` |
| `我好喜欢你` | (0.525, 0.225) | `{dopamine: 4.2, oxytocin: 3.15, affiliation: 3.15}` |
| `谢谢你` | (0.375, 0.0) | `{dopamine: 3.0, oxytocin: 2.25, affiliation: 2.25}` |
| `太好了` | (0.600, 0.300) | `{dopamine: 4.8, oxytocin: 3.6, affiliation: 3.6, cortisol: 0.0}` |
| `太棒了` | (0.0, 0.0) | `{}` |

### 4.8 组装：头部与尾部必须物理分离

```python
def assemble_messages(
    snapshot, config, lexicon,
    *, pure=True, phenomenology=None, pulse=None, persona_card=None,
) -> tuple[str, str]: ...
```

返回两条独立字符串，而不是一条拼接好的提示词。

| 输出 | 决定因素 | 稳定性 |
| --- | --- | --- |
| `system_head` | 仅 `persona_card` | 同一 persona 内逐字节恒定 |
| `tail_message` | 档位 / 冲突 / 模式 / traits / emotions / pulse | 每轮都变 |

这是 KV Cache 的硬性前提：头部可缓存（命中率 1.0），尾部每次重算。拼成一条消息再缓存，等于每轮都 miss。

尾部结构（三段式）：

```
【潜意识感知】：
[主场]：<主场景描写>
[拉扯]：<矛盾场景描写>        ← 仅 is_conflict 时出现
[旁边杵着]：<旁观者描写>
```

- 标签行 `【潜意识感知】：` 归**尾部**。它是动态内容的引导语，必须与后续内容在同一消息体内；头部必须保持"绝对纯粹的静态人设 + 封印"。
- `pure=True`（`chat_mode="pure"`）与 `pure=False`（`roleplay`）的差异**只在尾部**：头部完全一致；`roleplay` 在尾部追加第二份封印行。
- `pure=True` 时**刻意不做长度裁剪**，`roleplay` 才受 `MAX_INJECTION_BODY_CHARS = 50` 约束。裁剪是给"要塞进宿主上下文预算"的渲染准备的；纯渲染要保留完整三段，让下游模型看到张力。
- 冷档：`tail = 标签行 + COLD_NARRATION`（28 字符，直接相接，无换行）。

长度规则的三条不变式：

1. 头部长度只随 `persona_card` 变化（无卡 27 字 / 有卡 = 卡长 + 1 + 27）。
2. 尾部长度随冲突状态与模式变化，但**拼接顺序固定**：标签行 → 主场 → 拉扯 → 旁边杵着 → （roleplay）封印。
3. 降级顺序固定：先"长短句替换"（同一条目的 long/short 二选一），再"整行丢弃"（先丢 `[旁边杵着]`，再丢 `[拉扯]`），最后才字符裁剪。

### 4.9 打字延迟：从情绪到节奏

```python
def calculate_typing_delay(hormones: PulseState, is_conflict: bool, user_msg_len: int) -> int: ...
```

```
base   = 300 + 20 · min(user_msg_len, 100)
factor = 0.2   若 dopamine < 40 且 cortisol > 80     （暴怒：又快又冲）
       = 2.0   若 oxytocin > 70 且 45 ≤ cortisol ≤ 70 （害羞：迟疑）
       = 1.0   其他
factor *= 1.3  若 is_conflict
result  = clamp(base · factor, 500, 5000)
```

三条要点：

- **喂尾部长度，不是用户消息长度**。延迟对应的是"角色要输出多长"，不是"用户说了多长"。
- 尾部若被裁剪，喂裁剪后的长度。
- 上下限 `500 / 5000` 是硬夹紧，与情绪无关。

实测（`user_msg_len = 40`）：平静 1100 ms；害羞 2200 ms；暴怒 500 ms（被下限夹住）；冲突基线 1430 ms。

### 4.10 提交：只提交 tension 和 version

```python
def commit_delta(context: GlobalContext, delta: GlobalDelta) -> GlobalContext: ...
```

三条 load-bearing 的理由：

1. **收到 `pulse.*` 或 `ipc.*` 载体时抛 `ValueError`，而不是忽略。** 静默忽略会返回一个"tension 变了但调用方以为激素也变了"的对象——一次无声的空操作。
2. **激素不在这里提交。** `GlobalContext.pulse` 由编排层在事务里显式写回，`commit_delta` 若是第二个写入者，同一字段就有两个 owner。
3. **`GlobalContext` 没有 `ipc` 字段。** 这是结构事实；IPC 增量落在 `S1Snapshot.ipc` 上，由 `update_hormones` 处理。

`tension` 的边界为 `[0, 100]`，同样用 `model_validate(model_dump() | {...})` 往返。

---

## 5. 状态与持久化

### 5.1 两个载体，一个事务

```
S1Snapshot                      GlobalContext
├─ version (ge=1)               ├─ version (ge=0)
├─ scope_id                     ├─ baseline.persona
├─ timestamp                    ├─ baseline.persona_heat_modifier
├─ last_active_at               ├─ tension.value
├─ last_decay_at                ├─ tension.last_feedback_at
├─ is_conflict                  └─ pulse.dopamine / cortisol / oxytocin
├─ compute_tier                    pulse.delayed_impact
├─ ipc.affiliation / dominance
├─ ipc.relationship_stage / ceiling
├─ traits: dict[str, TraitState]
└─ emotions: dict[str, EmotionValue]
```

两者**必须原子更新**。只更新一个会产生"激素变了但快照版本没变"或反过来的撕裂状态，而撕裂在系统里是静默的——不抛异常、不报错，只表现为"某个维度再也不衰减"。

落盘用双 CAS（乐观锁）：两个载体的期望版本都必须匹配才写入。

读写都以 `scope_id` 为键：`get(tx, scope_id)` 只读该会话那一行，`save(...)` 的 CAS 也带 `WHERE scope_id = ?`。两侧必须用同一个键，否则会出现"读了 A 行、写了 A 行、而请求说的是 B"这种既不报错也不推进 B 的情形。

### 5.2 四张表

| 表 | 行数语义 | 用途 |
| --- | --- | --- |
| `global_context` | 恒 1 行（`CHECK (id = 1)`） | 脉冲/基线/张力 |
| `s1_snapshot` | 每个 `scope_id` 一行 | 局部状态（IPC/traits/emotions） |
| `stimulus_ledger` | 追加型 | 限幅流水（两小时窗口与日累计的共同来源） |
| `dead_letter_deltas` | 追加型 | 乐观锁连续失败的现场 |

`stimulus_ledger` 的一表两用是刻意的：窗口与日累计都是"同一批行在不同下界上的和"。两个载体就是两个真相源，会在某次事务失败后永久分歧。

流水记录的是**限幅后**的值。记录请求值会让窗口按"请求了多少"填满，而激素实际只变了"生效了多少"，之后每一轮都以为窗口已满，把后续刺激全部压成 0。

### 5.3 事务的三条边界

| 位置 | 行为 |
| --- | --- |
| 适配器 | `BEGIN IMMEDIATE`；`SAVEPOINT` 包裹双 CAS；**永不 commit、永不重试** |
| 编排层 | 拥有重试预算（3 轮）、决定 commit/rollback、写死信 |
| 端口 | 只声明 `Transaction` 的 `commit`/`rollback`，不实现 |

`BEGIN IMMEDIATE` 而非 `BEGIN`：立即取写锁，把写冲突暴露在事务开头而非提交阶段。

**不可嵌套**：同一事务内再调 `transaction()` 会抛 `sqlite3.OperationalError`。

---

## 6. 四条不可动摇的工程不变式

**① 状态变更必须走 `model_validate(model_dump() | {...})` 往返。**

`model_copy(update=...)` 不触发字段校验，是门禁（`check_model_copy.py`）的拦截对象。属性赋值（`snapshot.field = x`）同样绕过校验，因为模型的 `frozen` 是 `False`。两条路径都会让非法值进状态，且事后无法分辨。

**② 引擎层零 IO、零时钟、零随机。**

`import time` / `datetime.now()` / `random` / `__import__` / `importlib` 在 engine 与 domain 层由 AST 级门禁拦截，不是靠 code review。允许的顶层模块是显式白名单（domain 7 项 / engine 6 项，见 §2.1），不在白名单内一律失败；动态导入（`__import__` / `import_module`）单独一条规则，正则绕过不算数，走 AST 节点识别。

**③ 门禁的扫描面必须自证非空。**

"扫描了 0 个文件所以 0 违规"是最隐蔽的一类恒真陷阱：门禁退出码为 0、报告显示零违规，而生产代码从未被看过一眼。所有门禁都必须带一条非真空守卫（扫描面 ≥ N），且**分母必须取自被测者自己的枚举函数**——测试若用自己的 `rglob` 再数一遍，扫描器被改窄后测试仍会绿。

**④ 断言"不会发生"的测试，必须主动注入一次让它发生。**

前三条说的是"判据本身要有区分力"。这条说的是"要有东西去证明它有区分力"：`assert hits == []` 在违规不存在时永远为真，与被检查的逻辑是否正确无关。上游唯一可靠的手段是**变异测试**——在隔离副本里制造一次违规，确认断言变红，再还原并核对 sha256。

本项目有两次实证：`test_t8` 用 `Path(__file__).resolve().parents[1]` 拼出 `tests/packages`（不存在的目录），`git grep` 返回 rc=1，`assert rc == 1` 从建立起就恒过，**从未检查过 `packages/`**；`check_model_copy.py` 的 `iter_py_files` 少了 `ROOT in parent.parents` 限定，而工作区根自身被 git 追踪，于是每个文件都被当"嵌套独立仓库"跳过，扫描面为 **0**。两处的共同点是：报告是绿的，代码是错的。

---

## 7. 层次职责速查

| 职责 | 归属 | 不该出现在 |
| --- | --- | --- |
| 情绪数值的衰减/叠加/钳制 | `engine/dynamics.py` | adapter、use_case |
| 限幅三层判定 | `engine/dynamics.py::apply_limit_cap` | adapter |
| 限幅的单位换算与调用时机 | `apps/composition/use_case.py` | engine（engine 不知道点数/单位之别） |
| 时钟钳制 | `apps/composition/use_case.py` | engine |
| 重试与死信 | `apps/composition/use_case.py` | adapter |
| CAS 与事务原语 | `apps/adapters/outbound/` | engine、port |
| 提示词文本 | `engine/assembler.py` | adapter |
| 冲突规则表 | `domain/config.py` | 硬编码在 judge 里 |
| 档位判定 | `engine/heat.py` + `engine/router.py` | use_case（不重算） |
| MCP 传输格式 | `apps/adapters/inbound/dto.py` | domain（domain 不认识 DTO） |

---

## 8. 已知边界

| 边界 | 现状 | 影响 |
| --- | --- | --- |
| 动作意图固定 | `ActionIntent(emotion="neutral", sticker_tag="none")` | 表情/贴纸能力未启用 |
| 张力不累积 | 每轮 `GlobalDelta(tension=0.0)` | `tension >= 80` 的绕过路径当前不可达 |
| 档位不外部化 | 读 `snapshot.compute_tier`，不重算 | 惰性结算分支不可达（接线已就绪） |
| 死信无消费方 | 只写不读 | 诊断用，无自动重放 |
| 离线巩固未接 | `ConsolidationPort` 无实现 | 能力未启用 |
| 反事实审计未接 | `AuditPort` 无实现 | 能力未启用 |

七项数值占位（`PLACEHOLDER_PERSONA_HEAT_MODIFIER` / `PLACEHOLDER_STIMULUS_UNIT_POINTS` / `PLACEHOLDER_RESIDENT_BONUS_RATE` / `PLACEHOLDER_IPC_BONUS_RATE` / `PLACEHOLDER_TENSION_THRESHOLD` / `PLACEHOLDER_RESIDENT_RESET_DAYS` / `PLACEHOLDER_MAX_QUEUE_RETRIES`）与六项刺激系数（`PLACEHOLDER_STIMULUS_*`）全部基于当前词典校准，需真实对话数据压测后定稿。

---

## 9. 验证

```bash
cd "E:/AI Projects/FatFish-Whisper"
python -m pytest -q                  # 853 passed
python -m ruff check .               # All checks passed
python scripts/check_imports.py      # 六边形依赖
python scripts/check_composition.py  # 旧库隔离
python scripts/check_model_copy.py   # 状态变更路径
python scripts/check_demo_runner.py  # 端到端可跑
bash   scripts/check_path.sh         # 双仓物理隔离（唯一 shell 门禁）
```

五道门禁各管一件事，且都必须带非真空守卫（语义见 §8.3）：

| 门禁 | 拦截对象 | 扫描面 |
| --- | --- | --- |
| `check_imports.py` | 分层反向依赖、非白名单 stdlib、动态导入、`apps` 越界 | 31 个受检模块 |
| `check_composition.py` | 旧模块（`old*`）被桥包之外的代码 import | 五棵树 |
| `check_model_copy.py` | `model_copy(update=...)` 绕过校验 | 31 个生产文件 |
| `check_demo_runner.py` | 演示脚本末行不是 `ALL ACTS PASSED` | 1 个脚本 |
| `check_path.sh` | 研发代码引用只读交付区路径 | 109 个文件 |

测试面分布（共 853 条）：domain 256 / engine 323 / adapters 104 / composition 40 / entrypoints 10 / guards 12 / legacy_bridge 59 / observability 6 / ports 43。
