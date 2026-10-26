"""提示词注入面的清洗工具。

为什么需要它
------------
记忆条目会被拼进最终 Prompt。如果一条记忆里带有 ``---`` 或 ``[用户消息]``，
就能伪造段边界、抢占「用户真实消息」的位置，形成**持久化 prompt injection**
（记忆有 TTL，注入会反复生效）。

这里做三件事：
1. 去掉控制字符、压平换行（记忆是「一条事实」，不该是多行结构）；
2. 把段标记方括号换成全角，使其不再被当成结构标记；
3. 把连续破折号替换掉，避免伪造 ``---`` 分隔线；
4. 截断到上限，避免单条记忆撑爆 Prompt。
"""
from __future__ import annotations

import re
from typing import Final

#: 单条记忆的最大字符数（超出截断）
MAX_MEMORY_CHARS: Final[int] = 300

#: 整个记忆块的最大字符数
MAX_MEMORY_BLOCK_CHARS: Final[int] = 1200

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: 会被当成「段标记」的方括号写法（中英文、大小写都覆盖）
_MARKER = re.compile(
    r"[\[【]\s*(用户消息|相关回忆|你的深层潜意识|系统提示|system|user|assistant|developer)\s*[\]】]",
    re.IGNORECASE,
)
#: 连续 3 个及以上的短横线（含全角）——用来伪造分隔线
_FENCE = re.compile(r"[-—–_]{3,}")
_SPACES = re.compile(r"\s+")


def clean_memory_item(text: object, limit: int = MAX_MEMORY_CHARS) -> str:
    """把一条记忆清洗成「单行、无结构标记、长度受限」的纯文本。"""
    value = "" if text is None else str(text)
    value = _CONTROL.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    # 段标记降级为全角括号：保留可读性，但不再是结构标记
    value = _MARKER.sub(lambda m: "［" + m.group(1) + "］", value)
    value = _FENCE.sub("—", value)
    value = _SPACES.sub(" ", value).strip()
    if limit > 0 and len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def clean_memory_items(items: object, limit: int = MAX_MEMORY_CHARS) -> list[str]:
    """批量清洗并丢弃空条目。"""
    if not items:
        return []
    out: list[str] = []
    for item in list(items):
        cleaned = clean_memory_item(item, limit=limit)
        if cleaned:
            out.append(cleaned)
    return out
