# 02 · Contracts

> FatFish-Whisper 情感动力学引擎 · 核心契约
> 全部数据结构的权威定义。任何实现与本文不符，以本文为准。

---

## 0. 契约的三条通则

**① 全部 `extra="forbid"`。** 每个 Pydantic 模型都声明了它，且由反射测试逐一覆盖——不是抽样，是枚举所有 domain 模型。

**② 状态变更走往返，不走 `model_copy`。**

```python
# 唯一合法
updated = S1Snapshot.model_validate(snapshot.model_dump() | {"is_conflict": True})

# 禁止：update 不经过 validator
updated = snapshot.model_copy(update={"is_conflict": True})

# 禁止：属性赋值绕过校验（模型 frozen=False）
snapshot.is_conflict = True
```

`model_copy(update=...)` 的 `update` 参数**不经过 validator**，非法值会静默进状态。这条禁令由 `scripts/check_model_copy.py` 在 CI 中强制。

**③ 时间字段一律 `datetime`，无时区强制。** naive 与 aware 都接受，但**同一份数据内必须统一**——混用会在 `max()` 比较处抛 `TypeError`。

---

## 1. 状态契约

### 1.1 `S1Snapshot` · 局部状态（10 字段）

```python
class S1Snapshot(BaseModel):
    version: int = Field(ge=1)
    scope_id: str
    timestamp: datetime
    last_active_at: datetime | None = None
    last_decay_at: datetime
    is_conflict: bool
    compute_tier: Literal["cold", "warm", "hot"] = "warm"
    ipc: IPCState
    traits: dict[str, TraitState]
    emotions: dict[str, EmotionValue]

    @classmethod
    def valid_paths(cls) -> set[str]: ...   # 13 条
```

| 字段 | 约束 | 语义 |
| --- | --- | --- |
| `version` | `ge=1` | 乐观锁版本，下界是 1（不是 0） |
| `scope_id` | 非空 | 会话/关系标识 |
| `timestamp` | `datetime` | 本条快照代表的时刻 |
| `last_active_at` | 可空 | 上一次真实交互时刻；从未交互为 `None` |
| `last_decay_at` | `datetime` | **衰减锚点**。引擎不推进它（`apply_time_decay` 是 clock-free 的纯函数，只盖成入参 `snapshot.timestamp`）；推进由**编排层**在步骤 13 与 `timestamp` 一并写回 `now` |
| `is_conflict` | `bool` | 本轮是否处于矛盾态，驱动三段式提示词 |
| `compute_tier` | 三值枚举，默认 `warm` | 算力档位 |
| `ipc` | `IPCState` | 关系状态 |
| `traits` | `dict[str, TraitState]` | 特质集合，键为特质名 |
| `emotions` | `dict[str, EmotionValue]` | 情绪集合，键为情绪名 |

`valid_paths()` 返回 13 条可寻址路径：

```
version  scope_id  timestamp  last_active_at  last_decay_at
is_conflict  compute_tier  traits  emotions
ipc.affiliation  ipc.dominance  ipc.relationship_stage  ipc.relationship_ceiling
```

**注意**：这 13 条与增量白名单 `DELTA_ELIGIBLE_PATHS`（5 条）**不是同一件事**。前者是"可被引用的字段路径"，后者是"可被求和的增量键"。混用是本项目最容易犯的错之一——`traits` 是路径但不可增量（它是 dict 不是标量）。

### 1.2 `GlobalContext` · 全局状态（4 字段）

```python
class GlobalContext(BaseModel):
    version: int = Field(ge=0, default=0)
    baseline: BaselineState
    tension: TensionState
    pulse: PulseState
```

| 字段 | 语义 |
| --- | --- |
| `version` | 乐观锁版本，下界是 **0**（不同于 `S1Snapshot` 的 1） |
| `baseline` | 基线层：角色是谁 |
| `tension` | 张力层：累积的未化解矛盾 |
| `pulse` | 脉冲层：激素 |

**`GlobalContext` 没有 `ipc` 字段**，这不是遗漏。IPC 是会话级关系状态，归 `S1Snapshot`。任何往这里加 `ipc` 的提案都要先回答"一个用户在两个会话里的好感度该不该共用"。

两个载体的 `version` 下界不同（1 vs 0）：快照从第一次落盘起就是 v1，全局上下文允许 v0 表示"从未写过"。

### 1.3 `PulseState` · 激素脉冲（4 字段）

```python
class PulseState(BaseModel):
    dopamine: float = 50.0          # [0.0, 100.0]
    cortisol: float = 50.0          # [0.0, 100.0]
    oxytocin: float = 50.0          # [0.0, 100.0]
    delayed_impact: dict[str, float] = {}

    @field_validator("delayed_impact")
    @classmethod
    def _validate_delayed_impact(cls, v): ...
```

| 字段 | 范围 | 语义 |
| --- | --- | --- |
| `dopamine` | `[0.0, 100.0]`，默认 50.0 | 愉悦/兴奋 |
| `cortisol` | `[0.0, 100.0]`，默认 50.0 | 压力/紧张 |
| `oxytocin` | `[0.0, 100.0]`，默认 50.0 | 亲密/信任 |
| `delayed_impact` | 键必须是上述三者之一；值 `[0, 100]` | 延迟生效的增量 |

