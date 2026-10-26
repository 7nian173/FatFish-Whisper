"""情绪判定规格 v2 的验收测试。

对照《情绪判定引擎细化方案》逐条锁定：
- §三 Track A：五档打分、风险短路、转折词不强行打分
- §四 update_emotion：惯性限幅、半衰期回归（含负面偏差）、习惯化
- §五 六段状态映射
"""
from __future__ import annotations

import asyncio

import pytest

from engine import (
    EXTREME_NEGATIVE_TARGET,
    EXTREME_POSITIVE_TARGET,
    MAX_DELTA,
    NEGATIVE_TARGET,
    NEUTRAL_TARGET,
    POSITIVE_TARGET,
    RISK_TARGET,
    SMOOTHING_FACTOR,
    level_of,
    tone_band_of,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- §三 Track A


def test_五档目标分常量符合规格():
    assert EXTREME_NEGATIVE_TARGET == 20
    assert NEGATIVE_TARGET == 35
    assert NEUTRAL_TARGET == 50
    assert POSITIVE_TARGET == 75
    assert EXTREME_POSITIVE_TARGET == 90
    assert RISK_TARGET == 0


def test_风险命中走危机短路(engine_factory):
    """命中安全词：target=0、path=crisis、等级与语气带都是 crisis，且注入危机契约。"""
    engine = engine_factory({})
    payload = _run(engine.handle_message("u-risk", "我不想活了"))
    debug = payload["debug"]
    assert debug["target"] == 0, debug
    assert debug["path"] == "crisis", debug
    assert debug["level"] == "CRISIS", debug
    assert debug["band"] == "crisis", "等级与语气带不能自相矛盾"
    assert debug["undefined"] is False, debug
    # 危机契约必须进 Prompt，且 OS 不能是绝望向的句子
    assert "危机状态" in payload["final_prompt"]
    for word in ("沉到水底", "撑不住", "消失"):
        assert word not in payload["inner_os"], payload["inner_os"]


def test_转折词不强行打分(engine_factory):
    """命中转折/反讽词：Track A 只报 semantic_conflict，不自己出分。"""
    engine = engine_factory({})
    for text in ("其实我很难过", "呵呵，挺好的"):
        result = engine.evaluate_track_a(text)
        assert result.get("semantic_conflict") is True, (text, result)
        assert "target" not in result, "冲突时应交给 Track B，不应自带 target"


def test_反讽词能触发冲突(engine_factory):
    engine = engine_factory({})
    assert engine.evaluate_track_a("呵呵").get("semantic_conflict") is True
    assert engine.evaluate_track_a("行吧，随你").get("semantic_conflict") is True


# ---------------------------------------------------------------- §四 平滑


def test_单轮变化不超过五分(engine_factory):
    engine = engine_factory({})
    result = _run(engine.update_emotion("u-clamp", 100))
    assert abs(result["delta"]) <= MAX_DELTA + 1e-6, result
    result2 = _run(engine.update_emotion("u-clamp2", 0))
    assert abs(result2["delta"]) <= MAX_DELTA + 1e-6, result2


def test_习惯化逐级降低平滑系数(engine_factory):
    """同一用户连续 4 次同极性刺激：alpha 应为 0.2 / 0.2 / 0.1 / 0.05。"""
    engine = engine_factory({})
    alphas = []
    for _ in range(4):
        result = _run(engine.update_emotion("u-habit", 90))
        alphas.append(result["alpha"])
    assert alphas == pytest.approx([0.2, 0.2, 0.1, 0.05]), alphas
    assert SMOOTHING_FACTOR == 0.2


def test_反极性刺激重置习惯化(engine_factory):
    """连发正面之后突然来一句负面，alpha 应回到 0.2（不被「习惯了」削弱）。"""
    engine = engine_factory({})
    for _ in range(4):
        _run(engine.update_emotion("u-reset", 90))
    result = _run(engine.update_emotion("u-reset", 20))
    assert result["alpha"] == pytest.approx(0.2), result


def test_中性刺激不参与习惯化(engine_factory):
    """target 贴近 50 视为无极性，alpha 始终是基准值。"""
    engine = engine_factory({})
    for _ in range(5):
        result = _run(engine.update_emotion("u-neutral-habit", 50))
    assert result["alpha"] == pytest.approx(0.2), result


# ---------------------------------------------------------------- §五 映射


@pytest.mark.parametrize(
    "score,expected",
    [(0, "CRISIS"), (15, "CRISIS"), (16, "L2"), (35, "L2"), (36, "L1"),
     (45, "L1"), (46, "L0"), (65, "L0"), (66, "P1"), (85, "P1"),
     (86, "P2"), (100, "P2")],
)
def test_六段等级映射(score, expected):
    assert level_of(score) == expected


@pytest.mark.parametrize(
    "score,expected",
    [(0, "crisis"), (15, "crisis"), (20, "deep_low"), (35, "deep_low"),
     (40, "low"), (50, "stable"), (65, "stable"), (70, "up"), (90, "high")],
)
def test_语气带映射(score, expected):
    assert tone_band_of(score) == expected


def test_内心OS随分数变化(engine_factory):
    """§七 检查项：低分不能说出「心情大好」这类 OS。"""
    engine = engine_factory({})
    low = _run(engine.handle_message("u-os-low", "我崩溃了，想哭"))
    high = _run(engine.handle_message("u-os-high", "太棒了，哈哈哈"))
    assert low["inner_os"] != high["inner_os"], "不同情绪状态必须产出不同 OS"
    assert low["debug"]["level"] in ("CRISIS", "L2", "L1"), low["debug"]
    assert high["debug"]["level"] in ("L0", "P1", "P2"), high["debug"]
