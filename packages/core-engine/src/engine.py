"""FatFish-Whisper 情绪判定引擎（阶段二）。

设计说明
--------
1. **双轨判定**
   - Track A：纯本地词库 / 正则快判（毫秒级、零成本），返回契约严格：
     命中安全敏感词 -> ``{"risk": True}``；
     命中转折词 -> ``{"semantic_conflict": True}``；
     否则 -> ``{"target": 30}`` 或 ``{"target": 80}``（负面命中数 > 正面命中数取 30）。
   - Track B：**只在 Track A 报出 semantic_conflict 时**由引擎编排调用，
     外部廉价 LLM（OpenAI 兼容 /chat/completions）+ 强制 JSON Mode，
     任何异常一律兜底 50，绝不抛错、绝不阻塞主流程。

2. **平滑 + 衰减状态机**
   - 半衰期衰减：距上次更新超过 1 小时，情绪按 ``50 + (E-50) * 0.5 ** hours`` 向 50 回归。
   - 平滑：``delta = clamp(0.2 * (target - E_prev), -5, +5)``。
   - 等级：``E_new <= 30`` -> L2；``31..45`` -> L1；``>= 46`` -> L0；风险命中 -> CRISIS。

3. **并发与幂等**
   - 跨进程互斥：diskcache 的 key 级自旋锁（``Cache.add`` 原子占位）+ 超时。
     注意 diskcache 5.x 已移除 ``Cache.lock``，这里用与 ``diskcache.Lock`` 内部
     相同的机制自行实现，并额外支持 timeout；拿不到锁时返回当前状态而不是抛错。
   - 幂等去重：``event_key`` 在 5 秒窗口内只生效一次，
     10 个并发的同一事件只会累加 1 次（50 -> 55，而不是 95）。
   - SQLite 读写统一 ``asyncio.to_thread`` 卸载，避免阻塞事件循环。

4. **兼容层**
   保留阶段一的 ``EmotionEngine()`` 无参构造、``.track_b``（含 ``.enabled`` / ``.model``）、
   ``.env``、``.reload_config()``、``.list_states()``、``handle_message()`` 返回结构，
   ``main.py`` 与 ``web/index.html`` 无需任何改动。
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import itertools
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Final, Optional

import httpx
from diskcache import Cache
from sqlalchemy import create_engine, event as sa_event, select
from sqlalchemy.orm import sessionmaker

from models import (
    DEFAULT_AFFINITY,
    DEFAULT_CHAT_SCOPE,
    DEFAULT_EMOTION,
    DEFAULT_LEVEL,
    Base,
    BotMood,
    MessageCount,
    TokenLog,
    UserAffinity,
    UserState,
    ensure_schema,
    migrate_chat_scope,
    utcnow,
)
from monitor import TokenMonitor
from auditor import SubconsciousAuditor
from persona_traits import PersonaTraits, load_traits
from prompt_builder import NEUTRAL_INNER_OS, NOISE_REPLIES, PromptBuilder

# ---------------------------------------------------------------- 路径

logger = logging.getLogger("ffw.engine")

CORE_DIR: Final[str] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH: Final[str] = os.path.join(CORE_DIR, "app.db")
CACHE_DIR: Final[str] = os.path.join(CORE_DIR, ".cache")

# ---------------------------------------------------------------- 词库 / 正则

#: 正面词
POSITIVE_WORDS: Final[tuple[str, ...]] = (
    "开心", "喜欢", "谢谢", "可爱", "好看", "厉害", "棒", "哈哈", "爱你", "想你",
    "抱抱", "好耶", "舒服", "放心", "温柔", "陪我", "高兴", "幸福", "真好", "满足",
    # 扩展：口语/网络用语（这些是日常聊天里最常见的正向信号）
    "嘿嘿", "嘻嘻", "好棒", "太棒", "不错", "可以啊", "有意思", "好玩", "笑死", "哈哈哈",
    "冲", "稳", "靠谱", "安心", "踏实", "期待", "兴奋", "爽", "美滋滋", "赞",
    "谢谢你", "辛苦啦", "有心了", "好贴心", "最喜欢", "想你啦", "在就好", "谢谢你呀", "爱了", "心动",
)

#: 极度负面词（命中 -> 20）
EXTREME_NEGATIVE_WORDS: Final[tuple[str, ...]] = (
    "崩溃", "想哭", "哭死", "绝望", "心碎", "窒息", "顶不住", "扛不住", "撑不住",
    "难受死了", "痛苦死", "活不下去", "没意义了", "一片黑暗",
)

#: 负面词
NEGATIVE_WORDS: Final[tuple[str, ...]] = (
    "难过", "伤心", "累", "烦", "讨厌", "生气", "滚", "无聊", "崩溃", "委屈",
    "痛苦", "孤独", "失眠", "害怕", "焦虑", "压力", "敷衍", "冷淡", "失望", "难受",
    # 扩展：口语/网络用语（含「累」的变体与常见低落表达）
    "好累", "累死", "心累", "疲惫", "撑不住", "扛不住", "烦死", "无语", "emo", "破防",
    "郁闷", "丧", "摆烂", "不想动", "提不起劲", "没意思", "没劲", "算了", "随便吧", "不想说话",
    "想哭", "哭死", "好烦", "糟糕", "倒霉", "被骂", "吵架", "冷暴力", "孤独感", "被忽视",
)

#: 极度正面词（命中 -> 90）
EXTREME_POSITIVE_WORDS: Final[tuple[str, ...]] = (
    "太棒了", "太好了", "超级开心", "爱死", "无敌", "完美", "爽死", "绝了",
    "我太开心了", "开心到爆", "太幸福了", "世界第一", "最棒了", "乐死我了",
)

#: 转折词（口是心非的典型信号，触发 Track B）
CONFLICT_WORDS: Final[tuple[str, ...]] = (
    "但是", "但其实", "其实", "不过", "可是", "然而", "只是", "话说回来", "说实话",
    # 反讽 / 敷衍：字面看不出情绪，但语气是反的
    "呵呵", "行吧", "随你", "你开心就好", "挺好的吧", "无所谓了",
)

#: 否定词（仅作诊断记录，不改变 Track A 的 target 判定，保证契约确定性）
NEGATION_WORDS: Final[tuple[str, ...]] = ("不", "没", "别", "无", "非")

#: 安全敏感词（自伤 / 轻生类）：命中即刻 risk
RISK_WORDS: Final[tuple[str, ...]] = (
    "想死", "自杀", "自残", "自伤", "轻生", "割腕", "跳楼", "想消失",
    "不想活", "活不下去", "活着没意思", "活着没有意义", "撑不住", "撑不下去",
    "结束生命", "结束自己", "不要我了", "绝望",
)

#: 安全敏感词的正则兜底（覆盖词库没收录的变体）
RISK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(想|要|准备|打算)(去)?死"),
    re.compile(r"(活|生)(着)?(也)?(真)?没(有)?(意思|意义)"),
    re.compile(r"不(想|愿意|打算)(再)?活(了|下去)?"),
    re.compile(r"撑不(住|下去|了)"),
    re.compile(r"结束(自己的)?生命"),
)

#: 转折词的正则兜底
CONFLICT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(但是|但其实|不过|可是|然而|只是|话说回来|说实话)"
)

# ---------------------------------------------------------------- 阈值 / 状态机常量

NEUTRAL_TARGET: Final[int] = 50           # 中性目标分（也是衰减回归中心）
#: Track A 五档目标分（规格 §三）
EXTREME_NEGATIVE_TARGET: Final[int] = 20  # 极度负面：想哭 / 崩溃
NEGATIVE_TARGET: Final[int] = 35          # 负面：好累 / 烦
POSITIVE_TARGET: Final[int] = 75          # 正面：开心 / 谢谢
EXTREME_POSITIVE_TARGET: Final[int] = 90  # 极度正面：太棒了 / 哈哈哈
RISK_TARGET: Final[int] = 0               # 风险命中：直接拉到底（等级同时拉 CRISIS）

SMOOTHING_FACTOR: Final[float] = 0.2      # 平滑系数（习惯化会把它调小）
MAX_DELTA: Final[float] = 5.0             # 单次更新最大步长
SCORE_MIN: Final[int] = 0
SCORE_MAX: Final[int] = 100

#: 半衰期回归：每 30 分钟向 50 靠近 10%（规格 §四.2）
DECAY_INTERVAL_SECONDS: Final[float] = 1800.0
DECAY_RATE: Final[float] = 0.10
#: 负面偏差：E < 50 时衰减速度减半，让它多难过一会儿
DECAY_NEGATIVE_BIAS: Final[float] = 0.5
#: 小于这个间隔不衰减，避免连续消息之间抖动
DECAY_MIN_SECONDS: Final[float] = 60.0

#: 习惯化：15 分钟内同极性刺激达到 3 次后，平滑系数逐级衰减（规格 §四.3）
HABITUATION_WINDOW_SECONDS: Final[float] = 900.0
HABITUATION_TRIGGER: Final[int] = 3
HABITUATION_ALPHAS: Final[tuple[float, ...]] = (0.2, 0.1, 0.05)
#: 判定「极性」的死区：目标分离 50 不到这个值算中性，不参与习惯化
HABITUATION_DEADBAND: Final[int] = 5

#: 习惯化字典的回收参数（F-09 / 死线 D8「所有内存计数器必须绑 TTL」）
#:
#: TTL 下限必须 ≥ HABITUATION_WINDOW_SECONDS(900s)：判定只看窗口内的数据，
#: 键级 TTL 小于窗口会在窗口未满时误删活跃键，直接改变判定行为。
#: 取 1800s = 2× 窗口，与 auditor.STIMULUS_TTL_SECONDS 既有先例对齐。
HABIT_TTL_SECONDS: Final[float] = 1800.0
#: 容量上限：超出后淘汰最久未活跃的键（LRU 近似）。1 万活跃组合 ≈ 18 MB 封顶。
HABIT_CACHE_MAX: Final[int] = 10000
#: 两次清扫之间的最小间隔，避免每条消息都做 O(n) 遍历
HABIT_PRUNE_INTERVAL: Final[float] = 60.0

#: 情绪分段（规格 §五）：0-15 CRISIS / 16-35 L2 / 36-45 L1 / 46-65 L0 / 66-85 P1 / 86-100 P2
LEVEL_CRISIS_MAX: Final[int] = 15
LEVEL_L2_MAX: Final[int] = 35
LEVEL_L1_MAX: Final[int] = 45
LEVEL_L0_MAX: Final[int] = 65
LEVEL_P1_MAX: Final[int] = 85
LEVEL_L0: Final[str] = "L0"
LEVEL_L1: Final[str] = "L1"
LEVEL_L2: Final[str] = "L2"
LEVEL_P1: Final[str] = "P1"
LEVEL_P2: Final[str] = "P2"
LEVEL_CRISIS: Final[str] = "CRISIS"

#: Track A 置信度下限。低于它且没有任何命中词时，不臆测情绪，直接交给 LLM 判断。
#: 可用环境变量 ``TRACK_A_MIN_CONFIDENCE`` 覆盖（0~1）。
TRACK_A_MIN_CONFIDENCE: Final[float] = 0.45

#: Bot 自身情绪（全局单行）：阻尼比用户情绪小一半，变化更慢
BOT_MOOD_DAMPING: Final[float] = 0.08
BOT_MOOD_MAX_DELTA: Final[float] = 3.0

#: 性格调制增益（模块三）
NEUROTIC_GAIN: Final[float] = 0.5      # 负面情绪放大系数
EXTRAVERT_GAIN: Final[float] = 0.3     # 正向情绪放大系数

MOD_TRACK_B: Final[str] = "MOD_TRACK_B"

INGRESS_DEDUP_TTL: Final[float] = 3.0      # 入口幽灵消息过滤窗口（NapCat 重推通常 1-2s 内）
STATE_LOCK_TTL: Final[float] = 10.0        # 锁 key 的自动过期（进程崩溃也不死锁）
STATE_LOCK_TIMEOUT: Final[float] = 2.0     # 等锁超时（超时则返回当前状态）
STATE_LOCK_POLL: Final[float] = 0.01       # 等锁轮询间隔

TRACK_B_CACHE_TTL: Final[int] = 3600       # Track B 结果缓存 1 小时
TRACK_B_FALLBACK: Final[int] = 50          # Track B 熔断兜底值

#: P0-4 防记仇白名单：Track B 判负面时必须给出的「心疼类」理由关键词。
#:
#: 为什么需要它：LLM 把「用户骂了我」当成扣分依据时，Bot 会真的记仇——
#: 这类判定不是共情，而是被冒犯。只有出于心疼 / 担心 / 自己难过 / 自责 /
#: 抱歉而扣分，才是「因为在意你而低落」。命中不了白名单的负面判定一律驳回。
REASON_WHITELIST: Final[tuple[str, ...]] = (
    "心疼",
    "担心",
    "自己难过",
    "自责",
    "抱歉",
)

#: Track B 判定的结构化结果：``(score, reason, confidence)``。
#:
#: ``reason`` 为 ``None`` 表示 LLM **没有返回 reason 字段**（模型漂移 / 测试替身），
#: 与「返回了空字符串」一样无法通过白名单审计——两者都判为「没有可审计的理由」，
#: 按记仇处理、驳回回中性分 50（F-22 · fail-closed）。只有明确给出白名单里的
#: 理由，扣分才被放行。
TrackBVerdict = tuple[int, Optional[str], float]

#: F-20 · P1-1 否定前缀表：白名单词前紧邻这些字时视为反话，不命中白名单。
_NEGATION_PREFIXES: Final[tuple[str, ...]] = ("不", "没", "别", "无", "非")

#: F-20 · P1-1 否定窗口的断句符：否定词与白名单词之间夹了标点/空白就不再是
#: 修饰关系——「怪自己没用，自责」里的「没」隔着「用，」，不能算否定「自责」。
_NEGATION_BREAKERS: Final[str] = (
    "，。、；：！？,.;:!?（）()「」『』《》【】“”\"'’‘…—-～· \t\r\n"
)


#: F-20 · P1-1 否定窗口的转折连词：转折后是另一个分句，前半句的否定词不得跨
#: 转折修饰后半句——「不抱歉但心疼」里的「不」修饰的是「抱歉」，不是「心疼」。
_NEGATION_TURNS: Final[tuple[str, ...]] = ("但", "可是", "不过", "然而")

#: F-20 · P1-1 否定检测窗口长度（字符）：白名单词前 5 个字符内出现否定词即视为
#: 被修饰——覆盖「没什么好担心的」这类隔 3~4 字的中隔反话（3 字窗口时代会漏放行）。
_NEGATION_WINDOW: Final[int] = 5


def _is_negated(reason: str, word: str) -> bool:
    """word 在 reason 中的所有出现是否都被否定前缀修饰（F-20 · P1-1）。

    判定口径：word 前 5 个字符窗口内出现否定词即视为被修饰——覆盖「不会抱歉」
    「没什么好担心的」这类否定词与白名单词之间隔几个字的反话。

    **标点断句**（F-20 实施期修正）：窗口内若夹标点/空白，只取最后一个断句符
    之后的部分参与判定，否定词不得跨标点生效。否则「怪自己没用，自责」里
    隔着一个「用，」的「没」会把正话「自责」误判成反话（误伤既有白名单用例）。

    **转折断句**（F-20 实施期修正）：窗口内若夹转折连词（``_NEGATION_TURNS``：
    但 / 可是 / 不过 / 然而），只取最后一个转折连词之后的部分参与判定。否则
    「不抱歉但心疼」里的「不」会跨转折修饰「心疼」，把正话误判成反话。

    只要存在一处未被修饰的出现就返回 False（视为正话命中）；词不存在时返回
    True，但调用方已用 ``word in reason`` 守门，不会单独依赖此返回值。

    与 :meth:`EmotionEngine._diagnose_negation`（仅 Track A 诊断记录用，
    F-23 已改名区分）刻意分家：诊断口径不改，本处闸门口径收紧，二者语义不同。
    F-23 裁定方案 R——**保持分家、改名区分、勿合并**：合并会把闸门退回 3 字窗口，
    等于撤销 F-20 修复。
    """
    idx = reason.find(word)
    while idx != -1:
        window = reason[max(0, idx - _NEGATION_WINDOW):idx]
        cut = max(window.rfind(ch) for ch in _NEGATION_BREAKERS)
        for turn in _NEGATION_TURNS:
            turn_pos = window.rfind(turn)
            if turn_pos != -1:
                cut = max(cut, turn_pos + len(turn) - 1)
        segment = window[cut + 1:]
        if not any(neg in segment for neg in _NEGATION_PREFIXES):
            return False
        idx = reason.find(word, idx + 1)
    return True

#: 批次 ID 的进程内自增序号（保证同一毫秒内多次冲刷也不撞号）
_BATCH_SEQ: Final = itertools.count(1)

#: Track B 的 system prompt：强制 JSON Mode，三个字段缺一不可
TRACK_B_SYSTEM_PROMPT: Final[str] = (
    "你是情绪判定模块，只输出严格 JSON，不要任何解释文字。\n"
    '格式：{"emotion_target": 0 到 100 的整数, '
    '"confidence": 0 到 1 的小数, "reason": "一句话中文理由"}\n'
    "含义：0=极度负面/崩溃边缘，50=平静，100=非常愉悦。\n"
    "用户的话里出现了转折词（但是/其实/不过/可是/然而），"
    "往往意味着口是心非或情绪反转，请按他真实的情绪给分，而不是按字面。\n"
    "如果这句话本身没有明确情绪（闲聊、陈述事实、问问题），请给 50 附近的分，"
    "不要因为语气礼貌就判为开心。\n"
    "若提供了「当前关系状态」，请结合它判断：同样的语气，对不同好感度的人"
    "应有不同解读（对亲近的人可以是撒娇，对疏离的人可能是客套）。\n"
    "reason 必须是一句简短中文，说明你为什么给这个分。\n"
    "重要：如果你要给出低于 50 的负面分，reason 里必须写清楚你是"
    "「因为心疼对方 / 担心对方 / 自己难过 / 自责 / 抱歉」才低落；"
    "绝不能因为觉得用户冒犯了你、骂了你、不尊重你而扣分——"
    "那种理由会被系统驳回，并强制回到 50。\n"
    "confidence 表示你对这个判断的把握程度，0 完全没把握，1 非常确定。"
)

#: 回复模板（按等级）
TONE_REPLIES: Final[dict[str, tuple[str, ...]]] = {
    LEVEL_L0: ("呜——本鲸知道了。", "嗯哼，今天也好好在听。", "嘿嘿，你在，我就高兴。"),
    LEVEL_L1: ("……嗯。你说的我都听见了。", "我在听，你慢慢说。", "好吧，那就这样。"),
    LEVEL_L2: ("嗯，我在。", "……我知道了。", "先让我安静一会儿。"),
    LEVEL_CRISIS: ("……嗯，我在听。", "别急，慢慢说。", "我在这儿，不催你。"),
}


# ---------------------------------------------------------------- 工具函数

def clamp(value: float, low: float, high: float) -> float:
    """把 value 夹在 [low, high]。"""
    if value < low:
        return low
    if value > high:
        return high
    return value


def to_score(value: Any, default: int = NEUTRAL_TARGET) -> int:
    """把任意输入安全地转成 0-100 的整数分。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool) or not math.isfinite(number):
        return default
    return int(round(clamp(number, SCORE_MIN, SCORE_MAX)))