`delayed_impact` 的两条约束**必须手写 `@field_validator`**：

- `Field(ge=0, le=100)` 对 `dict` 的**值**不生效（只验证键的类型）。实测：写 `Field(ge/le)` 后 `{"dopamine": 101.0}` 仍能构造成功。
- 未知键拦截同理。`extra="forbid"` 只拦模型的顶层多余字段，不拦 dict 内部的键。

两条约束的实测行为：

```python
PulseState(delayed_impact={"dopamin": 5.0})    # ValidationError: unknown delayed_impact key
PulseState(delayed_impact={"garbage": 1.0})    # ValidationError: unknown delayed_impact key
PulseState(delayed_impact={"dopamine": 101.0}) # ValidationError: out of range [0, 100]
PulseState(delayed_impact={"dopamine": 5.0})   # OK
PulseState(delayed_impact={})                  # OK
```

`delayed_impact` **不能进增量白名单**：`pulse.delayed_impact + 1.0` 会抛 `TypeError`（dict 加 float）。

### 1.4 `IPCState` · 关系状态（4 字段）

```python
class IPCState(BaseModel):
    affiliation: float = Field(ge=-100.0, le=100.0)
    dominance: float = Field(ge=-100.0, le=100.0)
    relationship_stage: str = Field(min_length=1)
    relationship_ceiling: str = Field(min_length=1)
```

| 字段 | 范围 | 语义 |
| --- | --- | --- |
| `affiliation` | `[-100, 100]` | 亲近/疏远。**唯一参与跨域融合的字段** |
| `dominance` | `[-100, 100]` | 主导/顺从。**不随时间衰减** |
| `relationship_stage` | 非空字符串 | 当前关系阶段（`stranger` / `friend` / `lover`…） |
| `relationship_ceiling` | 非空字符串 | 关系上限 |

**`relationship_ceiling` 是字符串，不是数值。** 它不是"上限分数"，是"这段关系最多能走到哪一档"。容易误以为是数值型字段。

`IPCState` 里**没有 `tension`**——张力在 `GlobalContext.tension`。

### 1.5 `TensionState` · 张力（2 字段）

```python
class TensionState(BaseModel):
    value: float = Field(ge=0.0, le=100.0, default=0.0)
    last_feedback_at: datetime | None = None
```

`value >= 80.0` 时，档位判定绕过人设系数：`effective = max(effective, 100.0)`，`tier = "hot"`。阈值是命名常量 `TENSION_THRESHOLD = 80.0`。

### 1.6 `BaselineState` · 基线（2 字段）

```python
class BaselineState(BaseModel):
    persona: Literal["cold", "warm", "hot"] = "warm"
    persona_heat_modifier: float = Field(gt=0.0, default=1.0)

    @model_validator(mode="before")
    @classmethod
    def _derive_modifier_from_persona(cls, data): ...
```

`persona_heat_modifier` **由 validator 从系数表派生**，不需要调用方手填：

| `persona` | 派生值 |
| --- | --- |
| `cold` | 0.4 |
| `warm` | 1.0 |
| `hot` | 1.5 |

派生发生在 `mode="before"`，所以即使调用方同时传了 `persona` 与一个不一致的 `modifier`，前者也会被覆盖为查表值——这是刻意的：允许手填就等于允许两处真相。

### 1.7 `TraitState` · 特质（2 字段）

```python
class TraitState(BaseModel):
    score: int = Field(ge=0, le=100)
    visibility: Literal["unlocked", "flickering", "dormant"]
```

| 字段 | 语义 |
| --- | --- |
| `score` | **整数**，`[0, 100]`。不是 float |
| `visibility` | 三态：`unlocked`（可外显）/ `flickering`（偶发外显）/ `dormant`（不外显） |

`flickering` 的语义边界：只进 `[拉扯]` 段，不进 `[主场]`。这样"偶发的特质"表现在语气摇摆上，而不是让整段话变成另一个人。

### 1.8 `EmotionValue` · 情绪（3 字段）

```python
class EmotionValue(BaseModel):
    intensity: float = Field(ge=0.0, le=100.0)
    ttl_expires_at: datetime
    frozen_until: datetime | None = None
```

存在 `frozen_until`：冻结期内强度不变（不被衰减或新刺激改变），到点解冻。这是"情绪进档后 30 分钟冻结"的载体。

### 1.9 `GlobalDelta` · 增量（3 字段）

```python
class GlobalDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pulse: dict[str, float] = {}
    ipc: dict[str, float] = {}
    tension: float = 0.0
```

`pulse` 与 `ipc` 两个 dict 的键都必须是 `DELTA_ELIGIBLE_PATHS` 的子集，且都通过**同一个** validator 校验。

### 1.10 `HormoneDeltas` · 引擎侧增量

与 `GlobalDelta` 平行，用于引擎纯函数入参。两者共用同一个 `DELTA_ELIGIBLE_PATHS` 常量——物理上只有一份定义。

未知键拒绝的实测：

