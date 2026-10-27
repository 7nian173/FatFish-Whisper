"""P0-4 · 防记仇闸门（Track B 负面判定必须出自「心疼类」理由）。

背景
----
Track B 只管「这句话让 Bot 的情绪目标分变成多少」，它不区分两种截然不同的负面：

1. **共情式低落**：用户说自己难受 / 累，Bot 因为心疼而跟着低落——这是要的；
2. **被冒犯式记仇**：用户骂了 Bot，LLM 认为「我受到了不尊重」于是扣分——
   这会让 Bot 真的记仇，是缺陷。

两者在分数上无法区分（都是低于 50），唯一能区分的是 LLM 自己写的 ``reason``。
本文件的验收即：``reason`` 命中白名单才允许扣分，否则驳回回中性分 50。

判定表（与 ``EmotionEngine._apply_reason_whitelist`` 文档一致）
--------------------------------------------------------------
====================================  ==============================
target / reason                       期望
====================================  ==============================
>= 50（无论 reason）                  原样保留（不拦正向/中性）
< 50 且 reason 含白名单词             原样保留（心疼，不是记仇）
< 50 且 reason 不含白名单词           **驳回 → 50**，并打 [WARN] 日志
< 50 且 reason 为 ``None``（旧协议）   原样保留（无理由可审，保持历史行为）
====================================  ==============================
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from engine import EmotionEngine, TrackB

#: 有 LLM 配置（Track B enabled），请求由替身接管，不出网
LLM_ENV = {
    "LLM_API_BASE": "https://api.deepseek.com/v1",
    "LLM_API_KEY": "sk-mock-key-p04",
    "LLM_MODEL": "deepseek-chat",
}

#: 无任何情绪命中词的句子 -> 走 undefined 分支 -> 必定进 Track B
NEUTRAL_TEXT = "qwxz"


def _engine(engine_factory) -> EmotionEngine:
    return engine_factory(LLM_ENV)


def _patch_verdict(target, reason=None, confidence=None):
    """把 ``TrackB._request`` 换成回固定 JSON 的替身，返回恢复函数。

    ``reason=None`` 时**不写 reason 字段**，用于模拟旧协议 / 旧测试替身。
    """
    original = TrackB._request

    payload: dict[str, object] = {"emotion_target": target}
    if reason is not None:
        payload["reason"] = reason
    if confidence is not None:
        payload["confidence"] = confidence

    async def fake_request(self, body, headers):
        return {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    TrackB._request = fake_request

    def restore():
        TrackB._request = original

    return restore


def _process(engine: EmotionEngine, user_id: str = "u-p04"):
    return asyncio.run(engine.process(user_id, NEUTRAL_TEXT))


# ---------------------------------------------------------------- 1. 合法理由：放行


def test_合法理由担心你太累了正常扣分(engine_factory):
    """reason 命中「担心」-> 这属于共情，30 分必须原样落库。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(30, reason="担心你太累了，一直没休息", confidence=0.82)
    try:
        payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["track"] == "B", "该句无命中词，必须走 Track B"
    assert debug["target"] == 30, "心疼类理由必须放行，实际 {0}".format(debug["target"])
    assert debug["track_b_reason"] == "担心你太累了，一直没休息"
    assert debug["track_b_confidence"] == 0.82


@pytest.mark.parametrize(
    "reason",
    ["心疼", "担心你", "我自己难过", "怪自己没用，自责", "抱歉，是我不好"],
)
def test_白名单每个词都能放行扣分(engine_factory, reason):
    """白名单是 5 个词的并集，任一命中即可扣分。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(35, reason=reason)
    try:
        payload = _process(engine)
    finally:
        restore()

    assert payload["debug"]["target"] == 35, "理由 {0!r} 应命中白名单".format(reason)


# ---------------------------------------------------------------- 2. 非法理由：驳回


def test_非法理由你居然骂我拒绝扣分保持50(engine_factory, caplog):
    """reason 不含白名单词 -> 判定为记仇，强制拉回 50，且必须留下 WARN 日志。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(20, reason="你居然骂我，我很不开心", confidence=0.9)
    try:
        with caplog.at_level(logging.WARNING, logger="ffw.engine"):
            payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["track"] == "B"
    assert debug["target"] == 50, "记仇式扣分必须被驳回回 50，实际 {0}".format(debug["target"])
    # 原始判定仍要回显，否则排查时看不出「模型想扣 20，是被闸门拦下的」
    assert debug["track_b_reason"] == "你居然骂我，我很不开心"

    warnings = [r.getMessage() for r in caplog.records if "[WARN] Emotion drop rejected" in r.getMessage()]
    assert warnings, "驳回时必须打 [WARN] Emotion drop rejected by reason whitelist"
    assert any("20" in message for message in warnings), "日志要带原始分数，便于复盘"


