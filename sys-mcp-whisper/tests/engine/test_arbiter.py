"""DictionaryArbiter tests (task-FFW-P25-T5, card section 3.3 -- eight).

Anti-tautology spine: every interesting property is asserted on BOTH the
score and ``matched_keywords`` evidence, and the two clip tests force the raw
sum *outside* ``[-1, 1]`` so an implementation without the clamp cannot
coincidentally land inside.
"""

from __future__ import annotations

import pytest

from packages.core.engine import DictionaryArbiter


@pytest.fixture()
def arbiter() -> DictionaryArbiter:
    return DictionaryArbiter()


def test_empty_string_scores_all_zero_no_keywords(arbiter: DictionaryArbiter) -> None:
    scores = arbiter.evaluate("")
    assert scores["agreeableness"] == 0.0
    assert scores["arousal"] == 0.0
    assert scores["confidence"] == 0.0
    assert arbiter.matched_keywords("") == ()


def test_whitespace_only_scores_all_zero(arbiter: DictionaryArbiter) -> None:
    scores = arbiter.evaluate("   \t\n ")
    assert scores["agreeableness"] == 0.0
    assert scores["arousal"] == 0.0
    assert scores["confidence"] == 0.0
    assert arbiter.matched_keywords("   \t\n ") == ()


def test_no_hit_scores_all_zero_but_lexicon_is_nonempty(arbiter: DictionaryArbiter) -> None:
    # The zero result only means something because the lexicon is non-empty:
    # assert the miss is real, not a vacuous table.
    scores = arbiter.evaluate("今天天气不错 出门散步")
    assert scores["agreeableness"] == 0.0
    assert scores["arousal"] == 0.0
    assert scores["confidence"] == 0.0
    assert arbiter.matched_keywords("今天天气不错 出门散步") == ()


def test_positive_word_positive_agreeableness(arbiter: DictionaryArbiter) -> None:
    scores = arbiter.evaluate("太好了 谢谢你")
    assert scores["agreeableness"] > 0
    assert arbiter.matched_keywords("太好了 谢谢你") == ("太好了", "谢谢")


def test_negative_word_negative_agreeableness(arbiter: DictionaryArbiter) -> None:
    scores = arbiter.evaluate("你真讨厌")
    assert scores["agreeableness"] < 0
    assert "讨厌" in arbiter.matched_keywords("你真讨厌")


def test_multiple_hits_accumulate_exact_sum(arbiter: DictionaryArbiter) -> None:
    # ("谢谢", +0.5, 0.0) + ("好的", +0.4, -0.2): both same-direction on the
    # agreeableness axis, so the sum is 0.9 -- asserted as an exact value, not
    # merely "> 0", to lock the accumulation semantics.
    message = "谢谢你 好的 就这么办"
    scores = arbiter.evaluate(message)
    # confidence: count_factor = min(1, 2/2) = 1; matched chars 3+2=5 over
    # stripped length 10 -> density 0.5 >= 0.25 -> length_factor = 1;
    # confidence = 1.0, so the raw sums pass through unshrunk.
    assert scores["confidence"] == pytest.approx(1.0)
    assert scores["agreeableness"] == pytest.approx(0.5 + 0.4)
    assert scores["arousal"] == pytest.approx(0.0 + -0.2)
    assert arbiter.matched_keywords(message) == ("谢谢", "好的")


def test_clip_upper_bound_hits_exact_one_point_zero(arbiter: DictionaryArbiter) -> None:
    # "太好了"(+0.8) + "喜欢你"(+0.7) = 1.5 raw > 1.0: an unclamped
    # implementation returns 1.5, so == 1.0 fails for it (anti-tautology).
    message = "太好了 喜欢你"
    scores = arbiter.evaluate(message)
    # confidence 1.0 (two hits; matched chars 6 / stripped length 7 >= 0.25):
    # raw 1.5 clamps to exactly 1.0, unchanged by shrinkage.
    assert scores["confidence"] == pytest.approx(1.0)
    assert scores["agreeableness"] == 1.0
    assert arbiter.matched_keywords(message) == ("太好了", "喜欢你")