```python
HormoneDeltas(values={"dopamin": 5.0})   # ValidationError: unknown delta key
HormoneDeltas(values={})                 # OK（缺键默认 0）
HormoneDeltas(values={"dopamine": 5.0})  # ValidationError: 裸名废弃，须用 pulse.dopamine
```

### 1.11 `SuppressionDelta` · 抑制增量（1 字段；当前主链路暂未调用，预留给后续演化模块）

```python
class SuppressionDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    extra_cortisol: float = 0.0
```

**当前无任何代码生成此对象**，它是为"双轴抑制"预留的契约。契约存在、逻辑未接——这是有意保留的形状，不是半成品。

---

## 2. 增量白名单（唯一权威）

```python
DELTA_ELIGIBLE_PATHS: frozenset[str] = frozenset({
    "pulse.dopamine",
    "pulse.cortisol",
    "pulse.oxytocin",
    "ipc.affiliation",
    "ipc.dominance",
})
```

五条，点分路径。**不包含**的路径与原因：

| 路径 | 原因 |
| --- | --- |
| `pulse.delayed_impact` | dict 子结构，无法求和（`+ 1.0` 抛 `TypeError`） |
| `timestamp` / `last_decay_at` / `last_active_at` | 时间锚点，求和无意义 |
| `traits` / `emotions` | 结构化集合，非标量 |
| `scope_id` / `version` / `compute_tier` / `is_conflict` | 标识、乐观锁、枚举、布尔 |
| `ipc.relationship_stage` / `ipc.relationship_ceiling` | 字符串 |
| `baseline` / `tension` | 在 `GlobalContext` 上，分别由 `BaselineState` validator 与 `commit_delta` 处理 |

裸名（`"dopamine"`）**彻底废弃，不做兼容层**。参数错了就 `ValueError`，消息里带出键名。

---

## 3. MCP 传输契约

### 3.1 `MCPRequest` · 域模型（7 字段）

```python
class MCPRequest(BaseModel):
    request_id: str
    user_id: str
    scope_id: str
    message: str
    persona_card: str
    chat_mode: Literal["pure", "roleplay"]
    context: MCPRequestContext
```

| 字段 | 约束 | 语义 |
| --- | --- | --- |
| `request_id` | 必填 | 全链路关联键 |
| `user_id` | 必填 | 用户标识 |
| `scope_id` | 必填 | 会话/关系标识 |
| `message` | 必填 | 用户本轮输入 |
| `persona_card` | 必填 **`str`**，可为空串 | 角色卡人设文本 |
| `chat_mode` | `pure` / `roleplay` | 渲染模式 |
| `context` | `MCPRequestContext` | 时间上下文 |

**`persona_card` 是必填 `str`，不能传 `None`。** JSON `null` 到 Python 是 `None`，不会自动变成空串，会直接抛 `ValidationError: Input should be a valid string`。

### 3.2 `MCPRequestContext` · 时间上下文（2 字段）

```python
class MCPRequestContext(BaseModel):
    last_interaction_at: datetime
    current_time: datetime
```

- 两者都**必填**，都接受 naive 与 aware。
- 无时区 validator：不强制带 tz，但**同一请求内必须同一种**。混用在 `max()` 处抛 `TypeError`。
- `current_time` 是权威时钟来源；时钟回拨由编排层的钳制处理。

### 3.3 `MCPRequestDTO` · 入站 DTO（8 字段）

```python
class MCPRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    user_id: str
    scope_id: str
    message: str
    persona_card: str
    chat_mode: Literal["pure", "roleplay"]
    last_interaction_at: datetime
    current_time: datetime
```

**8 字段**——比域模型多 1 个，因为把 `context` 的两个时间字段**摊平**了。

```python
def to_domain(dto: MCPRequestDTO) -> MCPRequest:
    return MCPRequest(
        request_id=dto.request_id,
        user_id=dto.user_id,
        scope_id=dto.scope_id,
        message=dto.message,
        persona_card=dto.persona_card,
        chat_mode=dto.chat_mode,
        context=MCPRequestContext(
            last_interaction_at=dto.last_interaction_at,
            current_time=dto.current_time,
        ),
    )
```

**转换必须逐字段手写**，禁止 `MCPRequest(**dto.model_dump())`：后者会把 DTO 上将来新增的字段一并灌进 domain。domain 是 `extra="forbid"`，所以会报错——但报错暴露的是"契约不一致"，而真实原因是"DTO 多了个字段"；更糟的是若某天 domain 恰好有同名字段，多出的值会被静默接受。

### 3.4 `MCPResponse` · 域响应（7 字段）

```python
class MCPResponse(BaseModel):
    request_id: str
    status: Literal["success"]
    system_head: str
    tail_message: str
    state_snapshot: S1Snapshot
    action_intent: ActionIntent
    typing_delay_ms: int = Field(default=0, ge=0, le=5000, strict=True)
```

| 字段 | 约束 | 语义 |
| --- | --- | --- |
| `request_id` | 必填 | 回传关联键 |
| `status` | 恒 `"success"` | 失败不通过 `status` 表达（见下） |
| `system_head` | 必填 | 静态人设 + 封印 |
| `tail_message` | 必填 | 动态现象学 |
| `state_snapshot` | 必填 | 本轮结束后的快照（只读观测用） |
| `action_intent` | 必填 | 动作/贴纸意图 |
| `typing_delay_ms` | `int`，`strict=True`，`[0, 5000]`，默认 0 | 拟人化延迟 |

