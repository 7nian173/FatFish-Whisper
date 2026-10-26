"""性格向量（Persona Traits）。

设计原则
--------
**只提取一次，之后只读本地文件。** 每次对话都去问 LLM「她是什么性格」既贵又慢，
还会让判定结果在轮次之间漂移。所以：

1. 启动时 ``load_traits()`` 读 ``config/persona_traits.json``；
2. 文件不存在才做一次兜底推断（关键词启发式，零成本）；
3. 想用 LLM 精提，跑一次 ``python persona_traits.py``，把结果写进同一个文件；
4. 之后所有模块（情绪更新、解禁概率）只读缓存，**不再触网**。

四个维度均为 0.0 ~ 1.0，0.5 表示「没特别偏向」：

- ``neuroticism``   神经质/敏感度：高则负面情绪被放大
- ``extraversion``  外向性：高则正向表达更积极
- ``agreeableness`` 宜人性/柔软度：高则容易原谅；**低 = 傲娇/嘴硬**
- ``resilience``    韧性：高则负面情绪衰减越快
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Final, Optional

logger = logging.getLogger("ffw.persona")

SRC_DIR: Final[str] = os.path.dirname(os.path.abspath(__file__))
CORE_DIR: Final[str] = os.path.dirname(SRC_DIR)
DEFAULT_TRAITS_PATH: Final[str] = os.path.join(CORE_DIR, "config", "persona_traits.json")
DEFAULT_PERSONA_PATH: Final[str] = os.path.join(CORE_DIR, "persona.txt")

DIMENSIONS: Final[tuple[str, ...]] = (
    "neuroticism", "extraversion", "agreeableness", "resilience",
)

#: 关键词启发式：命中即加分（正负都有）。只在没有 LLM 结果时兜底。
_KEYWORDS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "neuroticism": (
        ("敏感", "多虑", "玻璃心", "易怒", "情绪化", "记仇", "患得患失", "内耗"),
        ("钝感", "大条", "没心没肺", "佛系"),
    ),
    "extraversion": (
        ("活泼", "话多", "开朗", "主动", "热情", "外向", "元气", "爱闹"),
        ("安静", "寡言", "冷淡", "内向", "社恐", "话少"),
    ),
    "agreeableness": (
        ("温柔", "体贴", "好说话", "迁就", "包容", "心软", "顺从", "善解人意"),
        ("傲娇", "嘴硬", "毒舌", "强势", "别扭", "刀子嘴", "不好惹"),
    ),
    "resilience": (
        ("坚强", "韧性", "抗压", "豁达", "乐观", "自愈", "想得开"),
        ("脆弱", "一碰就碎", "走不出来", "钻牛角尖"),
    ),
}


@dataclass(frozen=True)
class PersonaTraits:
    """性格向量（不可变，便于当默认值共享）。"""

    neuroticism: float = 0.5
    extraversion: float = 0.5
    agreeableness: float = 0.5
    resilience: float = 0.5
    source: str = "default"

    def clamped(self) -> "PersonaTraits":
        return PersonaTraits(
            **{dim: min(1.0, max(0.0, float(getattr(self, dim)))) for dim in DIMENSIONS},
            source=self.source,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp01(value: Any, fallback: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(1.0, max(0.0, number))


def heuristic_extract(persona_text: str) -> PersonaTraits:
    """关键词兜底：不联网、零成本，够用就行。"""
    text = persona_text or ""
    values: dict[str, float] = {}
    for dim, (positive, negative) in _KEYWORDS.items():
        score = 0.4
        score += 0.12 * sum(1 for word in positive if word in text)
        score -= 0.12 * sum(1 for word in negative if word in text)
        values[dim] = round(min(1.0, max(0.0, score)), 2)
    return PersonaTraits(**values, source="heuristic")


def load_traits(
    traits_path: Optional[str] = None,
    persona_path: Optional[str] = None,
    *,
    write_cache: bool = True,
) -> PersonaTraits:
    """读性格向量：优先 JSON 缓存，缺失时用启发式推断并写回。

    这是**唯一**会被业务代码调用的入口，内部只做文件 IO，绝不触网。
    """
    path = traits_path or os.environ.get("FFW_TRAITS_PATH") or DEFAULT_TRAITS_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            traits = PersonaTraits(
                **{dim: _clamp01(raw.get(dim)) for dim in DIMENSIONS},
                source=str(raw.get("source") or "file"),
            )
            return traits.clamped()
        except Exception:
            logger.warning("persona_traits.json 读取失败，回落到启发式推断", exc_info=True)

    persona_file = persona_path or DEFAULT_PERSONA_PATH
    text = ""
    if os.path.exists(persona_file):
        try:
            with open(persona_file, "r", encoding="utf-8") as handle:
                text = handle.read()
        except Exception:
            logger.warning("persona.txt 读取失败，使用默认性格", exc_info=True)

    traits = heuristic_extract(text) if text else PersonaTraits(source="default")
    if write_cache:
        save_traits(traits, path)
    return traits


def save_traits(traits: PersonaTraits, path: Optional[str] = None) -> str:
    target = path or DEFAULT_TRAITS_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(traits.as_dict(), handle, ensure_ascii=False, indent=2)
    return target


async def extract_with_llm(
    persona_text: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 12.0,
) -> Optional[PersonaTraits]:
    """用 LLM 精提一次（只在手动跑 CLI 时调用，不参与对话主链路）。"""
    if not (base_url and api_key and model):
        return None
    import httpx

    prompt = (
        "阅读下面的人设文本，输出严格 JSON（不要解释）：\n"
        '{"neuroticism":0.0-1.0,"extraversion":0.0-1.0,'
        '"agreeableness":0.0-1.0,"resilience":0.0-1.0}\n'
        "neuroticism=敏感/易被负面情绪影响；extraversion=外向/话多；"
        "agreeableness=柔软/易原谅（低=傲娇嘴硬）；resilience=抗压/恢复快。\n\n"
        "人设文本：\n" + (persona_text or "")[:4000]
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 120,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        raw = json.loads(match.group(0))
        return PersonaTraits(
            **{dim: _clamp01(raw.get(dim)) for dim in DIMENSIONS}, source="llm"
        ).clamped()
    except Exception:
        logger.warning("LLM 性格提取失败，保留原值", exc_info=True)
        return None


if __name__ == "__main__":  # pragma: no cover - 一次性 CLI
    import asyncio

    persona = DEFAULT_PERSONA_PATH
    if os.path.exists(persona):
        with open(persona, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = ""

    extracted = asyncio.run(
        extract_with_llm(
            text,
            base_url=os.environ.get("LLM_API_BASE", ""),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", ""),
        )
    )
    final = extracted or heuristic_extract(text)
    target = save_traits(final)
    print("已写入 {0}: {1}".format(target, json.dumps(final.as_dict(), ensure_ascii=False)))