def test_空理由同样驳回(engine_factory):
    """reason 是空串（模型给了字段但没内容）-> 无法审计，驳回。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(10, reason="")
    try:
        payload = _process(engine)
    finally:
        restore()

    assert payload["debug"]["target"] == 50


# ---------------------------------------------------------------- 3. 兼容与边界


def test_reason缺失时保持旧行为(engine_factory):
    """旧协议只回 emotion_target：没有理由可审，保持历史行为（放行）。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(22)
    try:
        payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["target"] == 22, "没有 reason 字段不得被闸门拦截"
    assert debug["track_b_reason"] is None
    assert debug["track_b_confidence"] == 0.0


@pytest.mark.parametrize("target", [50, 65, 100])
def test_非负面分数不受闸门影响(engine_factory, target):
    """target >= 50 一律原样返回，哪怕 reason 是「被冒犯」。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(target, reason="你居然骂我")
    try:
        payload = _process(engine)
    finally:
        restore()

    assert payload["debug"]["target"] == target


def test_缓存命中不丢reason(engine_factory):
    """第二次同句命中缓存，reason 必须一起读回，否则白名单被绕过。

    这是 ``TrackB._remember`` 存 dict 而不是裸分数的直接验收：若只缓存分数，
    第二次是 ``reason=None`` -> 走「无理由放行」-> 记仇扣分复活。
    """
    engine = _engine(engine_factory)
    restore = _patch_verdict(20, reason="你居然骂我")
    try:
        first = _process(engine)
        second = _process(engine)  # 同一句话 -> Track B 缓存命中
    finally:
        restore()

    assert first["debug"]["target"] == 50
    assert second["debug"]["target"] == 50, "缓存命中不得让记仇扣分复活"
    assert second["debug"]["track_b_reason"] == "你居然骂我"


# ---------------------------------------------------------------- 4. 协议与解析


def test_evaluate_verdict返回三元组(engine_factory):
    """``evaluate_verdict`` 必须带回 (score, reason, confidence)。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(40, reason="担心你", confidence=0.5)
    try:
        verdict = asyncio.run(engine.evaluate_track_b_verdict(NEUTRAL_TEXT, []))
    finally:
        restore()

    assert verdict == (40, "担心你", 0.5)


def test_旧接口evaluate仍返回裸分数(engine_factory):
    """``evaluate_track_b`` 保持 ``-> int`` 契约，不因内部改造破坏调用方。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(40, reason="担心你", confidence=0.5)
    try:
        score = asyncio.run(engine.evaluate_track_b(NEUTRAL_TEXT, []))
    finally:
        restore()

    assert score == 40


@pytest.mark.parametrize(
    "confidence,expected",
    [(0.5, 0.5), (0, 0.0), (1.0, 1.0), (1.7, 1.0), (-0.3, 0.0), ("bad", 0.0), (None, 0.0)],
)
def test_confidence越界与非法值被归一(engine_factory, confidence, expected):
    """confidence 必须落在 [0, 1]；坏值不得把异常漏出 Track B。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(45, reason="担心", confidence=confidence)
    try:
        _, _, got = asyncio.run(engine.evaluate_track_b_verdict(NEUTRAL_TEXT, []))
    finally:
        restore()

    assert got == expected


def test_白名单闸门是纯函数可单独断言():
    """``_apply_reason_whitelist`` 不依赖状态，直接对判定表逐行验证。"""
    apply_fn = EmotionEngine._apply_reason_whitelist

    assert apply_fn(30, "担心你") == 30        # 命中白名单
    assert apply_fn(30, "你居然骂我") == 50     # 未命中 -> 驳回
    assert apply_fn(30, None) == 30            # 无理由 -> 放行（旧协议）
    assert apply_fn(30, "") == 50              # 空理由 -> 驳回
    assert apply_fn(50, "你居然骂我") == 50     # 非负面 -> 不动
    assert apply_fn(80, "你居然骂我") == 80     # 正向 -> 不动