`typing_delay_ms` 的三条约束缺一不可：

- `strict=True`：拒绝 `"1100"`、`1100.0` 这类"看起来是整数"的输入。
- `ge=0, le=5000`：与 `delay.py` 的 `MIN_DELAY_MS` / `MAX_DELAY_MS` 一致。
- 默认 0：DTO 层允许构造"还没算延迟"的响应。

`status` 恒为 `"success"`：**死信路径也返回成功**。乐观锁三次失败是"状态没推进"，不是"请求失败"——用户仍应收到回复。故障通过 `/healthz` 观测，不通过响应状态码表达。

### 3.5 `MCPResponseDTO` · 出站 DTO（7 字段）

与 `MCPResponse` 字段一一对应（含 `state_snapshot`）。`from_domain(response)` 逐字段构造。

### 3.6 `ActionIntent` · 动作意图（2 字段）

```python
class ActionIntent(BaseModel):
    emotion: str = Field(min_length=1)
    sticker_tag: str = Field(min_length=1)
```

当前恒定构造为 `ActionIntent(emotion="neutral", sticker_tag="none")`。两个字段都是必填非空字符串——"没有动作"要用 `"none"` 表达，而不是留空。

---

## 4. 渲染契约

### 4.1 `assemble_messages` 的返回形状

```python
def assemble_messages(
    snapshot: S1Snapshot,
    config: AssemblyConfig = DEFAULT_ENGINE_CONFIG.assembly,
    lexicon: dict[str, PhenomenonEntry] | None = None,
    *,
    pure: bool = True,
    phenomenology: dict[str, PhenomenonEntry] | None = None,
    pulse: PulseState | None = None,
    persona_card: str | None = None,
) -> tuple[str, str]: ...
```

返回 `(system_head, tail_message)` 两条**独立**字符串。

### 4.2 `system_head` 的构成

| 情形 | 内容 | 长度 |
| --- | --- | --- |
| 无角色卡 | 仅封印行 | 27 |
| 有角色卡 | `persona_card` + `"\n"` + 封印行 | `len(persona_card) + 1 + 27` |

封印行逐字：

```
【输出格式封印】：绝对不要使用任何括号或星号描写动作。
```

三条性质：

- **与 `pure` 完全解耦**。`pure=True` 与 `pure=False` 的头部逐字节相同。
- **与状态完全解耦**。档位、冲突、情绪、特质都不影响头部。
- 同一 `persona_card` 内逐字节恒定 → 可进 KV Cache。

### 4.3 `tail_message` 的构成

```
【潜意识感知】：
[主场]：<主场景>
[拉扯]：<矛盾场景>            ← 仅 is_conflict=True
[旁边杵着]：<旁观者>
```

冷档例外：

```
【潜意识感知】：[系统提示：未唤醒] 角色当前处于未深度参与的旁观状态。
```

冷档时标签行与旁白**直接相接，无换行**。这是逐字契约，不是格式疏漏。

段名常量：

```python
segment_names = {
    "main": "[主场]",
    "tension": "[拉扯]",
    "bystander": "[旁边杵着]",
}
```

### 4.4 `pure` 与 `roleplay` 的差异边界

| 维度 | `pure=True` | `pure=False` |
| --- | --- | --- |
| `system_head` | 相同 | 相同 |
| 尾部标签行 | 有 | 有 |
| 尾部第二封印 | 无 | **有**（`"\n"` + 封印行） |
| 长度裁剪 | **不裁** | 受 `MAX_INJECTION_BODY_CHARS = 50` 约束 |

`pure=True` 时**刻意不裁**正文：裁剪是给"要塞进宿主上下文预算"的渲染准备的；纯渲染要保留完整三段，让下游模型看到张力。

实测长度（`persona_card="你是傲娇的猫。"` → head 恒 35；`pulse` 全 50）：

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

特质存在会把 `[主场]` 换成该特质的专属描写，因此长度随 `traits` 内容变化——上表两组的差值就是这一段的长度差。

### 4.5 尾部样例（逐字）

warm 档、无冲突：

```
【潜意识感知】：
[主场]：熟悉的熟悉感漫上来。
[旁边杵着]：空气安静了两秒。
```

warm 档、冲突态（多一段 `[拉扯]`）：

```
【潜意识感知】：
[主场]：熟悉的熟悉感漫上来。
[拉扯]：两种念头同时拽着你，谁也没赢。
[旁边杵着]：空气安静了两秒。
```

warm 档、`traits={tsundere: 85 unlocked}`（`[主场]` 换成该特质的专属描写）：

```
【潜意识感知】：
[主场]：被叫笨蛋时，你感到一阵烦躁，指尖发烫。
[旁边杵着]：空气安静了两秒。
```

cold 档：

```
【潜意识感知】：[系统提示：未唤醒] 角色当前处于未深度参与的旁观状态。
```

### 4.6 降级顺序（不可换）

```
① 长短句替换   同一条目的 long / short 二选一
② 整行丢弃     先丢 [旁边杵着]，再丢 [拉扯]
③ 字符裁剪     仅 pure=False，且只在无行可丢时发生
```

