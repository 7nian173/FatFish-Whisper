"""时间块验收：分钟粒度、时段划分、尾部位置、前缀稳定。

来源：B 线（WorkBuddy `main-77ae86b6`）`test_time_and_bands.py` 的时间块部分，
按 A/B 合并终裁（A-10）拆件吸收。B 线的 bands 元数据部分依赖 A 线尚无的
``bands_meta()``，本次未吸收。

守的是三件事：

1. 时间**只精确到分钟**——秒级变化会让同一分钟的两次请求 prompt 不同，
   白白击穿前缀缓存（死线 D3）；
2. 时间块必须在**动态尾部**（记忆块之后、分隔线与用户消息之前），
   绝不进 System 前缀（死线 D2）；
3. 时段语义要让模型读得懂「深夜」而不只是「23:47」。
"""
from __future__ import annotations

import datetime

from prompt_builder import PromptBuilder, time_block


def test_时间块只精确到分钟():
    moment = datetime.datetime(2026, 10, 26, 21, 47, 33)
    assert time_block(moment) == "[现在] 2026-10-26 21:47 周一 · 晚上"


def test_时间块时段划分():
    cases = {
        0: "深夜",
        4: "深夜",
        5: "清晨",
        8: "上午",
        11: "中午",
        13: "下午",
        17: "傍晚",
        19: "晚上",
        23: "深夜",
    }
    for hour, period in cases.items():
        text = time_block(datetime.datetime(2026, 10, 26, hour, 0))
        assert text.endswith(period), "{0} 点应是 {1}，实际 {2}".format(hour, period, text)


def test_同一分钟内的两次调用完全一致():
    """秒数不同但分钟相同 -> 文本必须一致，否则前缀缓存被无意义击穿。"""
    first = time_block(datetime.datetime(2026, 10, 26, 21, 47, 1))
    second = time_block(datetime.datetime(2026, 10, 26, 21, 47, 59))
    assert first == second


def test_时间块在Prompt尾部且不影响前缀():
    builder = PromptBuilder(seed=1)
    moment = datetime.datetime(2026, 10, 26, 21, 47)
    prompt = builder.build_final_prompt("（有点开心。）", "在吗", now=moment)

    assert "[现在] 2026-10-26 21:47 周一 · 晚上" in prompt
    # 时间块必须出现在用户消息之前、且整段前缀不因时间变化
    head_before_time = prompt.split("[现在]")[0]
    other = builder.build_final_prompt(
        "（有点开心。）", "在吗", now=moment + datetime.timedelta(minutes=5)
    )
    assert other.split("[现在]")[0] == head_before_time


def test_可以关掉时间块():
    builder = PromptBuilder(seed=1)
    prompt = builder.build_final_prompt("（嗯。）", "在吗", include_time=False)
    assert "[现在]" not in prompt


def test_时间块与记忆块可以共存():
    builder = PromptBuilder(seed=1)
    prompt = builder.build_final_prompt(
        "（嗯。）",
        "在吗",
        memories=["他上周说过喜欢下雨天"],
        now=datetime.datetime(2026, 10, 26, 9, 5),
    )
    assert "[相关回忆]" in prompt
    assert "[现在] 2026-10-26 09:05" in prompt
    # 顺序：回忆 -> 时间 -> 用户消息
    assert prompt.index("[相关回忆]") < prompt.index("[现在]") < prompt.index("[用户消息]")
