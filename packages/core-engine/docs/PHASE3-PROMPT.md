# 第三阶段开发提示词 · 前置防抖队列 + HTTP 接口打通

> 本文件是第三阶段的**开发提示词**，直接可投喂给编码代理。
> 阶段二已交付的部分在文末「已有能力」里列清，不要重复实现。

---

## 0. 目标

把「情绪机」接上 QQ 插件：`main.py` 里加**前置防抖队列** + **HTTP 接口**，
让 Adapter（NapCat / OneBot）能把消息打进来、把 Actions 拿回去。

```
Adapter ──HTTP──> api.py ──push──> debounce_queue ──flush(500ms)──> consumer
                                                                      │
                                           ┌──────────────────────────┘
                                           ▼
                          generate_event_key(user_id, merged_text, batch_id)
                                           │
                          EmotionEngine.update_emotion(event_key=...)
                                           │
                          PromptBuilder.build_final_prompt(...)
                                           │
                          response_queue ──> HTTP 层 / WebSocket 推送
```

---

## 1. 铁律：event_key 的生成位置与算法

**绝不在 HTTP 层生成 event_key。** 它只能由防抖队列的消费者在**冲刷（flush）时**生成。

```python
# debounce_queue.py —— 消费者里生成，不是 api.py
def generate_event_key(user_id: str, merged_text: str, batch_id: str) -> str:
    raw = f"{user_id}:{merged_text}:{batch_id}"
    return hashlib.md5(raw.encode()).hexdigest()
```

`batch_id` 由**每次冲刷**分配，形如 `f"{int(time.time()*1000)}-{seq}"`（毫秒时间戳 + 进程内自增），
一次 flush 里合并进来的 N 条消息共享同一个 `batch_id`。

> 阶段二已在 `engine.py` 提供 `new_batch_id()` / `make_event_key(user_id, merged_text, batch_id)` /
> `Debouncer`（返回 `Batch(text, batch_id)`，自带 `.event_key(user_id)`），**直接复用，不要重写**。

### 为什么必须带 batch_id 而不是纯文本哈希

| 场景 | 纯文本哈希（错误） | 带 batch_id（正确） |
|---|---|---|
| A. 网络重试：同一条「嗯嗯」1 秒内被推 3 次 | 3 次落在同一窗口 → 合并成 1 条 → 只扣 1 次（碰巧对） | 3 次在**同一个 batch** 里 → 1 个 key → 只扣 1 次 |
| B. 用户连发：发「嗯嗯」→ Bot 回复 → 再发「嗯嗯」 | 15s 内同文本 → 第二次被**吞掉** ❌ | 两次 flush → 两个 batch_id → 两个 key → **各算一次** ✅ |

**结论：去重靠「批次身份」，不靠「文本内容」。** 引擎绝不按文本内容去重。

### 15s 窗口用在哪

只用在**入口的幽灵消息过滤**（`IngressDeduper`，阶段二已提供）：

- 有平台 `message_id` 时按 `message_id` 判重（最准，优先用）；
- 没有时退化为 `(user_id, text)` 的 3s 短窗口。

命中即**丢弃**（不新建批次、不入队），这才是吃掉 NapCat 重推的正确姿势。
注意「丢弃」≠「合并」：合并会把用户真心连发的两句并成一句。

---

## 2. 数据流转三条规则

### 规则 1 · HTTP 层（`api.py` / 现有 `main.py`）

1. 收到请求 → 立刻 `await debounce_queue.push(user_id, message, message_id=...)`；
2. **不生成任何 event_key**；
3. 立即返回 `{"status": "queued", "queue_id": "..."}`；
   若调用方需要结果，用 `queue_id` 轮询或走 WebSocket 推送，**不要阻塞 HTTP 连接**。

### 规则 2 · 队列消费端（`debounce_queue.py`）

定时器触发 **500ms** 冲刷后，按顺序执行：

1. 提取合并后的 `merged_text`（同一 batch 内多条消息按 `\n` 拼接）；
2. 在此处调用 `generate_event_key(user_id, merged_text, batch_id)`；
3. 调用 `EmotionEngine.update_emotion(user_id, target_score, event_key=key)`
   —— 或直接 `handle_message(user_id, merged_text, debounce=False, event_key=key)`
   （后者会跑完 Track A → Track B → 更新 → PromptBuilder 全链路，推荐）；