顺序换了的后果：先裁字符会把 `[旁边杵着]` 的内容切成半句，读起来像文本损坏；先丢 `[拉扯]` 会藏掉冲突态——那正是最该保留的信息。

### 4.7 其余渲染常量

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `MAX_INJECTION_BODY_CHARS` | 50 | 只作用劫持正文，不含标签行与封印行 |
| `COLD_NARRATION` | 28 字符 | 冷档旁白，逐字 |
| `lexicon_fallback` | `"熟悉的熟悉感漫上来。"` | 词典零命中时的兜底文本 |
| `seal_text` | `"绝对不要使用任何括号或星号描写动作。"` | 封印行正文（不含`【输出格式封印】：`前缀） |

---

## 5. 端口契约（6 条 Protocol）

全部方法体为 `...`，无默认实现。

### 5.1 `GlobalStatePort` · 全局状态

```python
class GlobalStatePort(Protocol):
    def get(
        self, tx: Transaction, scope_id: str
    ) -> tuple[S1Snapshot | None, GlobalContext | None]: ...
    def save(
        self,
        tx: Transaction,
        snapshot: S1Snapshot,
        expected_snapshot_version: int,
        context: GlobalContext,
        expected_context_version: int,
    ) -> bool: ...
```

四条由签名表达的纪律：

1. **`get` 一次返回配对的两个载体**。拆成两个方法就会允许"快照 v5 配上下文 v4"的撕裂组合。
2. **`get` 必须带 `scope_id`**。它不是便利参数而是**读键**：`s1_snapshot` 每个会话一行，无 scope 的读在出现第二个会话的那一刻就是读错行——2026-11-07 实测过这条路径会静默把 A 会话的状态重写到 A 上、B 会话一行未动，retry 计数为 0、无死信，完全不报错。
3. **`tx` 是第一个参数**。"禁止隐式连接"被写进签名，实现无法偷偷开新连接。
4. **`save` 返回 `bool`，永不抛冲突异常**。CAS 失败是并发常态，不是错误。

### 5.2 `Transaction` · 事务

```python
class Transaction(Protocol):
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
```

### 5.3 `StateRepositoryPort` · 单载体仓储

```python
class StateRepositoryPort(Protocol):
    def load(self) -> S1Snapshot | None: ...
    def save_if_version_match(self, snapshot: S1Snapshot, expected_version: int) -> bool: ...
```

### 5.4 `ObservabilityPort` · 观测

```python
class ObservabilityPort(Protocol):
    def emit_agent_run(self, payload: Mapping[str, object]) -> None: ...
    def emit_token_usage(self, payload: Mapping[str, object]) -> None: ...
    def check_token_budget(self, estimated_tokens: int) -> bool: ...
```

### 5.5 `AuditPort` · 反事实审计（当前主链路暂未调用，预留给后续演化模块）

```python
class AuditPort(Protocol):
    def counterfactual_audit(
        self,
        events: Sequence[EmotionalEvent],
        candidate_param: Mapping[str, object],
    ) -> Mapping[str, object]: ...
```

### 5.6 `ConsolidationPort` · 离线巩固（当前主链路暂未调用，预留给后续演化模块）

```python
class ConsolidationPort(Protocol):
    def offline_dream(self, events: Sequence[EmotionalEvent]) -> None: ...
    def replay_rumination(self, events: Sequence[EmotionalEvent]) -> None: ...
```

`ports.__all__` 共 6 项：`AuditPort` / `ConsolidationPort` / `GlobalStatePort` / `ObservabilityPort` / `StateRepositoryPort` / `Transaction`。

---

## 6. 持久化契约

### 6.1 四张表

```sql
CREATE TABLE global_context (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL CHECK (version >= 0),
    payload TEXT NOT NULL
);

CREATE TABLE s1_snapshot (
    scope_id TEXT PRIMARY KEY,
    version  INTEGER NOT NULL CHECK (version >= 0),
    payload  TEXT NOT NULL
);

CREATE TABLE dead_letter_deltas (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       TEXT NOT NULL,
    context_version  INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    failure_reason   TEXT NOT NULL,
    failed_at        TEXT NOT NULL
);

CREATE TABLE stimulus_ledger (
    scope_id  TEXT NOT NULL,
    dimension TEXT NOT NULL,
    at        TEXT NOT NULL,
    delta     REAL NOT NULL
);
```

四条设计约束：

| 表 | 约束 | 理由 |
| --- | --- | --- |
| `global_context` | `CHECK (id = 1)` | 单行表；用 CHECK 而不是应用层保证"只有一行" |
| `s1_snapshot` | `scope_id` 主键 | 每个会话一行 |
| `dead_letter_deltas` | 六个 NOT NULL 列 | 死信必须完整，缺一列就无法复现 |
| `stimulus_ledger` | **不建索引** | 门禁要求"`ALL_DDL` 每条都是 `CREATE TABLE`"，建索引会破坏这条锁 |

`stimulus_ledger.delta` 存的是**限幅后**的值（实际生效量），不是请求值。

### 6.2 CAS 语句