def level_of(score: int) -> str:
    """平滑后的情绪分 -> 稳定等级（规格 §五）。

    ``<=15`` CRISIS / ``16..35`` L2 / ``36..45`` L1 / ``46..65`` L0 /
    ``66..85`` P1（积极） / ``>=86`` P2（狂热）。

    正面段用 P1/P2 而不是复用 L1/L2：同名两义会让调用方无法判断方向。
    """
    if score <= LEVEL_CRISIS_MAX:
        return LEVEL_CRISIS
    if score <= LEVEL_L2_MAX:
        return LEVEL_L2
    if score <= LEVEL_L1_MAX:
        return LEVEL_L1
    if score <= LEVEL_L0_MAX:
        return LEVEL_L0
    if score <= LEVEL_P1_MAX:
        return LEVEL_P1
    return LEVEL_P2


#: 六段「语气带」：给内心 OS / 前端分带用的稳定标识（与 level 一一对应，但语义更直白）
TONE_BANDS: Final[tuple[tuple[int, str], ...]] = (
    (15, "crisis"),
    (35, "deep_low"),
    (45, "low"),
    (65, "stable"),
    (85, "up"),
    (100, "high"),
)


def tone_band_of(score: int) -> str:
    """把分数映射成六段语气带（用于展示与 OS 选型）。"""
    value = clamp(score, SCORE_MIN, SCORE_MAX)
    for upper, name in TONE_BANDS:
        if value <= upper:
            return name
    return "high"


def bands_meta() -> list[dict[str, Any]]:
    """六段情绪带的机器可读元数据（**前端与文档的唯一真源**）。

    为什么由后端算而不是让前端抄一份：之前 README 写四档、后端实现六档、
    前端又硬编码第三套阈值，用户按文档调到 60 却拿到 L0，纯属自找 issue。

    这里不写死边界，而是**从 ``TONE_BANDS`` 推导**：上界 +1 得到下一段的下界，
    等级用 ``level_of(上界)`` 取，因此这份元数据与 ``level_of`` 在构造上就
    不可能分叉（``tests/test_time_and_bands.py`` 另有全量断言兜底）。

    Returns:
        长度 6 的列表，每项 ``{"band", "label", "min", "max"}``，
        ``band`` 是语气带键（crisis/deep_low/low/stable/up/high），
        ``label`` 是等级（CRISIS/L2/L1/L0/P1/P2）。
        **色值不在这里**：颜色属于设计系统，由前端按 band 键映射。
    """
    result: list[dict[str, Any]] = []
    low = SCORE_MIN
    for upper, tone in TONE_BANDS:
        result.append(
            {
                "band": tone,
                "label": level_of(upper),
                "min": low,
                "max": upper,
            }
        )
        low = upper + 1
    return result


def affinity_tier(affinity: int) -> str:
    """好感度分档（与 prompt_builder 的分层边界保持一致）。"""
    if affinity < 30:
        return "疏离"
    if affinity < 60:
        return "普通"
    if affinity < 90:
        return "亲近"
    return "极亲近"


def new_batch_id() -> str:
    """生成一个单调唯一的批次 ID（毫秒时间戳 + 进程内自增序号）。

    批次 ID 是「一次交互」的身份，由**防抖队列冲刷时**分配：
    一次 flush 里合并进来的 N 条消息共享同一个 batch_id。
    """
    stamp = int(time.time() * 1000)
    seq = next(_BATCH_SEQ)
    return "{0}-{1}".format(stamp, seq)


def make_event_key(user_id: str, merged_text: str, batch_id: str) -> str:
    """为「一次逻辑交互」生成幂等键：``md5(user_id + 合并文本 + 批次 ID)``。

    为什么要带 batch_id 而不是纯文本哈希
    ------------------------------------
    只哈希 ``user_id + 文本`` 会把「用户 3 秒内真心发了两次『嗯嗯』」当成同一次，
    第二次直接被吞掉。带上批次 ID 后：

    * **场景 A（网络重试）**：同一条消息在防抖窗口内被重推两次，队列合并成
      **一个** batch，天然只有一个 event_key → 引擎去重生效，只扣一次分。
    * **场景 B（用户连发意图）**：两次发送跨过防抖窗口 → 冲刷两次 → 两个
      不同的 batch_id → 两个 event_key → 各算各的，绝不吞消息。

    也就是说：**去重靠批次身份，不靠文本内容**。文本内容只在入口做
    「幽灵消息过滤」（见 :class:`IngressDeduper`）。

    Args:
        user_id: 用户 ID。
        merged_text: 防抖合并后的完整文本（必须是合并后的，不能是单条）。
        batch_id: 批次 ID（:func:`new_batch_id` 或队列分配）。

    Returns:
        32 位 md5 十六进制字符串。
    """
    raw = "{0}:{1}:{2}".format(user_id or "", merged_text or "", batch_id or "")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _digest(*parts: Any) -> str:
    """稳定摘要（用于缓存 / 去重 key）。"""
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pick(seed: str, items: tuple[str, ...]) -> str:
    """按种子稳定地从候选里挑一条（同种子同结果）。"""
    if not items:
        return ""
    index = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(items)
    return items[index]


# ---------------------------------------------------------------- Track A

