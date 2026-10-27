"""CQ 码清洗：把宿主平台的原生富文本标记剥离成纯文本。

为什么必须清洗
--------------
OneBot / NapCat 的 ``raw_message`` 里混着 ``[CQ:at,qq=123]`` / ``[CQ:face,id=1]``
这类标记。它们会带来两个真实伤害：

1. **污染 Track A 词典**：``[CQ:`` 里的字母会被当成正文参与匹配，置信度虚高；
2. **污染 Track B prompt**：模型会把 ``qq=123`` 当成用户说的话，判定漂移。

处理策略
--------
- 先把 OneBot 的 HTML 实体（``&#91;`` 等）还原，否则形如
  ``&#91;CQ:at,qq=1&#93;`` 的转义码会漏网；
- 再剥离所有 ``[CQ:...]``；
- 纯媒体消息（只有图片/语音）剥离后会变空串，此时保留一个占位标记
  （``[图片]`` / ``[语音]`` / ``[视频]`` / ``[文件]``），因为"发了张图"本身
  是有效信号，但绝不能把 ``file=xxx.jpg`` 这种参数喂给模型；
- 最后压缩空白。
"""
from __future__ import annotations

import re
from typing import Optional

#: CQ 码整体匹配。不匹配 ``]``，避免跨段贪婪吞掉后面的正文。
_CQ_PATTERN = re.compile(r"\[CQ:[^\]]*\]")

#: OneBot 在部分实现里会把 ``[`` ``]`` ``,`` ``&`` 转义成 HTML 实体
_HTML_ESCAPES = (
    ("&#91;", "["),
    ("&#93;", "]"),
    ("&#44;", ","),
    ("&amp;", "&"),
)

#: 纯媒体消息的占位标记（只取第一个命中的类型）
_MEDIA_MARKERS = (
    ("image", "[图片]"),
    ("record", "[语音]"),
    ("video", "[视频]"),
    ("file", "[文件]"),
)

_CQ_TYPE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)")

_WHITESPACE = re.compile(r"[ \t\u3000]+")


def _unescape(text: str) -> str:
    """还原 OneBot 的 HTML 实体转义。"""
    for entity, raw in _HTML_ESCAPES:
        if entity in text:
            text = text.replace(entity, raw)
    return text


def _media_marker(text: str) -> str:
    """从原始文本里找出第一个媒体类型，返回其占位标记。"""
    for match in _CQ_TYPE.finditer(text):
        kind = match.group(1).lower()
        for name, marker in _MEDIA_MARKERS:
            if kind == name:
                return marker
    return ""


def clean_cq(text: Optional[object], *, keep_media_marker: bool = True) -> str:
    """剥离 CQ 码，返回纯文本。

    Args:
        text: 原始消息（任何类型，非字符串会被 ``str()``）。
        keep_media_marker: 纯媒体消息是否保留 ``[图片]`` 这类占位标记。
            关掉后纯媒体消息会返回空串（调用方据此跳过判定）。

    Returns:
        清洗后的纯文本。纯 CQ 码消息可能返回空串。
    """
    raw = "" if text is None else str(text)
    if not raw:
        return ""

    unescaped = _unescape(raw)
    marker = _media_marker(unescaped) if keep_media_marker else ""
    stripped = _CQ_PATTERN.sub(" ", unescaped)

    # 逐行清理：行内压缩空格，行首尾去空白，丢掉空行
    lines = []
    for line in stripped.splitlines():
        line = _WHITESPACE.sub(" ", line).strip()
        if line:
            lines.append(line)
    cleaned = "\n".join(lines).strip()

    if cleaned:
        return cleaned
    # 整条消息只有 CQ 码：只有媒体才值得留个标记，@ / 表情一律视为无内容
    return marker


def has_content(text: Optional[object]) -> bool:
    """清洗后是否还有可判定内容（供调用方决定要不要请求 Core）。"""
    return bool(clean_cq(text))