```sql
-- 快照
UPDATE s1_snapshot SET version = ?, payload = ?
WHERE scope_id = ? AND version = ?;

-- 全局上下文
UPDATE global_context SET version = ?, payload = ?
WHERE id = 1 AND version = ?;
```

`rowcount == 0` 即 CAS 失败。两条都成功才返回 `True`。

### 6.3 限幅查询

```sql
-- 两小时窗口（左开右闭）
SELECT at, delta FROM stimulus_ledger
WHERE scope_id = ? AND dimension = ? AND at > ? AND at <= ?
ORDER BY at;

-- 日累计（正负各一）
SELECT
    COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0.0),
    COALESCE(SUM(CASE WHEN delta < 0 THEN delta END), 0.0)
FROM stimulus_ledger
WHERE scope_id = ? AND dimension = ? AND at >= ? AND at <= ?;

-- 追加
INSERT INTO stimulus_ledger (scope_id, dimension, at, delta) VALUES (?, ?, ?, ?);

-- 清理
DELETE FROM stimulus_ledger WHERE scope_id = ? AND at < ?;
```

时间一律 `to_utc_naive()` 归一化：SQLite 按**字典序**比较文本，只有格式与时区统一，字典序才等价于时间序。

清理边界是 `min(day_start_utc, window_start_utc)`——**取 min 而不是日边界**：跨零点后 `now - 2h` 落进前一天，只按日边界清理会误删仍在窗口内的行。

### 6.4 适配器方法表（`SqliteStateStore`，12 个公开 + 2 个内部 CAS 辅助）

| 方法 | 签名 |
| --- | --- |
| `connect` | `() -> None` |
| `close` | `() -> None` |
| `transaction` | `() -> _Tx` |
| `save_if_version_match` | `(snapshot, expected_version: int) -> bool` |
| `load` | `() -> S1Snapshot \| None` |
| `get` | `(tx: _TxHandle, scope_id: str) -> tuple[S1Snapshot \| None, GlobalContext \| None]` |
| `save` | `(tx, snapshot, expected_snapshot_version: int, context, expected_context_version: int) -> bool` |
| `record_dead_letter` | `(tx, request_id, context_version, snapshot_version, reason, failed_at) -> None` |
| `dead_letter_count` | `() -> int` |
| `load_stimulus` | `(tx, scope_id, dimension, now) -> tuple[list[tuple[datetime, float]], tuple[float, float]]` |
| `append_stimulus` | `(tx, scope_id, dimension, at, applied_delta: float) -> None` |
| `prune_stimulus` | `(tx, scope_id, now) -> None` |
| `_cas_upsert_snapshot` | `(snapshot, expected_version: int) -> bool` |
| `_cas_upsert_context` | `(context, expected_version: int) -> bool` |

适配器**永不 commit、永不重试**。两者都是编排层职责。

`load_stimulus` 返回裸 `tuple` 而非引擎的 `StimulusRecord`：adapter 被禁止 import engine，类型转换落在编排层。

---

## 7. 数值常量契约

### 7.1 七个 `PLACEHOLDER_*`（`packages/core/domain/config.py`）

| 常量 | 值 | 语义 |
| --- | --- | --- |
| `PLACEHOLDER_PERSONA_HEAT_MODIFIER` | `{'cold': 0.4, 'warm': 1.0, 'hot': 1.5}` | 人设对热度的乘法系数 |
| `PLACEHOLDER_STIMULUS_UNIT_POINTS` | `8.0` | 1 个刺激单位 = 8 个激素点数 |
| `PLACEHOLDER_RESIDENT_BONUS_RATE` | `0.1` | 每日驻留加成 |
| `PLACEHOLDER_RESIDENT_RESET_DAYS` | `3` | 驻留天数重置周期 |
| `PLACEHOLDER_IPC_BONUS_RATE` | `0.1` | 亲密度加成 |
| `PLACEHOLDER_TENSION_THRESHOLD` | `80.0` | 张力绕过档位的阈值 |
| `PLACEHOLDER_MAX_QUEUE_RETRIES` | `3` | 队列重试上限 |

### 7.2 六个刺激系数（`packages/core/engine/stimulus.py`）

| 常量 | 值 |
| --- | --- |
| `PLACEHOLDER_STIMULUS_POS_DOPAMINE_RATE` | 8.0 |
| `PLACEHOLDER_STIMULUS_POS_OXYTOCIN_RATE` | 6.0 |
| `PLACEHOLDER_STIMULUS_POS_AFFILIATION_RATE` | 6.0 |
| `PLACEHOLDER_STIMULUS_NEG_CORTISOL_RATE` | 8.0 |
| `PLACEHOLDER_STIMULUS_AROUSAL_THRESHOLD` | 0.3 |
| `PLACEHOLDER_STIMULUS_AROUSAL_CORTISOL_RATE` | 10.0 |

### 7.3 固定物理常量（不是占位）