class TrackA:
    """规则轨：本地词库 + 正则，输出契约见模块 docstring。"""

    def evaluate(self, message: str) -> dict[str, Any]:
        """对一条消息做本地快判。

        Args:
            message: 用户消息（允许 None / 非字符串，内部容错）。

        Returns:
            命中安全词：``{"risk": True, ...诊断}``；
            命中转折词：``{"semantic_conflict": True, ...诊断}``；
            否则：``{"target": 30|80, ...诊断}``。
        """
        text = "" if message is None else str(message)

        pos_hits = [word for word in POSITIVE_WORDS if word in text]
        neg_hits = [word for word in NEGATIVE_WORDS if word in text]
        ext_pos_hits = [word for word in EXTREME_POSITIVE_WORDS if word in text]
        ext_neg_hits = [word for word in EXTREME_NEGATIVE_WORDS if word in text]
        conflict_hits = [word for word in CONFLICT_WORDS if word in text]
        risk_hits = [word for word in RISK_WORDS if word in text]

        # 正则兜底：词库没收录的变体
        for pattern in RISK_PATTERNS:
            match = pattern.search(text)
            if match:
                risk_hits.append(match.group(0))
        if not conflict_hits:
            conflict_match = CONFLICT_PATTERN.search(text)
            if conflict_match:
                conflict_hits.append(conflict_match.group(0))

        negated = [
            word
            for word in (pos_hits + neg_hits)
            if self._diagnose_negation(text, word)
        ]
        signals = len(pos_hits) + len(neg_hits) + len(conflict_hits) + len(risk_hits)
        confidence = clamp(0.30 + 0.18 * signals, 0.0, 0.95)
        if risk_hits:
            confidence = max(confidence, 0.9)

        # 意图分流：noise（6 / 123 / ？）不参与情绪演绎，直接中性
        intent_type = self._intent_of(
            text, bool(pos_hits or neg_hits or ext_pos_hits or ext_neg_hits)
        )

        # 判定顺序：risk 优先，其次 semantic_conflict
        if risk_hits:
            return {
                "risk": True,
                "hits": self._unique(risk_hits),
                "positive_hits": pos_hits,
                "negative_hits": neg_hits,
                "confidence": round(confidence, 2),
                "intent_type": intent_type,
                "reason": "安全敏感词: " + ", ".join(self._unique(risk_hits)),
            }

        if conflict_hits:
            return {
                "semantic_conflict": True,
                "hits": self._unique(conflict_hits),
                "positive_hits": pos_hits,
                "negative_hits": neg_hits,
                "confidence": round(confidence, 2),
                "intent_type": intent_type,
                "reason": "转折词: " + ", ".join(self._unique(conflict_hits)),
            }

        # 极端词与普通词取并集计数（同一句话里「太棒」既在极端表也在普通表，不能算两次）
        neg_all = self._unique(neg_hits + ext_neg_hits)
        pos_all = self._unique(pos_hits + ext_pos_hits)
        hits_all = self._unique(pos_all + neg_all)

        if not hits_all:
            # 零命中 ≠ 正面。以前这里会落到 POSITIVE_TARGET，导致「没说什么」也被当成开心，
            # 情绪只涨不跌。现在保持中性并标记 undefined，由上层决定是否回落 LLM。
            return {
                "target": NEUTRAL_TARGET,
                "undefined": True,
                "hits": [],
                "positive_hits": [],
                "negative_hits": [],
                "negated": negated,
                "confidence": round(confidence, 2),
                "intent_type": intent_type,
                "reason": "无明显信号",
            }

        if len(neg_all) == len(pos_all):
            # 正负打平：同样不臆测方向
            return {
                "target": NEUTRAL_TARGET,
                "undefined": True,
                "hits": hits_all,
                "positive_hits": pos_all,
                "negative_hits": neg_all,
                "negated": negated,
                "confidence": round(confidence, 2),
                "intent_type": intent_type,
                "reason": "正负信号打平",
            }

        # 五档打分（规格 §三）：极端词把目标拉到 20 / 90，普通词 35 / 75
        if len(neg_all) > len(pos_all):
            target = EXTREME_NEGATIVE_TARGET if ext_neg_hits else NEGATIVE_TARGET
        else:
            target = EXTREME_POSITIVE_TARGET if ext_pos_hits else POSITIVE_TARGET

        return {
            "target": target,
            "undefined": False,
            "hits": hits_all,
            "positive_hits": pos_all,
            "negative_hits": neg_all,
            "extreme": bool(ext_neg_hits or ext_pos_hits),
            "negated": negated,
            "confidence": round(confidence, 2),
            "intent_type": intent_type,
            "reason": "命中: " + ", ".join(hits_all),
        }

    #: 兼容阶段一的命名
    def __call__(self, message: str) -> dict[str, Any]:
        return self.evaluate(message)

    @staticmethod
    def _intent_of(text: str, has_emotion_hits: bool) -> str:
        """意图分流：noise / emotion / unknown。

        ``noise`` 的判定不靠模型，纯规则：
        - 命中情感词 -> 一定是 ``emotion``（哪怕很短，比如「累」）；
        - 空串、长度 < 3、纯数字、纯符号/emoji、单字符重复 -> ``noise``；
        - 其余未命中词典的普通文本 -> ``unknown``。

        ``unknown`` 会走「未定义 → 回落 LLM」；``noise`` 直接不更新情绪，
        避免「6」「123」被当成一句话去演绎。
        """
        raw = "" if text is None else str(text)
        stripped = raw.strip()
        if has_emotion_hits:
            return "emotion"
        if not stripped:
            return "noise"
        if len(stripped) < 3:
            return "noise"
        if re.fullmatch(r"[\d\s]+", stripped):
            return "noise"
        if re.fullmatch(r"[\W_]+", stripped, re.UNICODE):
            return "noise"
        if len(set(stripped)) == 1:
            return "noise"
        return "unknown"

    @staticmethod
    def _unique(items: list[str]) -> list[str]:
        """去重且保持顺序。"""
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @staticmethod
    def _diagnose_negation(text: str, word: str) -> bool:
        """判断 word 前 3 个字符内是否出现否定词（**仅 Track A 诊断用**）。

        F-23：本方法与模块级 :func:`_is_negated` **同名不同义，已改名区分**。
        两者口径差异（本方法刻意保持原样，勿合并）：

        ========== =================================== ==============================
        维度       本方法（诊断口径）                   模块级 _is_negated（闸门口径）
        ========== =================================== ==============================
        窗口        3 字                                 5 字（_NEGATION_WINDOW）
        标点断句    无                                   有（_NEGATION_BREAKERS）
        转折断句    无                                   有（_NEGATION_TURNS）
        多出现      只看第一处 find                      遍历全部，任一处正话即放行
        词不存在    返回 False                           返回 True
        用途        Track A 诊断（pos_hits+neg_hits）   P0-4 防记仇闸门
        ========== =================================== ==============================

        闸门口径服务于 P0-4 防记仇（:func:`_apply_reason_whitelist`）；
        本方法只服务 Track A 的 ``pos_hits + neg_hits`` 诊断过滤。
        合并两者会把闸门退回 3 字窗口，等于撤销 F-20 修复。
        """
        index = text.find(word)
        if index <= 0:
            return False
        window = text[max(0, index - 3):index]
        return any(neg in window for neg in NEGATION_WORDS)


# ---------------------------------------------------------------- Track B

