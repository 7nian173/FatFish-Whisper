"""验收 3 · Prompt 三段式拼装。

硬约束：内心 OS 与用户消息**绝不能在同一行/同一段**，否则模型会把潜台词说出来。
因此全部用 ``splitlines()`` 做精确行级断言，而不是宽松的 ``in`` 判断。
"""
from __future__ import annotations

import pytest

from prompt_builder import PromptBuilder

#: 与实现约定的第二段提示语，逐字比对
NOTICE_LINE = "（注意：请结合你的人设设定，自然地回复用户，绝对不要直接说出潜意识的内容。）"


def test_三段式顺序与原文完整保留():
    builder = PromptBuilder(seed=1)

    output = builder.build_final_prompt("（测试OS）", "在吗\n你好")
    lines = output.splitlines()

    assert output.startswith("[你的深层潜意识]：（测试OS）")
    assert lines[0] == "[你的深层潜意识]：（测试OS）", "第一段必须是潜意识 + 内心 OS"
    assert lines[1] == NOTICE_LINE, "第二段必须是固定提示语"
    assert lines[2] == "---", "第三行必须是分隔线"
    assert lines[3] == "[用户消息]"
    assert "\n".join(lines[4:]) == "在吗\n你好", "用户原文（含换行）必须完整保留"
    assert len(lines) == 6, "在吗/你好 应各占一行，共 6 行，实际 {0}: {1}".format(
        len(lines), lines
    )
    assert output.count("---") == 1, "分隔线只应出现一次"


def test_内心os与用户消息不同行():
    """精确证明 OS 与用户消息没有混在同一行。"""
    builder = PromptBuilder(seed=2)

    output = builder.build_final_prompt("（测试OS）", "在吗\n你好")
    lines = output.splitlines()

    os_line = lines[0]
    message_lines = lines[4:]

    assert "在吗" not in os_line, "用户消息不得混入潜意识那一行"
    assert "你好" not in os_line
    assert "（测试OS）" not in "\n".join(message_lines), "内心 OS 不得混入用户消息段"
    assert message_lines == ["在吗", "你好"]


@pytest.mark.parametrize(
    "inner_os,user_message",
    [("", "hi"), (None, None), ("", None), (None, "hi"), ("   ", "hi")],
)
def test_空输入不崩且结构完整(inner_os, user_message):
    builder = PromptBuilder(seed=3)

    output = builder.build_final_prompt(inner_os, user_message)
    lines = output.splitlines()

    assert output.startswith("[你的深层潜意识]：")
    assert "---" in lines
    assert "[用户消息]" in lines
    # 空 OS 时应有中性兜底，不能是空串
    assert lines[0].strip() != "[你的深层潜意识]："


@pytest.mark.parametrize("level", ["L0", "L1", "L2", "CRISIS"])
@pytest.mark.parametrize("affinity", [0, 45, 75, 150])
def test_inner_os全等级全好感度层(level, affinity):
    """4 等级 × 4 好感度层：非空、中文括号包裹、≤40 字。"""
    builder = PromptBuilder(seed=7)

    text = builder.build_inner_os(level, affinity)

    assert isinstance(text, str)
    assert text.strip(), "内心 OS 不得为空"
    assert text.startswith("（") and text.endswith("）"), (
        "{0}/{1} 未用中文括号包裹: {2!r}".format(level, affinity, text)
    )
    assert len(text) <= 40, "{0}/{1} 超过 40 字（{2}）: {3!r}".format(
        level, affinity, len(text), text
    )


def test_generate_inner_os别名可用():
    builder = PromptBuilder(seed=8)
    assert builder.generate_inner_os("L1", 60)
    assert builder.generate_inner_os("L1", 60).startswith("（")


@pytest.mark.parametrize(
    "level,affinity",
    [("XX", 0), ("", 0), (None, 0), ("L0", -5), ("L0", 10 ** 9), ("L2", -100)],
)
def test_inner_os容错边界(level, affinity):
    """未知等级降级 L0；负好感度 / 超大好感度不崩。"""
    builder = PromptBuilder(seed=9)

    text = builder.build_inner_os(level, affinity)

    assert text.strip()
    assert text.startswith("（") and text.endswith("）")
    assert len(text) <= 40


def test_未知等级降级为L0文案():
    """未知等级应降级到 L0 的模板池（同一 tier 下与 L0 同池）。"""
    builder_a = PromptBuilder(seed=10)
    builder_b = PromptBuilder(seed=10)

    unknown = builder_a.build_inner_os("XX", 150)
    fallback = builder_b.build_inner_os("L0", 150)

    assert unknown == fallback