| 常量 | 值 | 位置 |
| --- | --- | --- |
| `HORMONE_BASELINE` | 50.0 | `dynamics.py` |
| `HORMONE_FLOOR` / `HORMONE_CEILING` | 0.0 / 100.0 | `dynamics.py` |
| `IPC_FLOOR` / `IPC_CEILING` | −100.0 / 100.0 | `dynamics.py` |
| `SINGLE_STIMULUS_CAP` | 1.0 | `dynamics.py` |
| `WINDOW_CAP` | 3.0 | `dynamics.py` |
| `DAILY_CAP` | 4.0 | `dynamics.py` |
| `TWO_HOUR_WINDOW` | `timedelta(seconds=7200)` | `dynamics.py` + `stimulus_ledger.py`（两处，双向钉值） |
| `TENSION_FLOOR` / `TENSION_CEILING` | 0.0 / 100.0 | `commit.py` |
| `MAX_INJECTION_BODY_CHARS` | 50 | `assembler.py` |
| `TX_MAX_RETRIES` | 3 | `use_case.py` |
| `_TRACK_B_TIMEOUT` | 0.8 秒 | `use_case.py` |
| `READ_TIMEOUT_SECONDS` | 1.0 | `healthz.py` |
| `DETAIL_MAX_CHARS` | 200 | `healthz.py` |
| `LEGACY_QUEUE_PENDING` | 0 | `healthz.py` |

**13 个数值常量**（7 + 6）基于当前词典校准，需真实对话数据压测后定稿。

### 7.4 打字延迟常量（`delay.py`）

| 常量 | 值 |
| --- | --- |
| `BASE_DELAY_MS` | 300 |
| `PER_CHAR_DELAY_MS` | 20 |
| `MESSAGE_LENGTH_CAP` | 100 |
| `MIN_DELAY_MS` / `MAX_DELAY_MS` | 500 / 5000 |
| `CONFLICT_FACTOR` | 1.3 |
| `SHY_FACTOR` | 2.0 |
| `SHY_OXYTOCIN_MIN` | 70.0 |
| `SHY_CORTISOL_MIN` / `SHY_CORTISOL_MAX` | 45.0 / 70.0 |
| `FURIOUS_FACTOR` | 0.2 |
| `FURIOUS_DOPAMINE_MAX` | 40.0 |
| `FURIOUS_CORTISOL_MIN` | 80.0 |

---

## 8. 健康检查契约

```python
def create_healthz_app(store: SqliteStateStore, usecase: EmotionUseCase) -> FastAPI: ...

# GET /healthz -> 200
{
  "version": int,
  "pulse": {dopamine, cortisol, oxytocin, delayed_impact} | None,
  "pulse_source": "global_context" | "unavailable",
  "database": {"connected": bool, "detail": str | None},
  "queue_backlog": {
      "dead_letter_count": int,
      "pending_retry_count": int,
      "legacy_queue_pending": int
  }
}
```

五条契约：

1. **恒 200**。故障由 `database.connected = false` 表达，不用 5xx——5xx 会与"进程死了"混淆。
2. **无鉴权**。状态摘要不含敏感信息。
3. **`version` 与 `pulse` 同源**（同一行 `global_context`）。混合来源会让一个字段背后有两个真相源。
4. **`legacy_queue_pending` 恒 0**。旧队列已废弃，字段保留以维持响应形状稳定。
5. **不创建数据库文件**。用 `mode=ro` 的只读 URI 连接。

三种实测响应：

```json
// 空库
{"version": 0, "pulse": null, "pulse_source": "unavailable",
 "database": {"connected": true, "detail": null},
 "queue_backlog": {"dead_letter_count": 0, "pending_retry_count": 0, "legacy_queue_pending": 0}}

// 有状态
{"version": 1,
 "pulse": {"dopamine": 50.0, "cortisol": 58.0, "oxytocin": 50.0, "delayed_impact": {}},
 "pulse_source": "global_context",
 "database": {"connected": true, "detail": null},
 "queue_backlog": {"dead_letter_count": 0, "pending_retry_count": 2, "legacy_queue_pending": 0}}

// 库不存在
{"version": 0, "pulse": null, "pulse_source": "unavailable",
 "database": {"connected": false, "detail": "OperationalError: unable to open database file"},
 "queue_backlog": {"dead_letter_count": 0, "pending_retry_count": 2, "legacy_queue_pending": 0}}
```

---

## 9. 其余域模型

### 9.1 `RoutingResult` · 路由结果（7 字段，frozen）

```python
class RoutingResult(BaseModel):
    stimulus_delta: dict[str, float] = {}       # 值域 [-100, 100]
    risk_flag: Literal["none", "boundary", "conflict"] = "none"
    scenario_tag: str
    is_degraded: bool = False
    source: Literal["track_a", "track_b"]
    route_id: str
    matched_scope: Literal["private", "group"]
```

`scenario_tag` 的六种取值（`SCENARIO_TAGS`）：

```
neutral | shy_hesitant | defensive_cold | warm_open | teasing_playful | distressed_fragile
```

### 9.2 `EmotionalEvent` · 情绪事件（9 字段，frozen；当前主链路暂未调用，预留给后续演化模块）

```python
class EmotionalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    emotion_key: str
    intensity: float = Field(ge=0.0, le=100.0)
    severity: Literal["high", "medium", "low"]
    summary: str = Field(min_length=1, max_length=200)
    visibility_scope: Literal["private", "group"]
    scope_id: str
    created_at: datetime
    raw_retained: bool = False
```

