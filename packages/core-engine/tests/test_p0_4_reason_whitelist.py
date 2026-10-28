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
< 50 且 reason 为 ``None``（模型漂移）   **驳回 → 50**（无理由可审，按记仇处理）
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

    ``reason=None`` 时**不写 reason 字段**，用于模拟模型漂移：LLM 不回 reason。
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


def test_reason缺失时fail_closed驳回(engine_factory):
    """模型漂移不回 reason → 按记仇处理，闸门驳回。

    （执行单字面写的 ``test_reason缺失时fail-closed驳回`` 含连字符，
    不是合法 Python 标识符，import 期即 SyntaxError；此处按语义改为
    ``fail_closed`` 下划线写法，行为与断言完全一致。）
    """
    engine = _engine(engine_factory)
    restore = _patch_verdict(22)
    try:
        payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["target"] == 50, "模型漂移不回 reason 时闸门必须驳回（fail-closed）"
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
    第二次是 ``reason=None`` -> 走「无理由即驳回」-> 记仇扣分不会复活。
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
    assert apply_fn(30, None) == 50            # 无理由 -> 驳回（fail-closed）
    assert apply_fn(30, "") == 50              # 空理由 -> 驳回
    assert apply_fn(50, "你居然骂我") == 50     # 非负面 -> 不动
    assert apply_fn(80, "你居然骂我") == 80     # 正向 -> 不动

# ---------------------------------------------------------------- 5. F-20 · P1-1 否定反话


def test_否定反话驳回(engine_factory):
    """「我不会抱歉，是你骂了我」——含白名单词但是反话，必须驳回回 50（F-20 P1-1）。"""
    from engine import _is_negated

    engine = _engine(engine_factory)
    restore = _patch_verdict(20, reason="我不会抱歉，是你骂了我", confidence=0.9)
    try:
        payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["track"] == "B"
    assert debug["target"] == 50, "否定反话必须驳回，实际 {0}".format(debug["target"])
    assert debug["track_b_reason"] == "我不会抱歉，是你骂了我"
    # 纯函数层面同步确认：所有出现均被否定修饰
    assert _is_negated("我不会抱歉，是你骂了我", "抱歉") is True


def test_否定前缀不误伤正话(engine_factory):
    """无否定前缀的正话（如「担心你」）不受否定检测影响——回归保护。"""
    from engine import _is_negated

    engine = _engine(engine_factory)
    restore = _patch_verdict(30, reason="担心你", confidence=0.8)
    try:
        payload = _process(engine)
    finally:
        restore()

    assert payload["debug"]["target"] == 30, "正话命中不得被否定检测误伤"
    assert _is_negated("担心你", "担心") is False


@pytest.mark.parametrize(
    "reason",
    [
        "不会心疼你",
        "没担心过你一天",         # 「没」紧邻「担心」，3 字窗口内
        "别提自己难过了",
        "不用自责了",             # 白名单词被否定修饰
        "谈不上抱歉",
    ],
)
def test_每个白名单词的否定形式均驳回(engine_factory, reason):
    """5 个白名单词 × 否定前缀，全部视为反话驳回（参数化全量覆盖）。

    样本均落在否定窗口可覆盖的结构内：否定词紧邻白名单词，或隔 0~2 字。
    否定词隔 3~4 字的中隔反话由补修（T-F20-4）扩到 5 字窗口后覆盖，
    见 :func:`test_四字距离否定反话驳回`；隔 5 字以上或需要整体语义理解
    的情形（如「谁都会自责吗？不，我不会」）仍不在字符窗口模型的表达范围
    内（F-22 记录为遗留限制）。
    """
    engine = _engine(engine_factory)
    restore = _patch_verdict(25, reason=reason)
    try:
        payload = _process(engine)
    finally:
        restore()

    assert payload["debug"]["target"] == 50, "理由 {0!r} 是否定反话，应驳回".format(reason)


def test_闸门在locked层生效不回归(engine_factory, caplog):
    """P1-3 回归确认：闸门已在 _evaluate_track_b_locked 内生效（经 process 路径验证）。"""
    engine = _engine(engine_factory)
    restore = _patch_verdict(15, reason="你居然骂我", confidence=0.9)
    try:
        with caplog.at_level(logging.WARNING, logger="ffw.engine"):
            payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["target"] == 50, "locked 层闸门必须继续拦截记仇扣分"
    warnings = [r.getMessage() for r in caplog.records if "[WARN] Emotion drop rejected" in r.getMessage()]
    assert warnings, "locked 层驳回仍须打 WARN 日志"


# ---------------------------------------------------------------- 6. F-20 补修 · 5 字窗口 + 转折断句


@pytest.mark.parametrize(
    ("reason", "word"),
    [
        ("没什么好担心的，是你活该", "担心"),   # 「没」距「担心」4 字：3 字窗口时代会漏放行
        ("我从来没有自责过", "自责"),          # 「没」距「自责」3 字
        ("根本不会心疼你", "心疼"),            # 「不」距「心疼」2 字
    ],
)
def test_四字距离否定反话驳回(engine_factory, reason, word):
    """否定词与白名单词隔 3~4 字时也算反话——5 字窗口的覆盖面（补修 T-F20-4）。"""
    from engine import _is_negated

    engine = _engine(engine_factory)
    restore = _patch_verdict(25, reason=reason)
    try:
        payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["track"] == "B"
    assert debug["target"] == 50, "理由 {0!r} 是否定反话，应驳回".format(reason)
    assert word in reason, "样本必须含白名单词 {0!r}".format(word)
    assert _is_negated(reason, word) is True, "{0!r} 中的 {1!r} 应被否定修饰".format(reason, word)


@pytest.mark.parametrize(
    "reason",
    [
        "不抱歉但心疼",                       # 「不」修饰「抱歉」，不跨转折修饰「心疼」
        "不担心，可是我心疼",                 # 转折后「心疼」是正话
        "没自责，不过我担心你",               # 转折后「担心」是正话
        "不抱歉，然而自己难过",               # 转折后「自己难过」是正话
    ],
)
def test_转折连词切断否定修饰(engine_factory, reason):
    """转折连词（但/可是/不过/然而）切断否定修饰——前半句的否定不跨转折生效。"""
    from engine import _is_negated

    engine = _engine(engine_factory)
    restore = _patch_verdict(30, reason=reason)
    try:
        payload = _process(engine)
    finally:
        restore()

    debug = payload["debug"]
    assert debug["track"] == "B"
    assert debug["target"] == 30, "理由 {0!r} 含转折后正话，应放行".format(reason)


def test_转折切断后前半句仍算否定(engine_factory):
    """转折只切断修饰关系，不改变前半句本身的否定判定（纯函数层断言）。"""
    from engine import _is_negated

    assert _is_negated("不抱歉但心疼", "抱歉") is True, "前半句「不抱歉」仍是反话"
    assert _is_negated("不抱歉但心疼", "心疼") is False, "转折后「心疼」不被「不」修饰"