def test_clip_lower_bound_hits_exact_minus_one_point_zero(arbiter: DictionaryArbiter) -> None:
    # "滚"(-0.9) + "闭嘴"(-0.8) = -1.7 raw < -1.0: unclamped would return
    # -1.7, so == -1.0 pins the lower clamp.
    message = "滚 闭嘴"
    scores = arbiter.evaluate(message)
    # confidence 1.0 (two hits; matched chars 3 / stripped length 3 = 1.0):
    # raw -1.7 clamps to exactly -1.0, unchanged by shrinkage.
    assert scores["confidence"] == pytest.approx(1.0)
    assert scores["agreeableness"] == -1.0
    assert arbiter.matched_keywords(message) == ("滚", "闭嘴")


def test_matched_keywords_reports_exact_hit_set(arbiter: DictionaryArbiter) -> None:
    # One positive, one negative, one arousal-heavy: the tuple must name all
    # three in lexicon order.
    message = "急死了 慢点 谢谢"
    # Lexicon order: 谢谢(idx1) 急死了(idx12) 慢点(idx13)
    assert arbiter.matched_keywords(message) == ("谢谢", "急死了", "慢点")
    scores = arbiter.evaluate(message)
    # confidence: count 3 -> 1.0; matched chars 3+2+2=7 / stripped length 8
    # -> density 0.875 >= 0.25 -> 1.0; raw sums pass through unshrunk.
    assert scores["confidence"] == pytest.approx(1.0)
    # arousal: 急死了 +0.9 + 慢点 -0.5 + 谢谢 0.0 = 0.4, below the clamp
    assert scores["arousal"] == pytest.approx(0.4)
    # agreeableness: 谢谢 +0.5 + 慢点 +0.3 + 急死了 -0.1 = 0.7, below the clamp
    assert scores["agreeableness"] == pytest.approx(0.7)

def test_high_confidence_multi_hit_close_to_raw_sum(arbiter: DictionaryArbiter) -> None:
    # Card section 13: two corroborating hits with dense coverage -> confidence
    # 1.0, scores equal the raw accumulated sums (no shrinkage).
    scores = arbiter.evaluate("太好了 喜欢你")
    assert scores["confidence"] == pytest.approx(1.0)
    # raw 0.8 + 0.7 = 1.5 -> clamp 1.0, unshrunk.
    assert scores["agreeableness"] == pytest.approx(1.0)


def test_low_confidence_single_rare_hit_shrinks_towards_zero(
    arbiter: DictionaryArbiter,
) -> None:
    # One short hit adrift in a long message: count_factor 0.5, density ~0.03
    # (2 chars / 70) -> length_factor ~0.12 -> confidence ~0.31.  The raw
    # agreeableness 0.8 must arrive well below 0.5, visibly shrunk.
    message = "太好了" + " 今天我们一起出门去公园散步然后吃午饭逛街看电影散长步聊天到天黑好吗" * 3
    scores = arbiter.evaluate(message)
    assert scores["confidence"] < 0.5
    assert scores["agreeableness"] == pytest.approx(0.8 * scores["confidence"])
    assert abs(scores["agreeableness"]) < 0.5


def test_sarcasm_lone_too_good_cannot_reach_extreme_value(
    arbiter: DictionaryArbiter,
) -> None:
    # The irony guard: a lone "太好了" (raw agreeableness 0.8, which after
    # clamping is NOT extreme, but raw arousal-style +-0.9 lexicon entries
    # like 滚 must also be unreachable from a single hit).
    scores = arbiter.evaluate("太好了")
    # count_factor = 0.5, density = 3/3 = 1.0 -> confidence = 0.75.
    assert scores["confidence"] == pytest.approx(0.75)
    # 0.8 * 0.75 = 0.6: shrunk, and no single-hit axis can ever reach 0.9
    # because 0.5 * |raw| <= 0.5 for any single-hit |raw| <= 1.0.
    assert scores["agreeableness"] == pytest.approx(0.6)
    assert abs(scores["agreeableness"]) < 0.9
    lone = arbiter.evaluate("滚")
    # 滚 raw arousal 0.8 shrunk by 0.75 -> 0.6, never the extreme 0.8+.
    assert lone["confidence"] == pytest.approx(0.75)
    assert lone["arousal"] == pytest.approx(0.6)
    assert abs(lone["arousal"]) < 0.9