4. 用 `PromptBuilder.build_final_prompt(inner_os, merged_text)` 组装最终 Prompt；
5. 把 `{prompt, actions, inner_os, debug}` 塞进 `response_queue`，
   HTTP 层轮询取走，WebSocket 则直接推送。

### 规则 3 · 引擎层（`engine.py`，阶段二已实现，勿改语义）

`update_emotion` 必须接收 `event_key`，并持久化到 `UserState.last_event_key`：

- 传进来的 `event_key` 与数据库里 `last_event_key` **相同** → 直接 `return` 当前状态，
  `applied=False`，**不扣分、不改状态、不动 `last_update`**；
- 不同 → 正常走衰减 + 平滑，并覆盖 `last_event_key`。

> 为什么落库而不是放 diskcache：diskcache 会被淘汰/清理，数据库不会。
> 老库无需手动迁移，`EmotionEngine.__init__` 里的 `ensure_schema()` 会自动
> `ALTER TABLE user_states ADD COLUMN last_event_key VARCHAR`。

---

## 3. 并发与顺序

- **队列单线程串行消费**：一个消费者协程按用户分区顺序处理，天然消除
  「好感度 ±1/±2 并发误差」——**不要给好感度额外加锁**。
- 引擎内部的 `diskcache` 跨进程锁（`lock:state:{user_id}`）保留，用于多进程/多 worker 兜底。
- 同一用户的多条消息必须**保序**（用 `asyncio.Queue` 或按 user_id 分片），
  否则「先骂后哄」会被判反。

---

## 4. 验收清单（第三阶段交付前必须逐条自测）

1. **队列合并**：500ms 内连发 3 条 → 只触发 1 次引擎调用，`merged_text` 是三行拼接。
2. **场景 A（重推）**：同一 `message_id` 推 3 次 → 引擎只扣 1 次分。
3. **场景 B（连发）**：两次 flush 的同一句「嗯嗯」→ 引擎扣 2 次分，`debug.event_key` 不同。
4. **HTTP 不阻塞**：`POST /api/v1/chat` 在 50ms 内返回 `{"status":"queued"}`。
5. **结果可取**：按 `queue_id` 能在 N 秒内拿到 `{prompt, actions, inner_os}`。
6. **保序**：同一用户「先骂后哄」两条，最终情绪必须是「哄」的结果，不能颠倒。
7. **不崩**：LLM 挂了 / 队列满 / 数据库锁住时，接口返回明确错误码，进程不退出。
8. **回归**：`pytest tests -q` 全绿（阶段二 99 个用例不许退化）。

---

## 5. 已有能力（阶段二交付，直接复用）

| 能力 | 位置 | 说明 |
|---|---|---|
| `make_event_key(user_id, merged_text, batch_id)` | `engine.py` | md5 32 位 |
| `new_batch_id()` | `engine.py` | 毫秒时间戳 + 自增序号 |
| `Debouncer.merge()` → `Batch(text, batch_id)` | `engine.py` | `Batch.event_key(user_id)` |
| `IngressDeduper.is_duplicate(uid, text, message_id)` | `engine.py` | 入口幽灵消息过滤 |
| `EmotionEngine.update_emotion(..., event_key=)` | `engine.py` | 落库 `last_event_key` 幂等 |
| `EmotionEngine.handle_message(..., event_key=)` | `engine.py` | 全链路主入口 |
| `PromptBuilder.build_inner_os / build_final_prompt` | `prompt_builder.py` | 三段式注入 |
| `UserState.last_event_key` | `models.py` | 自动迁移老库 |

## 6. 不要做的事

- ❌ 不要在 `api.py` 里 `hash(user_id + message)` 生成 event_key（会吞掉连发）。
- ❌ 不要在 HTTP 层 await 引擎结果（会拖垮 Adapter 的重试逻辑）。
- ❌ 不要给好感度单独加锁（队列串行已解决）。
- ❌ 不要用文本内容做引擎级去重（那是入口过滤器的职责）。
- ❌ 不要改 `event_key` 的语义（同 key = 同一次交互，只生效一次）。