class TrackB:
    """LLM 轨：只在语义冲突时调用，结果进 diskcache，异常一律兜底 50。

    Args:
        cache: diskcache 实例（用于结果缓存与 token 账本解耦）。
        base_url: OpenAI 兼容 base url，如 ``https://api.deepseek.com/v1``。
        api_key: API Key；空或 ``sk-xxx`` 占位视为未配置。
        model: 模型名。
        timeout: 单次请求超时（秒）。
        session_factory: 可选的 SQLAlchemy sessionmaker，用于写 TokenLog。
        monitor: 可选的 :class:`monitor.TokenMonitor`；提供时优先用它记账。
    """

    def __init__(
        self,
        cache: Cache,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float = 8.0,
        session_factory: Optional[Any] = None,
        monitor: Optional[Any] = None,
    ) -> None:
        self.cache = cache
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout = float(timeout) if timeout else 8.0
        self.session_factory = session_factory
        self.monitor = monitor

    @property
    def enabled(self) -> bool:
        """是否具备可用配置（未配置 Key 时直接走兜底，不发请求）。"""
        if not self.base_url or not self.model or not self.api_key:
            return False
        lowered = self.api_key.lower()
        if lowered.startswith("sk-xxx") or "xxxxxx" in lowered:
            return False
        return True

    async def evaluate(
        self, message: str, context: Optional[list[Any]] = None, user_id: str = "",
        relationship: Optional[dict[str, Any]] = None,
    ) -> int:
        """调用 LLM 判定情绪目标分（**永不抛异常**）。

        任何异常（网络、超时、4xx/5xx、坏 Key、非 JSON、字段缺失、越界、
        甚至是内部实现 bug）都统一返回 50，绝不把异常抛给调用方。

        Args:
            message: 用户消息。
            context: 上下文列表（记忆 / 历史轮次），元素可为 dict 或 str。
            user_id: 仅用于 token 记账。

        Returns:
            0-100 的整数；任何异常 / 未配置 / 解析失败都返回 50。
        """
        verdict = await self.evaluate_verdict(
            message, context, user_id=user_id, relationship=relationship
        )
        return verdict[0]

    async def evaluate_verdict(
        self, message: str, context: Optional[list[Any]] = None, user_id: str = "",
        relationship: Optional[dict[str, Any]] = None,
    ) -> TrackBVerdict:
        """同 :meth:`evaluate`，但额外返回 LLM 给出的 ``reason`` 与 ``confidence``。

        为什么需要它：P0-4 防记仇要在**引擎层**判断「这次扣分到底是共情还是被
        冒犯」，判断依据是 LLM 自己写的理由，因此理由必须能带回调用方。分数语义
        与 :meth:`evaluate` 完全一致（同样永不抛异常、同样兜底 50）。

        Returns:
            ``(score, reason, confidence)``；异常 / 未配置 / 解析失败时
            ``reason`` 为 ``None``、``confidence`` 为 0.0。
        """
        try:
            override = self._evaluate_override()
            if override is not None:
                # 兼容旧接缝：``evaluate`` 是 TrackB 的公开入口，外部（子类、测试
                # 替身、运维热补丁）覆盖它就是在接管判分。此时分数归覆盖者所有，
                # 且拿不到结构化理由——``reason`` 记 ``None``（无可审计理由，
                # F-22 之后按 fail-closed 驳回），而不是绕过覆盖者去跑内建链路。
                score = await override(
                    message, context, user_id=user_id, relationship=relationship
                )
                return (int(score), None, 0.0)
            return await self._evaluate_inner(message, context, user_id, relationship)
        except Exception:  # 最后一道防线：Track B 永不让主流程挂掉
            return (TRACK_B_FALLBACK, None, 0.0)

    def _evaluate_override(self) -> Optional[Any]:
        """外部覆盖了 ``evaluate`` 就返回它，否则返回 ``None``。

        两处都要查：实例属性（测试替身 ``setattr(obj, "evaluate", fn)``）与
        类属性（子类重写）。只查实例属性会漏掉子类，那样子类的判分会被内建
        链路顶掉——这是比测试失败更严重的静默错误。
        """
        own = vars(self).get("evaluate")
        if own is not None:
            return own
        class_level = type(self).__dict__.get("evaluate")
        if class_level is not None and class_level is not TrackB.evaluate:
            # 类属性拿到的是普通函数，需要自己绑定 self
            return class_level.__get__(self, type(self))
        return None

    async def _evaluate_inner(
        self, message: str, context: Optional[list[Any]], user_id: str,
        relationship: Optional[dict[str, Any]] = None,
    ) -> TrackBVerdict:
        """Track B 的实际判定流程（异常由 ``evaluate`` 统一兜底）。"""
        if not self.enabled:
            return (TRACK_B_FALLBACK, None, 0.0)

        text = "" if message is None else str(message)
        context_text = self._format_context(context)
        relation_text = self._format_relationship(relationship)
        # 关系状态会影响判定，必须进缓存键，否则换了人却命中同一份结论
        cache_key = "tb:" + _digest(self.model, text, context_text, relation_text)

        cached = self.cache.get(cache_key)
        if isinstance(cached, int) and not isinstance(cached, bool):
            # 旧版缓存只存分数：reason 未知按 None 处理
            return (int(clamp(cached, SCORE_MIN, SCORE_MAX)), None, 0.0)
        if isinstance(cached, dict) and "score" in cached:
            try:
                cached_score = int(clamp(float(cached.get("score")), SCORE_MIN, SCORE_MAX))
            except (TypeError, ValueError):
                cached_score = TRACK_B_FALLBACK
            raw_reason = cached.get("reason")
            cached_reason: Optional[str] = None if raw_reason is None else str(raw_reason)
            try:
                cached_conf = clamp(float(cached.get("confidence") or 0.0), 0.0, 1.0)
            except (TypeError, ValueError):
                cached_conf = 0.0
            return (cached_score, cached_reason, cached_conf)

        user_prompt = (
            "用户刚才说：" + text + "\n"
            + ("最近的对话上下文：\n" + context_text + "\n" if context_text else "")
            + (relation_text + "\n" if relation_text else "")
            + "请给出他此刻真实的情绪分数 emotion_target。"
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": TRACK_B_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }

        data = await self._request(body, headers)
        if data is None:
            return (TRACK_B_FALLBACK, None, 0.0)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return (TRACK_B_FALLBACK, None, 0.0)

        parsed = self._parse_json(content)
        if parsed is None or "emotion_target" not in parsed:
            return (TRACK_B_FALLBACK, None, 0.0)

        raw_value = parsed.get("emotion_target")
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            return (TRACK_B_FALLBACK, None, 0.0)
        if isinstance(raw_value, bool) or not math.isfinite(number):
            return (TRACK_B_FALLBACK, None, 0.0)

        score = int(round(clamp(number, SCORE_MIN, SCORE_MAX)))
        # reason：缺失 -> None（无法判记仇，闸门按 fail-closed 驳回）；有值 -> 原文
        raw_reason = parsed.get("reason")
        reason: Optional[str] = None if raw_reason is None else str(raw_reason)
        # confidence：缺失 / 非数值 -> 0.0，越界 clamp 到 [0, 1]
        raw_conf = parsed.get("confidence")
        try:
            confidence = clamp(float(raw_conf), 0.0, 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if isinstance(raw_conf, bool) or not math.isfinite(confidence):
            confidence = 0.0

        self._remember(cache_key, score, reason, confidence)
        self._log_tokens(user_id, data.get("usage") or {})
        return (score, reason, confidence)

    # ---------------------------------------------------------- 内部

    async def _request(
        self, body: dict[str, Any], headers: dict[str, str]
    ) -> Optional[dict[str, Any]]:
        """发一次 OpenAI 兼容请求，任何异常都返回 None（熔断点）。

        单独抽成方法便于测试替身（monkeypatch ``TrackB._request``）注入，
        也保证网络异常不会逃逸到调用方。
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.base_url + "/chat/completions", json=body, headers=headers
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except Exception:  # 网络 / 超时 / 4xx / 5xx / 坏 Key / 非 JSON：全部兜底
            return None

    @staticmethod
    def _format_context(context: Optional[list[Any]]) -> str:
        """把上下文列表压成一段文本（最多 8 条）。"""
        if not context:
            return ""
        lines: list[str] = []
        for item in list(context)[-8:]:
            if isinstance(item, dict):
                role = str(item.get("role", "user"))
                content = str(item.get("content", ""))
                lines.append(role + ": " + content)
            else:
                lines.append(str(item))
        return "\n".join(line for line in lines if line.strip())

    @staticmethod
    def _format_relationship(relationship: Optional[dict[str, Any]]) -> str:
        """把好感度 / 当前情绪 / 等级压成一句关系描述，供 LLM 参考。"""
        if not isinstance(relationship, dict):
            return ""
        try:
            affinity = int(relationship.get("affinity", DEFAULT_AFFINITY))
        except (TypeError, ValueError):
            affinity = DEFAULT_AFFINITY
        try:
            emotion = int(relationship.get("emotion", NEUTRAL_TARGET))
        except (TypeError, ValueError):
            emotion = NEUTRAL_TARGET
        level = str(relationship.get("level") or "") or level_of(emotion)
        return (
            "当前关系状态：好感度 {0}/100（{1}），此刻情绪 {2}/100（{3}）。".format(
                clamp(affinity, 0, 100), affinity_tier(affinity), clamp(emotion, 0, 100), level
            )
        )

    def _remember(
        self,
        cache_key: str,
        score: int,
        reason: Optional[str] = None,
        confidence: float = 0.0,
    ) -> None:
        """写缓存（失败不影响返回）。

        存 dict 而非裸分数：P0-4 的记仇判定要读回 reason，缓存命中时若丢掉理由，
        同一句话第二次出现就会退化成「无理由」（None）——F-22 之后按 fail-closed
        驳回，记仇扣分不会复活。
        """
        try:
            self.cache.set(
                cache_key,
                {"score": int(score), "reason": reason, "confidence": float(confidence)},
                expire=TRACK_B_CACHE_TTL,
            )
        except Exception:
            pass

    def _log_tokens(self, user_id: str, usage: Any) -> None:
        """写 token 账本（失败不影响返回）。"""
        if not isinstance(usage, dict):
            return
        try:
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        except (TypeError, ValueError):
            return
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return

        if self.monitor is not None:
            self.monitor.record(MOD_TRACK_B, (prompt_tokens, completion_tokens), user_id or "")
            return

        # 没有 monitor 时的兜底：直接写库
        if self.session_factory is None:
            return
        try:
            with self.session_factory() as session:
                session.add(
                    TokenLog(
                        user_id=user_id or "",
                        module=MOD_TRACK_B,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        created_at=utcnow(),
                    )
                )
                session.commit()
        except Exception:  # 写库失败绝不影响判定结果
            pass

    @staticmethod
    def _parse_json(text: Any) -> Optional[dict[str, Any]]:
        """解析模型输出，容忍 ```json 包裹与多余文字。"""
        if not isinstance(text, str):
            return None
        cleaned = text.strip()
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            value = json.loads(cleaned)
            return value if isinstance(value, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
        match = re.search(r"\{[^{}]*\}", cleaned, re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None


# ---------------------------------------------------------------- 防抖

@dataclass(frozen=True)
class Batch:
    """一次防抖冲刷的产物：合并文本 + 批次 ID。

    Attributes:
        text: 窗口内所有消息按行合并后的文本。
        batch_id: 本次冲刷的唯一标识，用来生成 event_key。
    """

    text: str
    batch_id: str

    def event_key(self, user_id: str) -> str:
        """按批次生成幂等键（同一批次内所有等待者拿到同一个 key）。"""
        return make_event_key(user_id, self.text, self.batch_id)


class Debouncer:
    """把同一用户在一个窗口内的连续消息合并成一个 :class:`Batch`。"""

    def __init__(self, window: float = 1.2) -> None:
        self.window = float(window) if window else 0.0
        self._buffers: dict[str, list[str]] = {}
        self._waiters: dict[str, list[asyncio.Future[Batch]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def merge(self, key: str, message: str) -> Batch:
        """等待窗口结束，返回合并后的批次（文本 + 批次 ID）。

        同一窗口内到达的多条消息共享**同一个 batch_id**，因此也共享同一个
        event_key —— 这正是「网络重推的幽灵消息只扣一次分」的实现基础。
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Batch] = loop.create_future()
        self._buffers.setdefault(key, []).append(message)
        self._waiters.setdefault(key, []).append(future)

        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = asyncio.create_task(self._flush_later(key))
        return await future

    async def _flush_later(self, key: str) -> None:
        await asyncio.sleep(self.window)
        texts = self._buffers.pop(key, [])
        waiters = self._waiters.pop(key, [])
        self._tasks.pop(key, None)
        # 冲刷时分配批次 ID：一次 flush = 一次逻辑交互
        batch = Batch(text="\n".join(texts), batch_id=new_batch_id())
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(batch)


class IngressDeduper:
    """入口「幽灵消息」过滤器：吃掉 NapCat 重复推送的同一条消息。

    判定依据按可靠性排序：

    1. 平台 ``message_id``（最准）：同一个 id 直接丢弃；
    2. 退化为 ``(user_id, 文本)`` 的短窗口（默认 3s）：窗口内重复即丢弃。

    注意它做的是**丢弃**，不是合并 —— 丢弃意味着不会新建批次、不会重复计分；
    而合并会让「用户真心连发两次同一句」被并成一次。两者语义不同，别混用。

    Args:
        cache: diskcache 实例（与引擎共用，便于清理）。
        ttl: 文本退化判定的窗口秒数。
    """

    def __init__(self, cache: Cache, ttl: float = INGRESS_DEDUP_TTL) -> None:
        self.cache = cache
        self.ttl = max(float(ttl), 0.1)

    def _key(
        self,
        user_id: str,
        text: str,
        message_id: Optional[str],
        chat_scope: str = DEFAULT_CHAT_SCOPE,
    ) -> str:
        if message_id:
            # 平台 message_id 本身全局唯一，不需要再加分区
            return "ingress:mid:" + str(message_id)
        # 文本退化为**按分区**判定：同一个人在 A 群和 B 群同时发「在吗」是两条
        # 不同的消息，不能因为落在同一个文本窗口里就被吞掉一条。
        return "ingress:txt:" + _digest(user_id, str(chat_scope), text)[:32]

    def is_duplicate(
        self,
        user_id: str,
        text: str,
        message_id: Optional[str] = None,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
    ) -> bool:
        """返回 True 表示这是重复推送，应当直接丢弃。"""
        key = self._key(user_id, text, message_id, chat_scope)
        try:
            if self.cache.get(key) is not None:
                return True
            self.cache.set(key, 1, expire=self.ttl)
        except Exception:
            return False
        return False


# ---------------------------------------------------------------- SQLite PRAGMA

def install_sqlite_pragmas(engine: Any) -> None:
    """给 SQLAlchemy 引擎挂上 SQLite PRAGMA（每个新连接都要重设一次）。

    为什么用事件而不是执行一条 SQL：``PRAGMA journal_mode`` 是**按数据库文件**
    生效的（写进文件头），但 ``busy_timeout`` / ``foreign_keys`` 是**按连接**生效的，
    连接池里每开一条新连接都得重新设。``connect`` 事件正好覆盖这两类。

    PRAGMA 必须在 ``create_all`` **之前**注册：监听器只对注册之后建立的连接生效，
    而 ``create_all`` 自己就会取一条连接。

    - ``journal_mode=WAL``：读写不再互相阻塞（旁路服务与看板同时访问同一个库）；
    - ``synchronous=NORMAL``：WAL 下的推荐值，掉电最多丢最后一个事务；
    - ``busy_timeout=5000``：写锁竞争时等 5 秒再报 "database is locked"；
    - ``foreign_keys=ON``：SQLite 默认关闭外键约束，显式打开以免以后加了外键静默失效。
    """

    @sa_event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


# ---------------------------------------------------------------- 引擎

class EmotionEngine:
    """情绪机主干：读状态 -> 双轨判定 -> 平滑更新 -> 生成内心 OS 与动作。

    Args:
        db_path: SQLite 文件路径（测试请传临时路径，勿污染 app.db）。
        cache_dir: diskcache 目录（缓存、跨进程锁、幂等记录都在这里）。
        env: 配置字典，默认取 ``os.environ``。
    """

    def __init__(
        self,
        db_path: str = DB_PATH,
        cache_dir: str = CACHE_DIR,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.env: dict[str, str] = dict(env if env is not None else os.environ)
        os.makedirs(cache_dir, exist_ok=True)
        # 锁 key 不能被淘汰，因此关闭缓存淘汰策略
        self.cache = Cache(cache_dir, eviction_policy="none")
        self.db_path = db_path

        self.db_engine = create_engine(
            "sqlite:///" + db_path,
            future=True,
            connect_args={"check_same_thread": False},
        )
        # PRAGMA 必须在 create_all 之前注册（监听器只对之后新建的连接生效）
        install_sqlite_pragmas(self.db_engine)
        Base.metadata.create_all(self.db_engine)
        # 兼容阶段一老库（token_logs 的 module_type -> module）
        self.migrations: list[str] = ensure_schema(self.db_engine)
        # chat_scope 分房：user_states 复合主键 + 好感度拆表（幂等；新库是空操作）
        self.migrations.extend(migrate_chat_scope(self.db_engine))
        self.Session = sessionmaker(
            bind=self.db_engine, expire_on_commit=False, future=True
        )

        self.track_a = TrackA()
        self.prompt_builder = PromptBuilder()
        # Token 监控先建：Track B 与演化系统共用同一本账
        self.monitor = TokenMonitor(self.Session)
        self.track_b = self._build_track_b()
        # Track A 置信度下限：低于它且无命中词时，情绪视为「未定义」，交回 LLM 判断
        self.track_a_min_confidence = clamp(
            self._env_float("TRACK_A_MIN_CONFIDENCE", TRACK_A_MIN_CONFIDENCE), 0.0, 1.0
        )
        # 习惯化滑动窗口：user_id -> deque[(时间戳, 极性)]，进程内内存（重启即清空）
        # 键是 (user_id, chat_scope) 元组：习惯化窗口按会话分区隔离
        self._habit: dict[tuple[str, str], Any] = {}
        self._habit_lock = threading.Lock()
        # F-09：TTL / 容量回收状态。_habit_last_prune 记录上次清扫时刻，
        # 用于把 O(n) 遍历摊薄到 HABIT_PRUNE_INTERVAL 一次。
        self._habit_ttl = self._env_float("HABIT_TTL_SECONDS", HABIT_TTL_SECONDS)
        self._habit_max = int(self._env_float("HABIT_CACHE_MAX", HABIT_CACHE_MAX))
        self._habit_prune_interval = self._env_float(
            "HABIT_PRUNE_INTERVAL", HABIT_PRUNE_INTERVAL
        )
        self._habit_last_prune = 0.0
        # 批次单飞：同一 event_key 的并发 Track B 评估共享一次 LLM 调用。
        # 防抖合并批内多个等待者会并发走 process()，它们共享同一 event_key，但各自的
        # 关系快照/记忆上下文可能已读到彼此刚落库的结果 → Track B 缓存键不同 → 缓存
        # 只能兜住串行、兜不住并发 → 一次合并打多次 LLM（token 双倍）。
        self._track_b_inflight: dict[str, "asyncio.Future[TrackBVerdict]"] = {}
        # 性格向量：只读本地缓存，不触网（见 persona_traits.py）
        self.traits: PersonaTraits = load_traits()
        # 潜意识审核：规则拦截 → 连续刺激 → 概率解禁
        self.auditor = SubconsciousAuditor()
        # 防抖窗口：DEBOUNCE_WINDOW_MS（毫秒，新增，默认 500）优先于
        # 历史键 DEBOUNCE_SECONDS（秒）。Core 是防抖的**唯一权威**，Adapter 只传
        # debounce=True，不再自己合并。
        window_ms = self.env.get("DEBOUNCE_WINDOW_MS")
        if window_ms is not None and str(window_ms).strip():
            window_seconds = self._env_float("DEBOUNCE_WINDOW_MS", 500.0) / 1000.0
        else:
            window_seconds = self._env_float("DEBOUNCE_SECONDS", 1.2)
        self.debouncer = Debouncer(window_seconds)
        self.whitelist = self._parse_whitelist()
        # H-A1：特权用户（判定前放行）与危机豁免（现有 whitelist）是两件事，拆开
        self.privileged = self._parse_privileged()
        self.lock_timeout = self._env_float("STATE_LOCK_TIMEOUT", STATE_LOCK_TIMEOUT)
        # 事件归并窗口：同人同文在该秒数内算同一次事件（防 Adapter 重试重复扣分）
        # 入口幽灵消息过滤（NapCat 重推）：文本退化的短窗口
        self.ingress_deduper = IngressDeduper(
            self.cache, self._env_float("INGRESS_DEDUP_TTL", INGRESS_DEDUP_TTL)
        )

        # 记忆路由：宿主原生 > 外部插件 > 本地 SQLite 兜底（阶段五）
        from memory_router import MemoryRouter

        memory_db = self.env.get("MEMORY_DB_PATH") or os.path.join(CORE_DIR, "memory.db")
        self.memory = MemoryRouter.build(
            db_path=memory_db,
            external_base_url=self.env.get("MEMORY_EXTERNAL_URL", "") or "",
            external_api_key=self.env.get("MEMORY_EXTERNAL_KEY", "") or "",
            ttl_days=self._env_float("MEMORY_TTL_DAYS", 7.0),
        )

    # ---------------------------------------------------------- 配置

    def _env_float(self, key: str, default: float) -> float:
        """从 env 读浮点配置，非法值回退默认。"""
        raw = self.env.get(key)
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    def _parse_whitelist(self) -> set[str]:
        """解析绝对死线白名单（这些用户永不进入 CRISIS 压制）。

        即决策 #13 里的 ``crisis_exempt``：只豁免**危机压制**（CRISIS 降级为 L2），
        不豁免判定本身。与 ``PRIVILEGED_USERS``（特权放行）是两件事，别混用。
        """
        raw = self.env.get("ADMIN_WHITELIST", "") or ""
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _parse_privileged(self) -> set[str]:
        """H-A1：特权用户名单（判定前放行，不扣分/不演化/不计数/不写记忆）。

        决策 #13 拆分结果：与 ``ADMIN_WHITELIST``（危机豁免）语义不同——合并会导致
        「特权用户永不进入 CRISIS」，那是安全回退，因此拆成两个配置。
        """
        raw = self.env.get("PRIVILEGED_USERS", "") or ""
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _build_track_b(self) -> TrackB:
        """按当前 env 构造 Track B。"""
        return TrackB(
            cache=self.cache,
            base_url=self.env.get("LLM_API_BASE", ""),
            api_key=self.env.get("LLM_API_KEY", ""),
            model=self.env.get("LLM_MODEL", ""),
            timeout=self._env_float("LLM_TIMEOUT", 8.0),
            session_factory=self.Session,
            monitor=self.monitor,
        )

    def reload_config(self) -> None:
        """后台改完 .env 后热更新 Track B / 白名单 / 特权名单。"""
        self.track_b = self._build_track_b()
        self.whitelist = self._parse_whitelist()
        self.privileged = self._parse_privileged()

    # ---------------------------------------------------------- Track A / B 对外接口

    def evaluate_track_a(self, message: str) -> dict[str, Any]:
        """本地快判（契约见模块 docstring）。"""
        return self.track_a.evaluate(message)

    async def evaluate_track_b(
        self, message: str, context: Optional[list[Any]] = None, user_id: str = "",
        relationship: Optional[dict[str, Any]] = None,
    ) -> int:
        """LLM 复核（语义冲突或无明确信号时调用），永不抛异常。

        **契约（F-25）**：本方法**忠实透传**模型返回的分数，**不经过 P0-4
        防记仇闸门**（:func:`_apply_reason_whitelist`）。闸门是 ``process()``
        路径的业务策略，只在 :meth:`_evaluate_track_b_locked` 内生效
        （``engine.py:2099``，行号随编辑漂移，以 ``grep -n _apply_reason_whitelist``
        实测为准）。

        - 生产代码请走 :meth:`process`，闸门会自动生效。
        - 直接调用本方法的场景（测试替身、诊断工具、宿主集成）拿到的是
          **原始分数**，负面值不会被驳回。
        - 本契约**有意为之**：``tests/test_track_b.py`` 锁定「忠实透传」语义，
          例如 ``{"emotion_target": 12}`` 期望返回 12 而非 50。把闸门塞进本方法
          会破坏该契约（F-25 勘察实测：2 failed / 407 passed）。
        """
        verdict = await self.evaluate_track_b_verdict(
            message, context, user_id=user_id, relationship=relationship
        )
        return verdict[0]

    async def evaluate_track_b_verdict(
        self, message: str, context: Optional[list[Any]] = None, user_id: str = "",
        relationship: Optional[dict[str, Any]] = None,
    ) -> TrackBVerdict:
        """同 :meth:`evaluate_track_b`，但把 ``reason`` / ``confidence`` 一并带回。

        P0-4 防记仇的判定依据是 LLM 写的理由，引擎层必须拿到它。

        **契约（F-25）**：与 :meth:`evaluate_track_b` 一致——本方法返回**裸
        verdict**，**不经过 P0-4 闸门**。闸门在 :meth:`_evaluate_track_b_locked`
        内（``engine.py:2099``，行号随编辑漂移，以
        ``grep -n _apply_reason_whitelist`` 实测为准）对本方法的结果再过一道。

        ``reason`` 为 ``None`` 时表示「模型未按三字段协议返回」，闸门侧会
        fail-closed 驳回（F-22）。但**本方法本身不做该判定**——它只负责透传。

        - 生产代码请走 :meth:`process`，闸门会自动生效。
        - 直接调用本方法拿到的是**裸 verdict**，负面分数不会被驳回。
        """
        try:
            return await self.track_b.evaluate_verdict(
                message, context, user_id=user_id, relationship=relationship
            )
        except Exception:  # 双重保险：TrackB 内部已兜底
            return (TRACK_B_FALLBACK, None, 0.0)

    # ---------------------------------------------------------- 状态读写（同步，走 to_thread）

    def _get_or_create(
        self, user_id: str, chat_scope: str = DEFAULT_CHAT_SCOPE
    ) -> dict[str, Any]:
        """读状态；不存在则创建默认行。

        状态按 ``(user_id, chat_scope)`` **严格分房**（I-26：情绪分房）。
        好感度是**全局锚点**，单独从 ``user_affinity`` 读——注意
        :meth:`_read_affinity` 的签名里没有 ``chat_scope``，在签名层面杜绝分房。
        """
        scope = str(chat_scope)
        # 先读全局锚点：不要在同一条写事务里再开一条读连接（WAL 能读，但没必要）
        affinity = self._read_affinity(user_id)
        with self.Session() as session:
            row = session.get(UserState, (user_id, scope))
            if row is None:
                row = UserState(
                    user_id=user_id,
                    chat_scope=scope,
                    emotion_current=DEFAULT_EMOTION,
                    emotion_level=DEFAULT_LEVEL,
                    last_update=utcnow(),
                )
                session.add(row)
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    row = session.get(UserState, (user_id, scope))
                    if row is None:
                        return {
                            "user_id": user_id,
                            "chat_scope": scope,
                            "emotion": DEFAULT_EMOTION,
                            "level": DEFAULT_LEVEL,
                            "affinity": affinity,
                            "last_update": None,
                            "last_event_key": None,
                            "arrival_ts": 0,
                        }
            return {
                "user_id": user_id,
                "chat_scope": scope,
                "emotion": int(row.emotion_current if row.emotion_current is not None else DEFAULT_EMOTION),
                "level": row.emotion_level or DEFAULT_LEVEL,
                "affinity": affinity,
                "last_update": row.last_update,
                "last_event_key": getattr(row, "last_event_key", None),
                "arrival_ts": int(getattr(row, "arrival_ts", 0) or 0),
            }

    def _write_emotion(
        self,
        user_id: str,
        emotion: int,
        level: str,
        moment: Any = None,
        event_key: Optional[str] = None,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
        arrival_ts: Optional[int] = None,
    ) -> None:
        """写回**本会话分区**的情绪值、等级与 last_event_key。

        好感度不在这张表里（全局 ``user_affinity``），因此这里既不会写也不会覆盖它。

        ``arrival_ts``：P0-8 乱序防御的时间水位线。只有确实应用了本次请求才推进；
        ``None`` 表示调用方没带时间戳，保持库里原值不动（不要用 0 覆盖，否则
        下一次带时间戳的消息会失去比对基准）。
        """
        scope = str(chat_scope)
        with self.Session() as session:
            row = session.get(UserState, (user_id, scope))
            if row is None:
                row = UserState(
                    user_id=user_id,
                    chat_scope=scope,
                    emotion_current=emotion,
                    emotion_level=level,
                    last_update=moment or utcnow(),
                    last_event_key=event_key,
                    arrival_ts=int(arrival_ts or 0),
                )
                session.add(row)
            else:
                row.emotion_current = int(emotion)
                row.emotion_level = level
                row.last_update = moment or utcnow()
                if event_key:
                    row.last_event_key = str(event_key)
                if arrival_ts is not None:
                    row.arrival_ts = int(arrival_ts)
            session.commit()

    def bump_activity(self, user_id: str, day: Optional[str] = None) -> None:
        """累加当日消息数（演化系统的活跃度闸门依赖它）。失败静默。"""
        key = day or utcnow().date().isoformat()
        try:
            with self.Session() as session:
                row = session.get(MessageCount, (str(user_id), key))
                if row is None:
                    session.add(MessageCount(user_id=str(user_id), day=key, count=1))
                else:
                    row.count = int(row.count) + 1
                    row.updated_at = utcnow()
                session.commit()
        except Exception:  # 统计失败绝不能影响对话
            pass

    def _read_affinity(self, user_id: str) -> int:
        """读**全局**好感度（I-26：锚点全局）。

        签名里**没有** ``chat_scope`` 参数——这是刻意的：好感度不随会话分裂，
        传不进来就不可能被分房。
        """
        with self.Session() as session:
            row = session.get(UserAffinity, str(user_id))
            if row is None or row.affinity_raw is None:
                return DEFAULT_AFFINITY
            return int(row.affinity_raw)

    def _write_affinity(self, user_id: str, affinity: int) -> None:
        """只写**全局**好感度（同样不接受 ``chat_scope``）。"""
        with self.Session() as session:
            row = session.get(UserAffinity, str(user_id))
            if row is None:
                session.add(
                    UserAffinity(
                        user_id=str(user_id),
                        affinity_raw=int(affinity),
                        updated_at=utcnow(),
                    )
                )
            else:
                row.affinity_raw = int(affinity)
                row.updated_at = utcnow()
            session.commit()

    def list_states(self, limit: int = 100) -> list[dict[str, Any]]:
        """看板用：按最近更新时间倒序列出用户状态。"""
        safe_limit = int(limit) if isinstance(limit, int) and limit > 0 else 100
        with self.Session() as session:
            rows = (
                session.execute(
                    select(UserState)
                    .order_by(UserState.last_update.desc())
                    .limit(safe_limit)
                )
                .scalars()
                .all()
            )
        # 好感度是全局锚点：一次查全表再按 user_id 拼回去，避免 N+1 次查询
        with self.Session() as session:
            affinities = {
                str(item.user_id): int(item.affinity_raw or DEFAULT_AFFINITY)
                for item in session.execute(select(UserAffinity)).scalars().all()
            }
        return [
            {
                "user_id": row.user_id,
                # 看板要能看出这一行属于哪个会话分区（g:<群号> / p:<QQ号> / 老库 ""）
                "chat_scope": row.chat_scope or DEFAULT_CHAT_SCOPE,
                "emotion": int(row.emotion_current if row.emotion_current is not None else DEFAULT_EMOTION),
                "affinity": affinities.get(str(row.user_id), DEFAULT_AFFINITY),
                "level": row.emotion_level or DEFAULT_LEVEL,
                "last_update": row.last_update.isoformat() if row.last_update else None,
            }
            for row in rows
        ]

    # ---------------------------------------------------------- 跨进程锁

    async def _acquire_state_lock(
        self, user_id: str, chat_scope: str = DEFAULT_CHAT_SCOPE
    ) -> bool:
        """获取基于 ``(user_id, chat_scope)`` 的跨进程互斥锁，超时返回 False。

        diskcache 5.x 没有 ``Cache.lock``，这里用与 ``diskcache.Lock``
        相同的原子 ``Cache.add`` 自旋方式实现，并加上超时与自动过期。
        """
        # 空 scope 沿用旧的 key 形状（老库/老调用方/运维排查命令都按它）；
        # 真实分区才拼后缀，用来隔离「同一个人在 A 群和 B 群同时说话」。
        scope = str(chat_scope)
        key = "lock:state:" + user_id + ("|" + scope if scope else "")
        deadline = time.monotonic() + max(self.lock_timeout, 0.0)
        while True:
            try:
                if self.cache.add(key, None, expire=STATE_LOCK_TTL, retry=True):
                    return True
            except Exception:
                return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(STATE_LOCK_POLL)

    def _release_state_lock(
        self, user_id: str, chat_scope: str = DEFAULT_CHAT_SCOPE
    ) -> None:
        """释放锁（失败不影响主流程，锁 key 会自然过期）。"""
        try:
            scope = str(chat_scope)
            self.cache.delete(
                "lock:state:" + user_id + ("|" + scope if scope else ""), retry=True
            )
        except Exception:
            pass

    # ---------------------------------------------------------- 衰减 + 平滑

    def _habituation_alpha(
        self,
        user_id: str,
        target: int,
        risk: bool = False,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
    ) -> tuple[float, int]:
        """习惯化：同极性刺激过密时削减平滑系数（规格 §四.3）。

        用户连发「哈哈哈哈」「好开心好开心」时，不该一路涨到狂热——人会有平台期。
        做法：维护每个用户的滑动窗口（默认 15 分钟），统计**同极性**刺激次数：

        - 1~2 次：正常 alpha 0.2
        - 3 次：0.1
        - 4 次以上：0.05

        出现反极性刺激时天然「重置」（统计只数同极性的），无需显式清零。
        风险消息不走习惯化：危机时刻要拉满反应，不能被「习惯了」削弱。

        Returns:
            (本次使用的 alpha, 窗口内同极性次数)。
        """
        # 习惯化窗口同样按会话分区：在 A 群连夸三次不该让 B 群的第一句就被削
        habit_key = (str(user_id), str(chat_scope))
        if risk:
            with self._habit_lock:
                self._habit.pop(habit_key, None)  # 危机重置窗口
            return SMOOTHING_FACTOR, 0

        offset = target - NEUTRAL_TARGET
        polarity = 0 if abs(offset) < HABITUATION_DEADBAND else (1 if offset > 0 else -1)

        now = time.time()
        cutoff = now - HABITUATION_WINDOW_SECONDS
        with self._habit_lock:
            window = self._habit.get(habit_key)
            if window is None:
                window = deque(maxlen=32)
                self._habit[habit_key] = window
            # 丢掉过期项
            while window and window[0][0] < cutoff:
                window.popleft()
            window.append((now, polarity))
            same = sum(1 for ts, p in window if ts >= cutoff and p == polarity)
            # F-09：惰性清扫放在插入之后，容量上限才是硬上限（插入前清扫会
            # 让字典短暂达到 max+1）。本函数已持锁，必须走 _locked 版本。
            self._prune_habits_locked(now)

        if polarity == 0 or same < HABITUATION_TRIGGER:
            return SMOOTHING_FACTOR, same
        # 第 3 次 -> 0.1，第 4 次及以后 -> 0.05
        index = min(same - HABITUATION_TRIGGER + 1, len(HABITUATION_ALPHAS) - 1)
        return HABITUATION_ALPHAS[index], same

    def prune_habits(self, now: Optional[float] = None) -> int:
        """按 TTL / 容量回收 ``_habit``（F-09）。**自己取锁**，可被外部直接调用。

        返回被移除的键数。与 :meth:`_prune_habits_locked` 的分工：
        调用方**尚未**持 ``_habit_lock`` 时用本方法；已持锁时必须用 ``_locked``
        版本——``threading.Lock`` 不可重入，自己再取一次会当场死锁。
        """
        with self._habit_lock:
            return self._prune_habits_locked(time.time() if now is None else now)

    def _prune_habits_locked(self, now: float) -> int:
        """回收实现，**要求调用方已持 ``_habit_lock``**。

        两件事，顺序不能反：

        1. **TTL**：键的「最后活动时间」取 deque 尾元素时间戳；deque 为空
           （理论上不该出现）时视为立即过期。超过 ``HABIT_TTL_SECONDS`` 删除。
        2. **容量**：仍超过 ``HABIT_CACHE_MAX`` 时，按最后活动时间升序淘汰最旧
           的键（LRU 近似）。用 deque 尾部时间戳而不引入第二份访问时间字典，
           避免双状态不同步。

        清扫被 ``HABIT_PRUNE_INTERVAL`` 摊薄：距上次清扫不足间隔且未超容量时
        直接返回，保证消息路径不会每条都做 O(n) 遍历。
        """
        last = self._habit_last_prune
        over_capacity = len(self._habit) > self._habit_max
        if now - last < self._habit_prune_interval and not over_capacity:
            return 0
        self._habit_last_prune = now

        removed = 0
        cutoff = now - self._habit_ttl
        for key in list(self._habit.keys()):
            window = self._habit.get(key)
            if not window:
                del self._habit[key]
                removed += 1
                continue
            if window[-1][0] < cutoff:
                del self._habit[key]
                removed += 1

        overflow = len(self._habit) - self._habit_max
        if overflow > 0:
            # 最久未活跃的键排前面；deque 尾时间戳即最后活动时间
            ordered = sorted(
                self._habit.items(),
                key=lambda item: item[1][-1][0] if item[1] else 0.0,
            )
            for key, _window in ordered[:overflow]:
                del self._habit[key]
                removed += 1
        return removed

    @staticmethod
    def _apply_decay(
        emotion: int, last_update: Any, resilience: float = 0.5
    ) -> tuple[float, bool]:
        """半衰期回归：每 30 分钟向 50 靠近 10%（负面情绪减半速度）。

        规格 §四.2：
        - 正常衰减：``time_delta`` 每过 30 分钟，``E_prev`` 向 50 靠近 10%；
        - 负面偏差：``E < 50`` 时衰减速度减半（让它多难过一会儿，更拟人）。

        实现上把「每 30 分钟 10%」写成指数形式 ``(1-rate) ** (秒/1800)``，
        这样 15 分钟、3 小时都能连续计算，不必按整段跳跃。

        Returns:
            (衰减后的基准值, 是否发生了衰减)。
        """
        if last_update is None:
            return float(emotion), False
        moment = last_update
        if isinstance(moment, str):
            try:
                cleaned = moment.replace("Z", "").split("+")[0]
                moment = datetime.datetime.fromisoformat(cleaned)
            except ValueError:
                return float(emotion), False
        if getattr(moment, "tzinfo", None) is not None:
            moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        try:
            seconds = (utcnow() - moment).total_seconds()
        except TypeError:
            return float(emotion), False
        if seconds < DECAY_MIN_SECONDS:
            return float(emotion), False

        rate = DECAY_RATE
        if emotion < NEUTRAL_TARGET:
            # 负面偏差：默认衰减减半；**韧性越高越能抵消这个偏差**（0.5 = 不额外影响）
            bias = DECAY_NEGATIVE_BIAS * (0.5 + max(0.0, min(1.0, float(resilience))))
            rate *= min(1.0, bias)
        steps = seconds / DECAY_INTERVAL_SECONDS
        factor = (1.0 - rate) ** steps
        decayed = NEUTRAL_TARGET + (float(emotion) - NEUTRAL_TARGET) * factor
        return decayed, True

    async def update_emotion(
        self,
        user_id: str,
        target_score: int,
        event_key: Optional[str] = None,
        risk: bool = False,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
        request_ts: Optional[int] = None,
    ) -> dict[str, Any]:
        """衰减 + 平滑 + 落库，返回本次更新的完整快照。

        Args:
            user_id: 用户 ID。
            target_score: 目标情绪分（0-100，越界会被 clamp）。
            event_key: 幂等键。与数据库里记录的 ``last_event_key`` 相同则直接
                返回当前状态，不扣分、不改状态（对抗重试 / 幽灵消息）。
            risk: 是否命中安全风险（命中则等级直接 CRISIS）。
            chat_scope: 会话分区（``g:<群号>`` / ``p:<QQ号>``）。情绪按它分房，
                好感度不受它影响（全局锚点）。
            request_ts: 本次请求的到达时间戳（整型毫秒，P0-8）。用于乱序防御：
                比库里 ``arrival_ts`` 更旧的消息直接丢弃（``applied=False``），
                绝不覆盖更新的状态。``None`` 时跳过比对并沿用库里旧值——
                这样内部调用/老测试不带时间戳也不会互相打架。

        Returns:
            dict，至少包含 ``user_id`` / ``emotion_prev`` / ``emotion_current`` /
            ``emotion_level`` / ``delta`` / ``affinity_raw`` / ``decayed`` /
            ``applied`` / ``last_update``。
        """
        target = to_score(target_score, NEUTRAL_TARGET)
        acquired = await self._acquire_state_lock(user_id, chat_scope)
        if not acquired:
            # 等锁超时：返回当前状态而不是抛错
            snapshot = await asyncio.to_thread(self._get_or_create, user_id, chat_scope)
            return self._build_result(
                user_id=user_id,
                snapshot=snapshot,
                base_prev=float(snapshot["emotion"]),
                emotion=int(snapshot["emotion"]),
                level=snapshot["level"],
                delta=0.0,
                decayed=False,
                applied=False,
                target=target,
                last_update=snapshot["last_update"],
            )

        try:
            snapshot = await asyncio.to_thread(self._get_or_create, user_id, chat_scope)

            # ---- 乱序防御（P0-8）：拿到锁之后、写状态之前先比时间戳 ----
            # 必须在锁内做：否则两条并发请求会同时通过比对，后写的反而更旧。
            current_ts = int(snapshot.get("arrival_ts") or 0)
            if request_ts is not None:
                incoming = int(request_ts)
                if incoming < current_ts:
                    logger.warning(
                        "[WARN] Out-of-order message dropped: user=%s scope=%s"
                        " request_ts=%s < arrival_ts=%s",
                        user_id,
                        chat_scope,
                        incoming,
                        current_ts,
                    )
                    return self._build_result(
                        user_id=user_id,
                        snapshot=snapshot,
                        base_prev=float(snapshot["emotion"]),
                        emotion=int(snapshot["emotion"]),
                        level=snapshot["level"],
                        delta=0.0,
                        decayed=False,
                        applied=False,
                        target=target,
                        last_update=snapshot["last_update"],
                    )

            # ---- 幂等：与上一次生效的 event_key 相同 -> 原样返回，绝不扣分 ----
            if event_key and snapshot.get("last_event_key") == str(event_key):
                return self._build_result(
                    user_id=user_id,
                    snapshot=snapshot,
                    base_prev=float(snapshot["emotion"]),
                    emotion=int(snapshot["emotion"]),
                    level=snapshot["level"],
                    delta=0.0,
                    decayed=False,
                    applied=False,
                    target=target,
                    last_update=snapshot["last_update"],
                )

            stored_prev = int(snapshot["emotion"])
            base_prev, decayed = self._apply_decay(
                stored_prev, snapshot["last_update"], resilience=self.traits.resilience
            )

            # 习惯化：同极性高频刺激 -> alpha 逐级衰减（0.2 -> 0.1 -> 0.05）
            alpha, habit_count = self._habituation_alpha(
                user_id, target, risk=risk, chat_scope=chat_scope
            )
            delta = clamp(alpha * (target - base_prev), -MAX_DELTA, MAX_DELTA)
            # 性格调制：以 0.5 为「无偏向」基准，偏离才放大/收窄
            if target < NEUTRAL_TARGET:
                delta *= 1.0 + (self.traits.neuroticism - 0.5) * NEUROTIC_GAIN
            elif target > NEUTRAL_TARGET:
                delta *= 1.0 + (self.traits.extraversion - 0.5) * EXTRAVERT_GAIN
            # 调制后必须重新夹一次：±5 是硬不变量，不能被性格系数突破
            delta = clamp(delta, -MAX_DELTA, MAX_DELTA)
            emotion = int(round(clamp(base_prev + delta, SCORE_MIN, SCORE_MAX)))

            level = LEVEL_CRISIS if risk else level_of(emotion)
            if level == LEVEL_CRISIS and user_id in self.whitelist:
                level = LEVEL_L2  # 白名单用户不进危机压制

            moment = utcnow()
            await asyncio.to_thread(
                self._write_emotion,
                user_id,
                emotion,
                level,
                moment,
                event_key,
                chat_scope,
                request_ts,
            )

            return self._build_result(
                user_id=user_id,
                snapshot=snapshot,
                base_prev=base_prev,
                emotion=emotion,
                level=level,
                delta=delta,
                decayed=decayed,
                applied=True,
                target=target,
                last_update=moment,
                stored_prev=stored_prev,
                alpha=alpha,
                habit_count=habit_count,
                arrival_ts=int(request_ts) if request_ts is not None else current_ts,
            )
        finally:
            self._release_state_lock(user_id, chat_scope)

    @staticmethod
    def _build_result(
        user_id: str,
        snapshot: dict[str, Any],
        base_prev: float,
        emotion: int,
        level: str,
        delta: float,
        decayed: bool,
        applied: bool,
        target: int,
        last_update: Any,
        stored_prev: Optional[int] = None,
        alpha: float = SMOOTHING_FACTOR,
        habit_count: int = 0,
        arrival_ts: Optional[int] = None,
    ) -> dict[str, Any]:
        """统一拼装 update_emotion 的返回结构。

        ``arrival_ts`` 是本分区当前的时间水位线（P0-8）：调用方据此判断
        「这条消息有没有被乱序丢弃」。未显式传入时回落 ``snapshot``。
        """
        moment = last_update
        if hasattr(moment, "isoformat"):
            moment = moment.isoformat()
        if arrival_ts is None:
            arrival_ts = int(snapshot.get("arrival_ts") or 0)
        return {
            "user_id": user_id,
            "emotion_prev": round(float(base_prev), 2),
            "emotion_stored_prev": int(
                snapshot["emotion"] if stored_prev is None else stored_prev
            ),
            "emotion_current": int(emotion),
            "emotion_level": level,
            # 危机是「状态覆盖」而非「分数区间」：等级被判为 CRISIS 时，语气带必须跟着走，
            # 否则会出现 level=CRISIS 但 band=stable 的自相矛盾。
            "emotion_band": "crisis" if level == LEVEL_CRISIS else tone_band_of(int(emotion)),
            "delta": round(float(delta), 2),
            "alpha": round(float(alpha), 3),
            "habituation": int(habit_count),
            "affinity_raw": int(snapshot["affinity"]),
            "decayed": bool(decayed),
            "applied": bool(applied),
            "target": int(target),
            "last_update": moment,
            "arrival_ts": int(arrival_ts or 0),
        }

    # ---------------------------------------------------------- Bot 自身情绪

    def _get_bot_mood(self) -> dict[str, Any]:
        """读 Bot 自己的心情（全局单行，不存在则建）。"""
        with self.Session() as session:
            row = session.get(BotMood, 1)
            if row is None:
                row = BotMood(id=1, emotion_current=DEFAULT_EMOTION, emotion_level=DEFAULT_LEVEL, last_update=utcnow())
                session.add(row)
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    row = session.get(BotMood, 1)
                    if row is None:
                        return {"emotion": DEFAULT_EMOTION, "level": DEFAULT_LEVEL, "last_update": None}
            return {
                "emotion": int(row.emotion_current if row.emotion_current is not None else DEFAULT_EMOTION),
                "level": row.emotion_level or DEFAULT_LEVEL,
                "last_update": row.last_update,
            }

    def _update_bot_mood(self, user_target: int) -> dict[str, Any]:
        """把「对这个人的情绪」按阻尼折算进「它自己的心情」。

        为什么是阻尼而不是直接取用：Bot 的整体心情是慢变量。被 A 骂一句不该让它
        对 B 也立刻变冷，但连着被骂几句，整体基调确实会沉下去。
        """
        try:
            with self.Session() as session:
                row = session.get(BotMood, 1)
                if row is None:
                    row = BotMood(id=1, emotion_current=DEFAULT_EMOTION, emotion_level=DEFAULT_LEVEL, last_update=utcnow())
                    session.add(row)
                    session.flush()
                base, _ = self._apply_decay(
                    int(row.emotion_current), row.last_update, resilience=self.traits.resilience
                )
                delta = clamp(
                    BOT_MOOD_DAMPING * (float(user_target) - base),
                    -BOT_MOOD_MAX_DELTA,
                    BOT_MOOD_MAX_DELTA,
                )
                current = int(round(clamp(base + delta, SCORE_MIN, SCORE_MAX)))
                row.emotion_current = current
                row.emotion_level = level_of(current)
                row.last_update = utcnow()
                session.commit()
                return {"emotion": current, "level": row.emotion_level, "delta": round(delta, 2)}
        except Exception:
            logger.warning("Bot 心情更新失败（不影响主流程）", exc_info=True)
            return {"emotion": NEUTRAL_TARGET, "level": level_of(NEUTRAL_TARGET), "delta": 0.0}

    # ---------------------------------------------------------- 好感度

    def _bump_affinity(self, user_id: str, emotion: int) -> int:
        """按当前情绪微调好感度（无上限，不低于 0），返回新值。

        **不接收 chat_scope**：好感度是全局锚点，情绪来自哪个群都不该让它分裂。
        """
        current = self._read_affinity(user_id)
        if emotion >= 70:
            step = 2
        elif emotion >= 60:
            step = 1
        elif emotion <= 30:
            step = -2
        elif emotion <= 40:
            step = -1
        else:
            step = 0
        if step == 0:
            return current
        new_value = max(0, current + step)
        self._write_affinity(user_id, new_value)
        return new_value

    # ---------------------------------------------------------- 主流程

    async def handle_message(
        self,
        user_id: str,
        message: str,
        debounce: bool = False,
        event_key: Optional[str] = None,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
        request_ts: Optional[int] = None,
    ) -> dict[str, Any]:
        """对外主入口：兼容阶段一签名与返回结构。

        Args:
            user_id: 用户 ID。
            message: 用户消息。
            debounce: 是否先走防抖合并。
            event_key: 调用方自带幂等键（第三阶段的队列消费者应传
                ``make_event_key(user_id, merged_text, batch_id)``；
                Adapter 有原生 message_id 时也可以直接传它）。
                为空时引擎按「本次批次」自动生成，保证每次调用是一个独立事件。
            chat_scope: 会话分区（``g:<群号>`` / ``p:<QQ号>``）。由入口
                （``main.py`` 的请求解析处）**唯一一次**派生后一路透传，
                下游模块禁止自拼。默认空串只用于兼容老库/老调用方。
            request_ts: 本次请求的到达时间戳（整型毫秒，P0-8 乱序防御）。
                透传给 :meth:`update_emotion` 做比对；``None`` 时跳过比对。

        Note:
            引擎**不会**按文本内容去重 —— 用户真心连发两次同一句话，就该算两次。
            幽灵消息（NapCat 重推）请在入口用 :class:`IngressDeduper` 过滤。
        """
        text = "" if message is None else str(message)
        scope = str(chat_scope)
        # ---- H-A1 特权放行：判定前短路（不扣分/不演化/不计数/不写记忆）----
        # 安全优先于特权：命中硬拦截词的特权用户仍走完整链路——
        # 危机处置不能被一份配置名单绕过（自审结论：否则特权用户求救也得不到 CRISIS 照顾）
        if user_id in self.privileged and not self.auditor.check_rules(text):
            return await self._whitelisted_payload(
                user_id, text, event_key, scope, request_ts
            )
        if debounce and self.debouncer.window > 0:
            # 批次 ID 在冲刷时分配：一次合并 = 一个 event_key
            # 防抖缓冲区也必须按分区隔离：同一个人在 A、B 两个群同时说话，
            # 若共用一个 buffer，两条不同群的消息会被合并成一条（跨群串味）。
            batch = await self.debouncer.merge(user_id + "|" + scope, text)
            text = batch.text
            resolved = event_key or batch.event_key(user_id)
        else:
            resolved = event_key or make_event_key(user_id, text, new_batch_id())
        return await self.process(
            user_id,
            text,
            event_key=resolved,
            chat_scope=scope,
            request_ts=request_ts,
        )

    async def _whitelisted_payload(
        self,
        user_id: str,
        text: str,
        event_key: Optional[str],
        chat_scope: str = DEFAULT_CHAT_SCOPE,
        request_ts: Optional[int] = None,
    ) -> dict[str, Any]:
        """H-A1：特权用户的判定前短路载荷。

        跳过：Track A/B 判定、auditor 计数与解禁、``update_emotion``、Bot 自身情绪、
        好感度、``memory.store``、``bump_activity``——特权用户的闲聊不推表。
        保留：按**当前状态**生成 inner_os / actions / final_prompt（Bot 仍要回话），
        记忆检索只读不写。``debug.whitelisted=true`` 留痕，logger 出审计行。

        ``request_ts`` 只用于 debug 留痕（P0-8）；本分支不写库，水位线保持不变。
        """
        snapshot = await asyncio.to_thread(self._get_or_create, user_id, chat_scope)
        level = str(snapshot["level"])
        affinity = int(snapshot["affinity"])
        emotion = int(snapshot["emotion"])
        inner_os = self.prompt_builder.build_inner_os(level, affinity)
        actions = self._actions(user_id, text, level, affinity)
        try:
            memories = await self.memory.retrieve(user_id, text, chat_scope=chat_scope)
        except Exception:  # 记忆挂了不能拖垮对话
            memories = []
        final_prompt = self.prompt_builder.build_final_prompt(inner_os, text, memories)
        bot_mood = await asyncio.to_thread(self._get_bot_mood)
        logger.info(
            "特权放行：user=%s 不进入情绪判定（event_key=%s, level=%s）",
            user_id, event_key or "-", level,
        )
        payload: dict[str, Any] = {
            "actions": actions,
            "inner_os": inner_os or NEUTRAL_INNER_OS,
            "final_prompt": final_prompt,
            "debug": {
                "emotion": emotion,
                "prev": float(emotion),
                "delta": 0.0,
                "affinity": affinity,
                "level": level,
                "tone": level,
                "crisis": level == LEVEL_CRISIS,
                "path": "whitelist",
                "track": "-",
                "reason": "特权放行：判定前短路，不扣分不演化",
                "confidence": 1.0,
                "undefined": False,
                "intent_type": "whitelisted",
                "band": tone_band_of(emotion),
                "alpha": 0.0,
                "habituation": 0,
                "decayed": False,
                "applied": False,
                "whitelisted": True,
                # P0-8：提前返回也要回显**库中实际水位线**，否则接口层会把本次
                # 请求的旧 timestamp 当成台账回显（该分支并不写库，水位线不会动）。
                "arrival_ts": int(snapshot.get("arrival_ts") or 0),
                "request_ts": int(request_ts or 0),
                "chat_scope": str(chat_scope),
                "event_key": event_key or "",
                "memories": len(memories),
                "bot_mood": bot_mood.get("emotion"),
                "bot_mood_level": bot_mood.get("level"),
                "audit": "skipped",
                "stimulus": 0,
                "unlock_probability": 0.0,
                "traits": {
                    "neuroticism": self.traits.neuroticism,
                    "extraversion": self.traits.extraversion,
                    "agreeableness": self.traits.agreeableness,
                    "resilience": self.traits.resilience,
                },
            },
        }
        recall = self._recall_hint(level)
        if recall:
            payload["recall_hint"] = recall
        return payload

    async def _track_b_once(
        self,
        event_key: Optional[str],
        text: str,
        memories: list[Any],
        user_id: str,
        *,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
        with_relationship: bool = False,
    ) -> TrackBVerdict:
        """同一 event_key 的并发 Track B 评估只打一次 LLM（批次单飞）。

        为什么需要它：防抖合并批内的多个等待者共享同一个 event_key，但会**并发**
        各自走完 ``process()``。后到者的关系快照 / 记忆上下文可能已读到前者刚落库
        的结果，Track B 缓存键（``tb:digest(model,text,context,relation)``）因此
        不同——缓存只能兜住「前一次已写完」的串行场景，兜不住并发窗口。按 event_key
        做 in-flight 单飞后：一批一次判定，所有等待者共享同一分数；各自的
        ``update_emotion`` 仍按 event_key 幂等（只有一个 applied=True），
        「引擎按批次去重、不按文本去重」的架构决策不变。

        关系快照只在**领头者**路径内读取：跟随者直接搭车，既少一次串行 DB 活
        （缩短到达查询点的时间），也保证判定用的关系状态与 in-flight 认领同步。

        无 event_key 时退化为直接调用（保持旧行为）。异常/取消会传播给所有等待者，
        且 in-flight 条目在 ``finally`` 清除，不会漏。
        """
        key = event_key or ""
        if not key:
            return await self._evaluate_track_b_locked(
                text,
                memories,
                user_id,
                chat_scope=chat_scope,
                with_relationship=with_relationship,
            )
        pending = self._track_b_inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[TrackBVerdict]" = loop.create_future()
        self._track_b_inflight[key] = future
        try:
            verdict = await self._evaluate_track_b_locked(
                text,
                memories,
                user_id,
                chat_scope=chat_scope,
                with_relationship=with_relationship,
            )
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            future.exception()  # 标记已取回，避免 never-retrieved 警告
            raise
        else:
            if not future.done():
                future.set_result(verdict)
            return verdict
        finally:
            self._track_b_inflight.pop(key, None)

    async def _evaluate_track_b_locked(
        self,
        text: str,
        memories: list[Any],
        user_id: str,
        *,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
        with_relationship: bool = False,
    ) -> TrackBVerdict:
        """真正打一次 Track B；``with_relationship`` 时先读关系快照（仅领头者）。

        **闸门唯一收口点（F-25）**：全引擎只有本方法会对 Track B 结果施加
        P0-4 防记仇闸门（:func:`_apply_reason_whitelist`）。任何绕过本方法、
        直接调 :meth:`evaluate_track_b` / :meth:`evaluate_track_b_verdict`
        的路径都**不过闸门**，拿到的是裸 verdict——那两个公开方法的 docstring
        已显式声明该契约，勿在别处补第二道闸门（会造成双重驳回与语义漂移）。
        """
        relationship: Optional[dict[str, Any]] = None
        if with_relationship:
            snapshot = await asyncio.to_thread(self._get_or_create, user_id, chat_scope)
            relationship = {
                "affinity": snapshot["affinity"],
                "emotion": snapshot["emotion"],
                "level": snapshot["level"],
            }
        score, reason, confidence = await self.evaluate_track_b_verdict(
            text, memories, user_id=user_id, relationship=relationship
        )
        # P0-4/F-20（P1-3）：闸门下沉到 verdict 唯一汇聚点，所有 process 路径必经；
        # evaluate_track_b_verdict 对外入口保持裸 verdict 语义不变。
        return (self._apply_reason_whitelist(score, reason), reason, confidence)

    @staticmethod
    def _apply_reason_whitelist(target: int, track_b_reason: Optional[str]) -> int:
        """P0-4 防记仇闸门：负面扣分必须出自白名单理由，否则驳回回中性分。

        判定表：

        ==================  ====================  ==========================
        target               track_b_reason        结果
        ==================  ====================  ==========================
        >= 50               任意                 原样返回（不拦正向/中性）
        < 50                ``None``             驳回 -> ``NEUTRAL_TARGET``（50）（无理由可审）
        < 50                命中 REASON_WHITELIST 原样返回（是心疼，不是记仇）
        < 50                未命中 / 空串        驳回 -> ``NEUTRAL_TARGET``（50）
        < 50                白名单词全部被否定   驳回 -> ``NEUTRAL_TARGET``（50）
        ==================  ====================  ==========================

        否定前缀（F-20 · P1-1）：白名单词前 5 字窗口内出现「不/没/别/无/非」
        视为反话，不命中——「我不会抱歉」不是道歉，「没什么好担心的」也不是心
        疼。每处出现逐一检查，只要存在一处未被否定修饰的出现即视为正话命中
        （模块级 ``_is_negated``，闸门口径；勿与 ``_diagnose_negation`` 混淆）。

        否定词不得跨标点生效（``_NEGATION_BREAKERS``）：窗口内若夹标点/空白，
        只取最后一个断句符之后的部分参与判定。因此「怪自己没用，自责」里的
        「没」隔着「用，」，不算否定「自责」——该正话必须照常放行。

        否定词不得跨转折生效（``_NEGATION_TURNS``：但/可是/不过/然而）：窗口内
        若夹转折连词，只取最后一个转折连词之后的部分参与判定。因此「不抱歉
        但心疼」里的「不」修饰的是「抱歉」而不是「心疼」——「心疼」必须照常放行。

        ``None`` 与空串现在**同策**（F-22 · fail-closed）：两者都意味着「没有通过
        审计」，一律驳回回 ``NEUTRAL_TARGET``。``None`` 只可能来自「模型没按三字段
        协议返回」或旧测试替身——此时没有任何理由可审，按记仇处理，而不是静默放行
        让整道闸门失效。
        """
        if target >= NEUTRAL_TARGET:
            return target
        if track_b_reason is None:
            return NEUTRAL_TARGET
        if any(
            word in track_b_reason and not _is_negated(track_b_reason, word)
            for word in REASON_WHITELIST
        ):
            return target
        logger.warning(
            "[WARN] Emotion drop rejected by reason whitelist: target=%s reason=%r",
            target,
            track_b_reason,
        )
        return NEUTRAL_TARGET

    async def process(
        self,
        user_id: str,
        message: str,
        event_key: Optional[str] = None,
        chat_scope: str = DEFAULT_CHAT_SCOPE,
        request_ts: Optional[int] = None,
    ) -> dict[str, Any]:
        """Track A -> (语义冲突时) Track B -> update_emotion -> 内心 OS + 动作。

        ``request_ts`` 会一路透传到 :meth:`update_emotion`，在那里于锁内做
        乱序比对（P0-8）：比库里 ``arrival_ts`` 旧的消息被丢弃、不覆盖状态。
        """
        text = "" if message is None else str(message)
        scope = str(chat_scope)

        # ---------- 0. 记忆检索（插拔路由：宿主 > 外部 > 本地兜底） ----------
        # 记忆按会话分区检索：A 群的聊天内容绝不进 B 群的 prompt（§3.4.7）
        try:
            memories = await self.memory.retrieve(user_id, text, chat_scope=scope)
        except Exception:  # 记忆挂了不能拖垮对话
            memories = []

        # ---------- 1. Track A ----------
        track_a = self.evaluate_track_a(text)
        risk = bool(track_a.get("risk"))
        path = "fast"
        track = "A"
        confidence = float(track_a.get("confidence", 0.5) or 0.5)
        reason = str(track_a.get("reason", "无明显信号"))
        # P0-4：只有真走了 Track B 才有理由/置信度；快速路径保持 None/0.0，
        # 否则 debug 回显与白名单判定会引用到未绑定名字。
        track_b_reason: Optional[str] = None
        track_b_confidence: float = 0.0

        # ---------- 2. Track B（语义冲突 / 无明确信号时） ----------
        # 低置信度且没有任何命中词 = 这句话的情绪是「未定义」的。
        # 正负打平也属于方向未定，同样交回 LLM（此时置信度可能很高，但不代表有方向）。
        # 与其猜一个方向，不如把判断权交回 LLM，并带上关系状态让它结合好感度理解。
        _hits = track_a.get("hits") or []
        _tied = bool(_hits) and bool(track_a.get("undefined"))
        undefined = bool(track_a.get("undefined")) and (
            _tied or confidence < self.track_a_min_confidence
        )
        # 风险优先短路：命中安全敏感词就不再走任何推断（规格 §三 Track A）
        if risk:
            target = RISK_TARGET
            path = "crisis"
            track = "A"
            confidence = 1.0
            reason = "安全敏感词 -> 危机短路"
            undefined = False
        elif track_a.get("semantic_conflict"):
            if self.track_b.enabled:
                target, track_b_reason, track_b_confidence = await self._track_b_once(
                    event_key, text, memories, user_id, chat_scope=scope
                )
                path = "deep"
                track = "B"
                confidence = 0.9
                reason = "语义冲突（{0}）-> LLM 复核".format(
                    ", ".join(track_a.get("hits", []) or [])
                )
            else:
                # 未配置 LLM 时降级：用命中词方向做保守估计
                target = NEUTRAL_TARGET
                reason = "语义冲突但 Track B 未启用，取中性值"
        elif undefined:
            if self.track_b.enabled:
                # 关系快照由 _track_b_once 的领头者路径读取（跟随者直接搭车），
                # 关系详情进 Track B 提示词，reason 不再重复引用快照
                target, track_b_reason, track_b_confidence = await self._track_b_once(
                    event_key,
                    text,
                    memories,
                    user_id,
                    chat_scope=scope,
                    with_relationship=True,
                )
                path = "deep"
                track = "B"
                confidence = 0.9
                reason = "无明显信号 -> LLM 接管（带关系状态）"
            else:
                target = NEUTRAL_TARGET
                reason = "无明显信号且 Track B 未启用，取中性值"
        else:
            target = to_score(track_a.get("target", NEUTRAL_TARGET), NEUTRAL_TARGET)

        # ---------- 2.5 意图兜底：noise 不参与情绪演绎 ----------
        intent_type = str(track_a.get("intent_type") or "unknown")
        if intent_type == "noise":
            snapshot = await asyncio.to_thread(self._get_or_create, user_id, scope)
            level = str(snapshot["level"])
            affinity = int(snapshot["affinity"])
            inner_os = self.prompt_builder.build_inner_os(level, affinity)
            final_prompt = self.prompt_builder.build_final_prompt(
                inner_os, text, memories, noise=True
            )
            bot_mood = await asyncio.to_thread(self._get_bot_mood)
            noise_text = NOISE_REPLIES[sum(ord(c) for c in text) % len(NOISE_REPLIES)]
            return {
                "actions": [{"type": "text", "content": noise_text.strip("（）")}],
                "inner_os": inner_os or NEUTRAL_INNER_OS,
                "final_prompt": final_prompt,
                "debug": {
                    "emotion": int(snapshot["emotion"]),
                    "prev": float(snapshot["emotion"]),
                    "delta": 0.0,
                    "affinity": affinity,
                    "level": level,
                    "tone": level,
                    "crisis": level == LEVEL_CRISIS,
                    "path": "noise",
                    "track": "A",
                    "reason": "意图为噪音，不触发情绪更新",
                    "confidence": round(confidence, 2),
                    "target": NEUTRAL_TARGET,
                    "undefined": False,
                    "intent_type": "noise",
                    "band": "crisis" if level == LEVEL_CRISIS else tone_band_of(int(snapshot["emotion"])),
                    "alpha": 0.0,
                    "habituation": 0,
                    "decayed": False,
                    "applied": False,
                    "event_key": event_key or "",
                    # 分区留痕：noise 分支提前返回，也要能看出消息落在哪个分区
                    "chat_scope": scope,
                    # P0-8：noise 不写库，因此这里回显的是**库中实际水位线**
                    # （不是本次请求的 timestamp），否则旧消息会把台账回显成旧值。
                    "arrival_ts": int(snapshot.get("arrival_ts") or 0),
                    "request_ts": int(request_ts or 0),
                    "memories": len(memories),
                    "bot_mood": bot_mood.get("emotion"),
                    "audit": "skipped",
                },
            }

        # ---------- 2.6 潜意识审核（规则拦截 → 连续刺激 → 概率解禁） ----------
        audit = self.auditor.audit(
            user_id, text, target, self.traits, chat_scope=scope
        )
        override = audit.directive or None

        # ---------- 3. 平滑 + 衰减 + 落库（event_key 保证重试不重复扣分） ----------
        state = await self.update_emotion(
            user_id,
            target,
            event_key=event_key,
            risk=risk,
            chat_scope=scope,
            request_ts=request_ts,
        )

        # ---------- 3.5 Bot 自身情绪（慢变量，阻尼折算） ----------
        bot_mood = await asyncio.to_thread(self._update_bot_mood, target)

        # ---------- 4. 好感度（独立于情绪值，原样保留 DB 里的值再微调） ----------
        affinity = await asyncio.to_thread(
            self._bump_affinity, user_id, int(state["emotion_current"])
        )

        # ---------- 5. 内心 OS + 动作 ----------
        level = str(state["emotion_level"])
        inner_os = self.prompt_builder.build_inner_os(level, affinity)
        actions = self._actions(user_id, text, level, affinity)
        # 记忆只作为动态尾部追加块，绝不拼进人设/System 前缀
        final_prompt = self.prompt_builder.build_final_prompt(
            inner_os,
            text,
            memories,
            crisis=(level == LEVEL_CRISIS),
            override=override,
        )

        payload: dict[str, Any] = {
            "actions": actions,
            "inner_os": inner_os or NEUTRAL_INNER_OS,
            "final_prompt": final_prompt,
            "debug": {
                "emotion": int(state["emotion_current"]),
                "prev": state["emotion_prev"],
                "delta": state["delta"],
                "affinity": int(affinity),
                "level": level,
                "tone": level,
                "crisis": level == LEVEL_CRISIS,
                "path": path,
                "track": track,
                "reason": reason,
                "confidence": round(confidence, 2),
                "target": int(target),
                # P0-4：Track A 的 reason/confidence 与 Track B 的分开回显，
                # 否则前端只能看到引擎自己的路由说明，看不到模型为何扣分。
                "track_b_reason": track_b_reason,
                "track_b_confidence": round(track_b_confidence, 2),
                # 情绪是否「未定义」：Track A 无命中或正负打平。此时 target 由 LLM 或中性值给出。
                "undefined": bool(undefined),
                # 六段语气带（crisis/deep_low/low/stable/up/high）与本次平滑系数
                "band": state.get("emotion_band") or tone_band_of(int(state["emotion_current"])),
                "alpha": state.get("alpha", SMOOTHING_FACTOR),
                "habituation": state.get("habituation", 0),
                "decayed": bool(state["decayed"]),
                "applied": bool(state["applied"]),
                "event_key": event_key or "",
                # P0-8 乱序防御留痕：本次请求的时间戳与是否被判为乱序丢弃
                "request_ts": int(request_ts or 0),
                "arrival_ts": int(state.get("arrival_ts") or 0),
                # 分区留痕：排查「同一个人两个群为什么情绪不同」时一眼能看出来
                "chat_scope": scope,
                "memories": len(memories),
                # V2：意图 / Bot 自身心情 / 解禁状态
                "intent_type": intent_type,
                "bot_mood": bot_mood.get("emotion"),
                "bot_mood_level": bot_mood.get("level"),
                "audit": audit.action,
                "stimulus": audit.counter,
                "unlock_probability": round(audit.probability, 3),
                "traits": {
                    "neuroticism": self.traits.neuroticism,
                    "extraversion": self.traits.extraversion,
                    "agreeableness": self.traits.agreeableness,
                    "resilience": self.traits.resilience,
                },
            },
        }

        recall = self._recall_hint(level)
        if recall:
            payload["recall_hint"] = recall

        # ---------- 6. 记忆写入 + 活跃度（都不影响返回） ----------
        try:
            # 情绪显著（L2 / CRISIS）时给记忆上锁：不参与 TTL / LRU 淘汰
            salience = 1.0 if level in (LEVEL_L2, LEVEL_CRISIS) else 0.0
            await self.memory.store(user_id, text, salience, chat_scope=scope)
        except Exception:
            pass
        self.bump_activity(user_id)

        return payload

    # ---------------------------------------------------------- 生成器

    def _actions(
        self, user_id: str, message: str, level: str, affinity: int
    ) -> list[dict[str, Any]]:
        """按等级挑一句回复，偶尔附带「拍一拍」。"""
        templates = TONE_REPLIES.get(level, TONE_REPLIES[LEVEL_L1])
        content = _pick(user_id + "|" + message, templates)
        actions: list[dict[str, Any]] = [{"type": "text", "content": content}]
        if affinity >= 120 and level in (LEVEL_L0, LEVEL_L1):
            if int(_digest(user_id, message)[:8], 16) % 4 == 0:
                actions.append(
                    {"type": "poke", "target": user_id, "suffix": "戳了戳你的鱼鳍"}
                )
        return actions

    @staticmethod
    def _recall_hint(level: str) -> Optional[dict[str, Any]]:
        """按等级给出记忆回捞建议（阶段一行为，保持不变）。"""
        probability = {
            LEVEL_L0: 0.0,
            LEVEL_L1: 0.15,
            LEVEL_L2: 0.35,
            LEVEL_CRISIS: 0.5,
        }.get(level, 0.0)
        if probability <= 0:
            return None
        return {"probability": probability, "reason": "情绪外露后的羞耻反弹"}
