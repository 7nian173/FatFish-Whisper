"""六段情绪带元数据一致性测试。

这里守的是一件"一旦分叉就会变成 issue 噪音"的事：``bands_meta()`` 的边界
必须与 ``level_of()`` 完全一致（0-100 全覆盖、无缝隙无重叠）。

来源：B 线 ``tests/test_time_and_bands.py`` 的 bands 部分（6 条用例），
按 F-06 执行单 v2「只取 bands 6 条、时间块 6 条不重复移植」拆件吸收——
时间块部分 A 线已有 ``tests/test_time_block.py``，不在此重复。
"""
from __future__ import annotations

from engine import (
    LEVEL_CRISIS,
    LEVEL_L0,
    LEVEL_L1,
    LEVEL_L2,
    LEVEL_P1,
    LEVEL_P2,
    bands_meta,
    level_of,
    tone_band_of,
)


# ---------------------------------------------------------------- bands 元数据


def test_bands_返回六段():
    bands = bands_meta()
    assert len(bands) == 6
    assert [item["band"] for item in bands] == [
        "crisis",
        "deep_low",
        "low",
        "stable",
        "up",
        "high",
    ]


def test_bands_覆盖0到100无缝隙无重叠():
    bands = bands_meta()
    assert bands[0]["min"] == 0
    assert bands[-1]["max"] == 100
    for previous, current in zip(bands, bands[1:]):
        assert current["min"] == previous["max"] + 1, "段与段之间不许有缝隙或重叠"


def test_bands_与level_of逐分一致():
    """0-100 每一分都必须落在"标着对应等级"的那一段里。"""
    for score in range(0, 101):
        expected = level_of(score)
        hits = [item for item in bands_meta() if item["min"] <= score <= item["max"]]
        assert len(hits) == 1, "分数 {0} 命中了 {1} 段".format(score, len(hits))
        assert hits[0]["label"] == expected, "分数 {0} 的段标签与 level_of 不一致".format(score)


def test_bands_标签覆盖全部等级():
    labels = {item["label"] for item in bands_meta()}
    assert labels == {LEVEL_CRISIS, LEVEL_L2, LEVEL_L1, LEVEL_L0, LEVEL_P1, LEVEL_P2}


def test_bands_语气带与tone_band_of一致():
    for item in bands_meta():
        for score in (item["min"], item["max"]):
            assert tone_band_of(score) == item["band"]


def test_bands_不含颜色():
    """颜色属于设计系统，后端只给阈值与语义键。"""
    for item in bands_meta():
        assert set(item.keys()) == {"band", "label", "min", "max"}
