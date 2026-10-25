"""FatFish-Whisper 情绪判定引擎。

设计要点
--------
- 双轨判定：Track A 是纯本地的词典/规则快判（毫秒级、零成本）；
             Track B 才调用外部 LLM，且只在"高不确定性 / 语义冲突 / 长文本"时触发。
- 平滑更新：delta = clamp(0.2 * (target - E_prev), -5, +5)，防止情绪跳变。
- 持久化：SQLAlchemy + SQLite（app.db）。单机场景没有 Redis / Docker。
- 防抖：同一用户窗口内的连续消息用 asyncio 队列合并（可选，adapter 自己做防抖时关掉）。
- 缓存：diskcache 缓存 Track B 判定结果，省 token。
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx
from diskcache import Cache
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from models.db_models import Base, TokenLog, UserState

# ---------------------------------------------------------------- 路径与常量

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(CORE_DIR, "app.db")
CACHE_DIR = os.path.join(CORE_DIR, ".cache")

POSITIVE_WORDS = (
    "开心", "喜欢", "谢谢", "可爱", "好看", "厉害", "棒", "哈哈", "爱你", "想你",
    "抱抱", "好耶", "舒服", "放心", "温柔", "陪我",
)
NEGATIVE_WORDS = (
    "难过", "伤心", "累", "烦", "讨厌", "生气", "滚", "无聊", "崩溃", "委屈",
    "痛苦", "孤独", "失眠", "害怕", "焦虑", "压力", "敷衍", "冷淡", "算了",
)
CONFLICT_WORDS = ("但是", "其实", "不过", "可是", "然而", "只是")
NEGATION_WORDS = ("不", "没", "别", "无", "非")

LEVEL_ORDER = ("CRISIS", "L2", "L1", "L0")

INNER_OS_TEMPLATES = {
    "L0": (
        "（今天的他有点乖，我心里咕嘟咕嘟冒泡。）",
        "（明明没说什么，我却偷偷高兴了一下。）",
        "（被在意了一点点，尾巴尖都在晃。）",
    ),
    "L1": (
        "（他这话有点扎人，可我还是想再听两句。）",
        "（心里泛起点小波浪，先记下来。）",
        "（有点在意，但我不想显得太在意。）",
    ),
    "L2": (
        "（有点凉。我把话咽回去了，不想让他看出来。）",
        "（算了，先躲进壳里缓一缓。）",
        "（装作没事是我最熟练的技能。）",
    ),
    "CRISIS": (
        "（很沉。我现在只想安安静静沉到水底。）",
        "（别问了。再问一句我就要哭了。）",
        "（把灯关掉吧，今天不想被看见。）",
    ),
}

REPLY_TABLE = {
    "L0": ("呜——本鲸知道了。", "嗯哼，今天也好好在听。", "收到收到，我记下了。"),
    "L1": ("……嗯。你说的我都听见了。", "有点在意，不过我先不吵你。", "好吧，那就这样。"),
    "L2": ("嗯，我在。", "……我知道了。", "先让我安静一会儿。"),
    "CRISIS": ("……嗯，我在听。", "别急，慢慢说。", "我在这儿，不催你。"),
}

SYSTEM_PROMPT = """你是"潜鱼潜语"，一个 Bot 的潜意识情绪判定模块。
你只输出严格 JSON，不要任何解释文字，字段如下：
{"score": 0-100 的整数, "reason": "一句话说明依据", "inner_os": "第一人称的内心独白，用中文括号包裹，30 字以内"}
score 含义：0=极度负面/崩溃边缘，50=平静，100=非常愉悦。
注意反话、克制、口是心非：例如"我没事"往往意味着 score 偏低。"""


def clamp(value: float, low: float, high: float) -> float:
    """把 value 夹在 [low, high]。"""
    return max(low, min(high, value))


def level_of(score: int) -> str:
    """情绪分 -> 等级。"""
    if score <= 20:
        return "CRISIS"
    if score <= 40:
        return "L2"
    if score <= 65:
        return "L1"
    return "L0"


@dataclass
class Verdict:
    """一次判定的结果。"""

    score: int
    confidence: float
    track: str                      # "A" 或 "B"
    reason: str
    inner_os: Optional[str] = None
    tokens: tuple[int, int] = (0, 0)


# ---------------------------------------------------------------- Track A

class TrackA:
    """规则轨：词典命中 + 冲突标记 + 否定翻转 + 强度信号。"""

    def evaluate(self, text: str) -> Verdict:
        text = text or ""
        hits_pos = [w for w in POSITIVE_WORDS if w in text]
        hits_neg = [w for w in NEGATIVE_WORDS if w in text]
        conflict = [w for w in CONFLICT_WORDS if w in text]

        score = 50.0
        score += 7.0 * len(hits_pos)
        score -= 8.0 * len(hits_neg)

        # "其实/不过/可是"这类转折，往往藏着没说出口的情绪
        if conflict:
            score -= 3.0 * len(conflict)

        # 否定翻转："不开心"命中"开心"，但前面带了否定词
        for word in hits_pos:
            if self._negated(text, word):
                score -= 12.0
        for word in hits_neg:
            if self._negated(text, word):
                score += 8.0

        # 强度信号
        score -= 2.5 * min(text.count("！") + text.count("!"), 3)
        score += 2.0 * min(text.count("哈哈"), 3)
        if re.search(r"(.)\1{2,}", text):
            score -= 2.0
        if len(text) > 120:
            score -= 3.0

        score = clamp(score, 0, 100)

        signals = len(hits_pos) + len(hits_neg) + len(conflict)
        confidence = clamp(0.30 + 0.18 * signals, 0.0, 0.95)
        if not signals and len(text) < 8:
            confidence = 0.25

        reason = "命中: " + ", ".join(hits_pos + hits_neg + conflict) if signals else "无明显信号"
        return Verdict(score=int(round(score)), confidence=confidence, track="A", reason=reason)

    @staticmethod
    def _negated(text: str, word: str) -> bool:
        idx = text.find(word)
        if idx <= 0:
            return False
        window = text[max(0, idx - 3):idx]
        return any(neg in window for neg in NEGATION_WORDS)


# ---------------------------------------------------------------- Track B

class TrackB:
    """LLM 轨：只在必要时调用，结果进 diskcache。"""

    def __init__(self, cache: Cache, base_url: str, api_key: str, model: str, timeout: float = 8.0):
        self.cache = cache
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model) and not self.api_key.startswith("sk-xxx")

    async def evaluate(self, text: str, emotion_prev: int, affinity: int,
                       memory_text: str = "") -> Optional[Verdict]:
        if not self.enabled:
            return None

        cache_key = "tb:" + hashlib.sha256(
            (self.model + "|" + text + "|" + str(emotion_prev)).encode("utf-8")
        ).hexdigest()
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                return Verdict(**cached)
            except TypeError:
                pass

        user_prompt = (
            "当前状态：情绪值 " + str(emotion_prev) + "/100，好感度 " + str(affinity) + "。\n"
            + ("最近记忆：\n" + memory_text + "\n" if memory_text else "")
            + "用户刚才说：" + text + "\n请判定他此刻的情绪分数。"
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.base_url + "/chat/completions", json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, OSError):
            return None

        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None

        parsed = self._parse_json(content)
        if parsed is None:
            return None

        try:
            score = int(round(float(parsed.get("score", 50))))
        except (TypeError, ValueError):
            return None

        usage = data.get("usage") or {}
        verdict = Verdict(
            score=int(clamp(score, 0, 100)),
            confidence=0.9,
            track="B",
            reason=str(parsed.get("reason", "LLM 判定")),
            inner_os=parsed.get("inner_os") or None,
            tokens=(int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)),
        )
        self.cache.set(cache_key, asdict(verdict), expire=3600)
        return verdict

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        text = (text or "").strip()
        text = re.sub(r"^\x60\x60\x60[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*\x60\x60\x60$", "", text)
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                try:
                    value = json.loads(match.group(0))
                    return value if isinstance(value, dict) else None
                except json.JSONDecodeError:
                    return None
        return None


# ---------------------------------------------------------------- 防抖

class Debouncer:
    """把同一用户在一个窗口内的连续消息合并成一条文本。"""

    def __init__(self, window: float = 1.2):
        self.window = window
        self._buffers: dict[str, list[str]] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def merge(self, key: str, message: str) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
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
        merged = "\n".join(texts)
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(merged)


# ---------------------------------------------------------------- 引擎

class EmotionEngine:
    """情绪机主干：读状态 -> 双轨判定 -> 平滑更新 -> 生成内心 OS 与动作。"""

    def __init__(self, db_path: str = DB_PATH, cache_dir: str = CACHE_DIR,
                 env: Optional[dict[str, str]] = None):
        self.env = dict(env or os.environ)
        os.makedirs(cache_dir, exist_ok=True)
        self.cache = Cache(cache_dir)
        self.db_engine = create_engine(
            "sqlite:///" + db_path,
            future=True,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.db_engine)
        self.Session = sessionmaker(bind=self.db_engine, expire_on_commit=False, future=True)

        self.track_a = TrackA()
        self.track_b = self._build_track_b()
        self.debouncer = Debouncer(float(self.env.get("DEBOUNCE_SECONDS", "1.2") or 1.2))
        self.whitelist = {
            item.strip() for item in self.env.get("ADMIN_WHITELIST", "").split(",") if item.strip()
        }
        self._locks: dict[str, asyncio.Lock] = {}

        # 延迟导入避免循环依赖
        from memory import MemoryRouter

        self.memory = MemoryRouter(cache_dir)

    # ---------------------------------------------------------- 配置

    def _build_track_b(self) -> TrackB:
        return TrackB(
            cache=self.cache,
            base_url=self.env.get("LLM_API_BASE", ""),
            api_key=self.env.get("LLM_API_KEY", ""),
            model=self.env.get("LLM_MODEL", ""),
            timeout=float(self.env.get("LLM_TIMEOUT", "8") or 8),
        )

    def reload_config(self) -> None:
        """后台改完 .env 后热更新 Track B 配置。"""
        self.track_b = self._build_track_b()
        self.whitelist = {
            item.strip() for item in self.env.get("ADMIN_WHITELIST", "").split(",") if item.strip()
        }

    # ---------------------------------------------------------- 状态读写

    def _get_or_create(self, user_id: str) -> dict[str, Any]:
        with self.Session() as session:
            row = session.get(UserState, user_id)
            if row is None:
                row = UserState(user_id=user_id, affinity_raw=50, emotion_current=50, emotion_level="L0")
                session.add(row)
                session.commit()
            return {
                "emotion": int(row.emotion_current),
                "affinity": int(row.affinity_raw),
                "level": row.emotion_level,
            }

    def _save_state(self, user_id: str, emotion: int, affinity: int, level: str) -> None:
        with self.Session() as session:
            row = session.get(UserState, user_id)
            if row is None:
                row = UserState(user_id=user_id)
                session.add(row)
            row.emotion_current = emotion
            row.affinity_raw = affinity
            row.emotion_level = level
            row.last_update = datetime.datetime.utcnow()
            session.commit()

    def _log_tokens(self, user_id: str, tokens: tuple[int, int]) -> None:
        prompt_tokens, completion_tokens = tokens
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        with self.Session() as session:
            session.add(TokenLog(
                user_id=user_id,
                module_type="MOD_TRACK_B",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ))
            session.commit()

    def list_states(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as session:
            rows = session.execute(
                select(UserState).order_by(UserState.last_update.desc()).limit(limit)
            ).scalars().all()
        return [
            {
                "user_id": row.user_id,
                "emotion": int(row.emotion_current),
                "affinity": int(row.affinity_raw),
                "level": row.emotion_level,
                "last_update": row.last_update.isoformat() if row.last_update else None,
            }
            for row in rows
        ]

    # ---------------------------------------------------------- 主流程

    async def handle_message(self, user_id: str, message: str, debounce: bool = False) -> dict[str, Any]:
        if debounce and self.debouncer.window > 0:
            message = await self.debouncer.merge(user_id, message)
        return await self.process(user_id, message)

    async def process(self, user_id: str, message: str) -> dict[str, Any]:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            state = self._get_or_create(user_id)
            prev = state["emotion"]
            affinity = state["affinity"]

            verdict = self.track_a.evaluate(message)
            path = "fast"
            threshold = float(self.env.get("TRACK_B_CONFIDENCE", "0.55") or 0.55)

            if (verdict.confidence < threshold or len(message) > 120) and self.track_b.enabled:
                deep = await self.track_b.evaluate(message, prev, affinity, self.memory.as_text(user_id))
                if deep is not None:
                    verdict = deep
                    path = "deep"
                    self._log_tokens(user_id, deep.tokens)

            delta = clamp(0.2 * (verdict.score - prev), -5.0, 5.0)
            emotion = int(round(clamp(prev + delta, 0, 100)))

            level = level_of(emotion)
            if user_id in self.whitelist and level == "CRISIS":
                level = "L2"  # 白名单用户不进入危机压制

            affinity_delta = 0.8 if emotion >= 65 else (-0.6 if emotion <= 40 else 0.1)
            new_affinity = int(round(clamp(affinity + affinity_delta, 0, 200)))

            inner_os = verdict.inner_os or self._inner_os(user_id, emotion, new_affinity, level)
            actions = self._actions(user_id, message, level, new_affinity, inner_os)

            payload: dict[str, Any] = {
                "actions": actions,
                "inner_os": inner_os,
                "debug": {
                    "emotion": emotion,
                    "prev": prev,
                    "delta": round(delta, 2),
                    "affinity": new_affinity,
                    "level": level,
                    "path": path,
                    "track": verdict.track,
                    "reason": verdict.reason,
                    "confidence": round(verdict.confidence, 2),
                },
            }
            recall = self._recall_hint(level)
            if recall:
                payload["recall_hint"] = recall

            self._save_state(user_id, emotion, new_affinity, level)
            self.memory.remember(user_id, message, actions[0]["content"] if actions else "")
            return payload

    # ---------------------------------------------------------- 生成器

    @staticmethod
    def _pick(seed: str, items: tuple[str, ...]) -> str:
        idx = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(items)
        return items[idx]

    def _inner_os(self, user_id: str, emotion: int, affinity: int, level: str) -> str:
        seed = user_id + "|" + str(emotion) + "|" + str(int(time.time() // 300))
        return self._pick(seed, INNER_OS_TEMPLATES.get(level, INNER_OS_TEMPLATES["L0"]))

    def _actions(self, user_id: str, message: str, level: str, affinity: int,
                 inner_os: str) -> list[dict[str, Any]]:
        content = self._pick(user_id + "|" + message, REPLY_TABLE.get(level, REPLY_TABLE["L0"]))
        actions: list[dict[str, Any]] = [{"type": "text", "content": content}]

        # 高好感 + 非低落时，偶尔撒个娇（拍一拍）
        if affinity >= 120 and level in ("L0", "L1"):
            if int(hashlib.sha256((user_id + message).encode("utf-8")).hexdigest(), 16) % 4 == 0:
                actions.append({"type": "poke", "target": user_id, "suffix": "戳了戳你的鱼鳍"})
        return actions

    @staticmethod
    def _recall_hint(level: str) -> Optional[dict[str, Any]]:
        probability = {"L0": 0.0, "L1": 0.15, "L2": 0.35, "CRISIS": 0.5}.get(level, 0.0)
        if probability <= 0:
            return None
        return {"probability": probability, "reason": "情绪外露后的羞耻反弹"}
