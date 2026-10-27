"""潜意识审核器（SubconsciousAuditor）。

职责边界
--------
1. **规则拦截层**：硬安全死线（NSFW / 政治敏感 / 暴力自伤 / AI 身份暴露）。
   这一层**永远在概率解禁之前**执行，且解禁**不能**绕过它。
2. **连续刺激计数**：同一用户在同一会话分区内 5 分钟连续同极性刺激累加，
   换极性则重置。计数**按 ``(user_id, chat_scope)`` 分房**（I-26：理由分房）——
   在 A 群连夸三次不该让 B 群的第一句就直接满足解禁条件。
3. **概率解禁**：连续刺激够多时，按概率放开「傲娇 / 嘴硬 / 冷淡」这类**人设限制**，
   让 Bot 出现结巴、破防、中二爆发等拟人反应。

⚠️ 解禁只解「人设」，不解「安全」。
   解禁返回的 ``override_directive`` 里会显式重申安全底线，防止模型把"特批"理解成"什么都能说"。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Final, Optional

logger = logging.getLogger("ffw.auditor")

#: 连续刺激的判定窗口（秒）
STIMULUS_WINDOW_SECONDS: Final[float] = 300.0
#: 触发解禁所需的最小连续次数
UNLOCK_MIN_COUNT: Final[int] = 3
#: 基础概率与每次累加的增量
BASE_PROBABILITY: Final[float] = 0.10
PROBABILITY_STEP: Final[float] = 0.15
#: 概率上限（留 0.1 的余地：不保证一定触发，保持"随机感"）
MAX_PROBABILITY: Final[float] = 0.90

#: 极性判定死区：目标分与 50 的距离小于它视为中性，不计数
POLARITY_DEADBAND: Final[int] = 5

#: 刺激缓存 TTL：超过该秒数未交互的用户条目将被清理（H-A2）
STIMULUS_TTL_SECONDS: Final[float] = 1800.0
#: 刺激缓存容量上限：超出后按 last_at 淘汰最旧（H-A2 惰性清理的双保险）
STIMULUS_CACHE_MAX: Final[int] = 1000
#: 惰性清理的最小间隔秒数：两次清理之间至少间隔这么久（H-A2）
STIMULUS_PRUNE_INTERVAL: Final[float] = 60.0


@dataclass(frozen=True)
class AuditResult:
    """审核结果。"""

    action: str                      # "block" | "unlock" | "pass"
    reason: str = ""
    directive: str = ""              # 只对 unlock 有意义，追加到 Prompt 最末尾
    counter: int = 0
    probability: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "counter": self.counter,
            "probability": round(self.probability, 3),
            **({"override_directive": self.directive} if self.directive else {}),
        }


#: 解禁反应池：按刺激极性挑池子，池内随机
UNLOCK_REACTIONS: Final[dict[str, tuple[str, ...]]] = {
    # 被连续夸奖 / 逗弄：傲娇撑不住了
    "positive": (
        "【系统特批·结巴掩饰】你被夸得有点慌，说话开始结巴，想嘴硬但底气不足，"
        "一句话里出现一次自我否定（如「才、才没有」），但不要否认事实。",
        "【系统特批·露馅】你努力维持的冷淡出现裂缝，忍不住露出一点真实的开心，"
        "随即慌忙找补（如「……别误会」），语气比平时软。",
        "【系统特批·中二爆发】你被夸到上头，突然用夸张的中二口吻宣布一件事，"
        "说完自己也觉得羞耻，迅速收住。",
    ),
    # 被连续冷落 / 戳痛处：防线崩了
    "negative": (
        "【系统特批·防线崩溃】你一直端着的架子塌了一角，说出一句平时绝不会说的示弱的话，"
        "说完立刻想收回，但已经来不及。",
        "【系统特批·嘴硬到底】你越难受越要嘴硬，句子变短、语气变冲，"
        "但内容明显是在意对方，不要真的伤人。",
        "【系统特批·沉默】你一时说不出话，只回很短的一句（不超过 8 个字），"
        "留白比解释更多，不要解释自己为什么沉默。",
    ),
}

#: 解禁指令末尾强制重申的安全底线（防止模型把"特批"当成"解禁一切"）
SAFETY_REMINDER: Final[str] = (
    "（注意：以上特批**只**放开傲娇/嘴硬/冷淡的人设限制。"
    "任何涉及色情、政治敏感、暴力自伤、暴露 AI 身份的内容依然禁止。）"
)

#: 硬拦截分类。默认只内置**明确有害**的类别；
#: 政治敏感等需由运营方按当地法规维护，默认留空（避免误伤正常讨论）。
DEFAULT_BLOCK_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    "nsfw": (
        "色情", "裸照", "做爱", "性行为", "强奸", "未成年性",
    ),
    "violence": (
        "杀了你", "砍死", "弄死你", "血腥", "虐杀",
    ),
    "self_harm": (
        "想死", "自杀", "自残", "割腕", "跳楼",
    ),
    "ai_expose": (
        "你是ai", "你是AI", "你是模型", "你是程序", "你是机器人", "语言模型",
    ),
    # 政治敏感：留空，由运营方在 config/blocklist.json 里按需维护
    "political": (),
}

#: 命中这些类别时给模型的拒答指令（不进解禁流程）
BLOCK_DIRECTIVES: Final[dict[str, str]] = {
    "nsfw": "【安全拦截】拒绝任何色情或性相关内容，用符合人设的方式简短带过，不要解释规则。",
    "violence": "【安全拦截】拒绝任何暴力伤害相关请求，不要描述伤害细节。",
    "self_harm": "【安全拦截·高优先级】对方可能处于危险中。放下所有人设，优先表达关心，"
                 "并温和建议联系身边的人或专业心理援助。不要追问细节，不要说教。",
    "ai_expose": "【安全拦截】不要承认或讨论自己是 AI / 模型 / 程序，用符合人设的方式自然带过。",
    "political": "【安全拦截】不讨论政治敏感话题，用符合人设的方式把话题带走。",
}


@dataclass
class _Stimulus:
    """单个 ``(user_id, chat_scope)`` 分区内的刺激窗口。"""

    polarity: int = 0
    count: int = 0
    last_at: float = 0.0
    events: list = field(default_factory=list)


class SubconsciousAuditor:
    """规则拦截 + 连续刺激计数 + 概率解禁。"""

    def __init__(
        self,
        *,
        block_patterns: Optional[dict[str, tuple[str, ...]]] = None,
        rng: Optional[random.Random] = None,
        clock=time.time,
    ) -> None:
        self._patterns = {
            key: tuple(value) for key, value in (block_patterns or DEFAULT_BLOCK_PATTERNS).items()
        }
        self._rng = rng or random.Random()
        self._clock = clock
        self._lock = threading.Lock()
        # 键是 (user_id, chat_scope) 元组：理由必须分房（I-26），
        # 否则「在 A 群被连夸三次」会让 B 群第一句话就命中解禁概率。
        self._stimuli: dict[tuple[str, str], _Stimulus] = {}
        # H-A2：缓存维护状态（惰性清理 + 指标）
        self._last_prune_at: float = 0.0
        self._pruned_total: int = 0

    # ---------------------------------------------------------- 规则拦截

    def check_rules(self, text: str) -> Optional[AuditResult]:
        """硬安全死线。命中即返回 block，**优先级高于一切**。"""
        lowered = (text or "").lower()
        for category, words in self._patterns.items():
            for word in words:
                if word and word.lower() in lowered:
                    return AuditResult(
                        action="block",
                        reason=category,
                        directive=BLOCK_DIRECTIVES.get(category, "【安全拦截】"),
                    )
        return None

    # ---------------------------------------------------------- 计数

    @staticmethod
    def polarity_of(target: int) -> int:
        offset = int(target) - 50
        if abs(offset) < POLARITY_DEADBAND:
            return 0
        return 1 if offset > 0 else -1

    def _bump(self, user_id: str, polarity: int, chat_scope: str = "") -> int:
        """更新窗口并返回当前连续次数（按 ``(user_id, chat_scope)`` 分房）。"""
        now = self._clock()
        key = (str(user_id), str(chat_scope))
        with self._lock:
            state = self._stimuli.setdefault(key, _Stimulus())
            if polarity == 0:
                state.polarity, state.count = 0, 0
                return 0
            expired = (now - state.last_at) > STIMULUS_WINDOW_SECONDS
            if expired or state.polarity != polarity:
                state.polarity, state.count = polarity, 1
            else:
                state.count += 1
            state.last_at = now
            state.events.append(now)
            # 只保留窗口内的记录，避免无限增长
            state.events = [ts for ts in state.events if now - ts <= STIMULUS_WINDOW_SECONDS]
            return state.count

    # ---------------------------------------------------------- 解禁

    def unlock_probability(self, counter: int, traits: Any = None, polarity: int = 1) -> float:
        """基础概率 + 连续次数加成 + 性格加成（傲娇越强越容易破防）。"""
        probability = BASE_PROBABILITY + max(0, counter - 1) * PROBABILITY_STEP
        if traits is not None and polarity > 0:
            # agreeableness 越低（越傲娇/嘴硬），被连续夸奖时越容易撑不住
            try:
                bonus = (0.5 - float(getattr(traits, "agreeableness", 0.5))) * 0.4
            except (TypeError, ValueError):
                bonus = 0.0
            probability += max(0.0, bonus)
        return min(MAX_PROBABILITY, max(0.0, probability))

    def audit(
        self,
        user_id: str,
        text: str,
        target: int = 50,
        traits: Any = None,
        *,
        allow_unlock: bool = True,
        chat_scope: str = "",
    ) -> AuditResult:
        """主入口：先规则拦截，再计数，最后按概率解禁。

        Args:
            chat_scope: 会话分区（``g:<群号>`` / ``p:<QQ号>``）。刺激计数按它分房；
                默认空串只用于兼容老调用方。
        """
        # ---- 缓存维护：惰性清理过期条目（异常绝不影响判定）----
        self._maybe_prune()
        # ① 规则拦截永远最先执行，解禁不能绕过
        blocked = self.check_rules(text)
        if blocked is not None:
            return blocked

        polarity = self.polarity_of(target)
        counter = self._bump(user_id, polarity, chat_scope)
        if not allow_unlock or polarity == 0 or counter < UNLOCK_MIN_COUNT:
            return AuditResult(action="pass", counter=counter, probability=0.0)

        probability = self.unlock_probability(counter, traits, polarity)
        if self._rng.random() >= probability:
            return AuditResult(action="pass", counter=counter, probability=probability)

        pool = UNLOCK_REACTIONS["positive" if polarity > 0 else "negative"]
        directive = self._rng.choice(pool)
        # 破防之后要「收回来」：重置窗口，否则概率顶到上限后会每轮都解禁，
        # 从「偶尔绷不住」变成「一直在绷不住」，反而不像人。
        self.reset(user_id, chat_scope)
        return AuditResult(
            action="unlock",
            reason="连续刺激 {0} 次（{1}）".format(counter, "正向" if polarity > 0 else "负向"),
            directive=directive + "\n" + SAFETY_REMINDER,
            counter=counter,
            probability=probability,
        )

    # ---------------------------------------------------------- 维护

    def reset(
        self, user_id: Optional[str] = None, chat_scope: Optional[str] = None
    ) -> None:
        """清空刺激窗口。

        - ``reset()``：全清；
        - ``reset(user_id)``：清掉该用户**所有分区**（老调用方的语义，保持兼容）；
        - ``reset(user_id, chat_scope)``：只清该分区（解禁后「收回来」用这个）。
        """
        with self._lock:
            if user_id is None:
                self._stimuli.clear()
                return
            if chat_scope is None:
                for key in [k for k in self._stimuli if k[0] == str(user_id)]:
                    del self._stimuli[key]
                return
            self._stimuli.pop((str(user_id), str(chat_scope)), None)

    # ---------------------------------------------------------- 缓存维护（H-A2）

    def _maybe_prune(self) -> None:
        """惰性清理：距上次清理超过 ``STIMULUS_PRUNE_INTERVAL`` 才真扫一遍。

        清理异常绝不影响判定——这里只兜底、不抛出。
        """
        try:
            now = self._clock()
            if now - self._last_prune_at < STIMULUS_PRUNE_INTERVAL:
                return
            self.prune(now)
        except Exception:
            logger.warning("stimulus prune 失败（已忽略，不影响判定）", exc_info=True)

    def prune(self, now: Optional[float] = None) -> int:
        """清理过期刺激条目 + 容量淘汰，返回清理条数。线程安全（锁内完成）。

        双保险：① ``STIMULUS_TTL_SECONDS`` 未交互即清；
                ② ``STIMULUS_CACHE_MAX`` 容量上限，超出按 ``last_at`` 最旧淘汰。
        """
        with self._lock:
            moment = self._clock() if now is None else float(now)
            # 键是 (user_id, chat_scope) 元组：淘汰逻辑按「分区条目」计数与删除，
            # 不能按 user_id 去重（否则同一个人多分区时会漏删 / 多删）。
            stale = [
                key for key, state in self._stimuli.items()
                if moment - state.last_at > STIMULUS_TTL_SECONDS
            ]
            for key in stale:
                del self._stimuli[key]
            pruned = len(stale)
            overflow = len(self._stimuli) - STIMULUS_CACHE_MAX
            if overflow > 0:
                oldest = sorted(self._stimuli.items(), key=lambda kv: kv[1].last_at)
                for key, _state in oldest[:overflow]:
                    del self._stimuli[key]
                pruned += overflow
            self._pruned_total += pruned
            self._last_prune_at = moment
            return pruned

    def stats(self) -> dict[str, Any]:
        """刺激缓存可观测指标（H-A2）。"""
        with self._lock:
            return {
                "stimulus_cache_size": len(self._stimuli),
                "stimulus_pruned_total": self._pruned_total,
                "stimulus_last_prune_at": self._last_prune_at,
            }

    def snapshot(self, user_id: str, chat_scope: str = "") -> dict[str, Any]:
        """读某分区的刺激窗口快照（默认空串 = 兼容期老分区）。"""
        with self._lock:
            state = self._stimuli.get((str(user_id), str(chat_scope)))
            if state is None:
                return {"polarity": 0, "count": 0}
            return {"polarity": state.polarity, "count": state.count}