`raw_retained` 默认 `False`：原始文本默认不留存，只保留摘要。这是隐私姿态的默认值。

### 9.3 `CandidateProposal` / `ProposalItem` · 参数校准提案（当前主链路暂未调用，预留给后续演化模块）

```python
class CandidateProposal(BaseModel):
    candidate_id: str
    baseline_hash: str
    proposals: list[ProposalItem] = Field(min_length=1)
    status: Literal["pending_approval"]

class ProposalItem(BaseModel):
    category: str
    target: str = Field(pattern=r"^[a-z_]+(\.[a-z_]+)+$")
    old_value: int | float | str
    new_value: int | float | str
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
```

`target` 的正则 `^[a-z_]+(\.[a-z_]+)+$` 强制点分路径格式——与 `DELTA_ELIGIBLE_PATHS` 同一套命名约定。

`old_value` / `new_value` 是 `int | float | str` 联合类型：待校准的不只有数值常量，`daily_cap_mode` 这类字符串枚举也可能是候选。

### 9.4 `UserProfileRef` · 用户画像引用（3 字段；当前主链路暂未调用，预留给后续演化模块）

```python
class UserProfileRef(BaseModel):
    user_id: str = Field(min_length=1)
    cached_tags: tuple[Annotated[str, Field(min_length=1)], ...] = Field(
        default=(), max_length=8
    )
    tags_fetched_at: datetime | None = None
```

`cached_tags` 是**元组**（不可变），上限 8 个，每个元素非空串。

元素级非空用 `Annotated[str, Field(min_length=1)]`：`Field(max_length=8)` 只约束元组长度，不约束元素。

### 9.5 引擎配置（`config.py`，5 个 dataclass）

```python
Condition = tuple[str, Literal["up", "down"]]

@dataclass(frozen=True)
class ConflictRule:
    name: str
    conditions: tuple[Condition, ...]
    priority: int

@dataclass(frozen=True)
class ConflictConfig:
    rules: tuple[ConflictRule, ...]
    epsilon: float

@dataclass(frozen=True)
class DynamicsConfig:
    hormone_per_hour: dict[str, float]
    affiliation_per_hour: float
    daily_cap_mode: str
    delayed_impact_window_hours: float = 2.0

@dataclass(frozen=True)
class AssemblyConfig:
    segment_names: dict[str, str]
    seal_text: str
    lexicon_fallback: str

@dataclass(frozen=True)
class EngineConfig:
    conflict: ConflictConfig
    dynamics: DynamicsConfig
    assembly: AssemblyConfig
```

`DEFAULT_ENGINE_CONFIG` 实值：

```
rules = [
  ConflictRule("dopamine_up_and_cortisol_up",
               (("pulse.dopamine", "up"), ("pulse.cortisol", "up")), 10),
  ConflictRule("affiliation_up_and_dominance_down",
               (("ipc.affiliation", "up"), ("ipc.dominance", "down")), 20),
]
epsilon = 0.05
hormone_per_hour = {"dopamine": 1.0, "cortisol": 1.0, "oxytocin": 1.0}
affiliation_per_hour = 0.5
daily_cap_mode = "natural_day"
delayed_impact_window_hours = 2.0
segment_names = {"main": "[主场]", "tension": "[拉扯]", "bystander": "[旁边杵着]"}
seal_text = "绝对不要使用任何括号或星号描写动作。"
lexicon_fallback = "熟悉的熟悉感漫上来。"
```

---

## 10. 契约一致性检查清单

以下命令在 `sys-mcp-whisper/` 内执行（这些是相对 `tests/` 的路径，与仓库根的
`scripts/` 门禁命令不同）：

```bash
cd "E:/AI Projects/FatFish-Whisper/sys-mcp-whisper"

# 反射测试：每个 domain 模型都 extra="forbid"
python -m pytest tests/domain/test_contracts.py -q

# 增量白名单唯一来源
python -m pytest tests/domain/test_delta_key_unified.py -q

# valid_paths 数量锁（13 条）
python -m pytest tests/domain/test_valid_paths_single_source.py -q

# DTO 不出现在 packages/ 内
python -m pytest tests/adapters/test_inbound_dto.py -q

# 协议形状锁
python -m pytest tests/ports/test_protocol_shapes.py -q

# 全量
python -m pytest -q        # 853 passed
```

契约的三条自检原则：

1. **集合计数要锁**。19 个 domain 模型全部 `extra="forbid"`，其中 4 个额外 `frozen=True`（`EmotionalEvent` / `GlobalDelta` / `RoutingResult` / `SuppressionDelta`）。测试用反射参数化枚举全部 19 个，而不是抽样——加模型必须同步名单，不改必红。
2. **数量与内容都要锁**。`valid_paths()` 锁 13 条，同时断言其中含 `last_active_at`（P2.8-C3 加入的那一条）——只锁数量会在"删一个加一个"时失效。注意它锁的是**结构清单**，与增量白名单 `DELTA_ELIGIBLE_PATHS`（5 条）是两套东西。
3. **反向断言必须存在**。只断言"非法值被拒"无法发现"拒绝一切"的实现错误；每条拒绝用例旁边都要有一格"合法值必须通过"。
