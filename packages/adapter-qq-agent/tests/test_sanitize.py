"""CQ 码清洗单元测试。

跑法（仓库根目录）：
    venv/Scripts/python.exe -m pytest packages/adapter-qq-agent/tests -q
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER = os.path.dirname(_HERE)
if _ADAPTER not in sys.path:
    sys.path.insert(0, _ADAPTER)

from fatfish_adapter import clean_cq  # noqa: E402


def test_at码被剥离且保留正文():
    assert clean_cq("[CQ:at,qq=12345] 你在干嘛") == "你在干嘛"


def test_多个CQ码连续出现():
    raw = "[CQ:at,qq=1][CQ:at,qq=2]  说句话"
    assert clean_cq(raw) == "说句话"


def test_纯表情消息清洗后为空():
    """纯 face 没有语义价值，不能变成任何正文去骗 Track A。"""
    assert clean_cq("[CQ:face,id=178]") == ""


def test_纯at消息清洗后为空():
    assert clean_cq("[CQ:at,qq=999]") == ""


def test_纯图片保留占位标记():
    """发了张图本身是信号，但不能把 file=xxx.jpg 喂给模型。"""
    cleaned = clean_cq("[CQ:image,file=abc.jpg,url=https://x/y.jpg]")
    assert cleaned == "[图片]"
    assert "abc.jpg" not in cleaned


def test_图片加文字保留文字():
    cleaned = clean_cq("[CQ:image,file=abc.jpg] 你看这个")
    assert cleaned == "你看这个"


def test_语音与视频标记():
    assert clean_cq("[CQ:record,file=a.amr]") == "[语音]"
    assert clean_cq("[CQ:video,file=a.mp4]") == "[视频]"


def test_HTML实体转义也能识别():
    """部分 OneBot 实现会把方括号转义，漏处理就会残留 CQ 码。"""
    assert clean_cq("&#91;CQ:at,qq=1&#93;在吗") == "在吗"


def test_普通文本原样返回():
    text = "今天好累啊"
    assert clean_cq(text) == text


def test_空值安全():
    assert clean_cq(None) == ""
    assert clean_cq("") == ""


def test_多行消息保留换行():
    assert clean_cq("[CQ:at,qq=1] 第一行\n第二行") == "第一行\n第二行"


def test_方括号正文不被误伤():
    """不是 CQ 码的方括号必须原样保留。"""
    assert clean_cq("[公告] 明天维护") == "[公告] 明天维护"


def test_非字符串输入被安全转换():
    assert clean_cq(12345) == "12345"
