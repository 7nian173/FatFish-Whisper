"""P3-E2: to_delta 纯函数与常量锁定测试 (task-FFW-P3-E2).

T1-T8 数值/行为精确断言 (禁 pytest.approx, 期望值均为 round(3) 后可精确
表示的十进制数); T9 白名单闭合; T10 常量存在 + 法定注释逐字 6 次;
T11 AST 级纯函数守卫 (无 IO 名, 无 ast.Global, 无写回入参的赋值目标).
"""

from __future__ import annotations

import ast
import inspect

import pytest

import packages.core.engine.stimulus as mod
from packages.core.domain.global_context import DELTA_ELIGIBLE_PATHS
from packages.core.engine.stimulus import to_delta

# 法定注释句 (人类裁决逐字文案; 全角标点用 unicode 转义拼接,
# 写法参照 tests/domain/test_global_context.py:41 的既有模式).

LEGAL_COMMENT = (
    "\u6b64\u4e3a\u7ed3\u6784\u5360\u4f4d\u503c\uff0c\u5b9e\u9645\u4e1a\u52a1\u6570\u503c\u5f85 P4 "
    "\u9636\u6bb5\u57fa\u4e8e\u771f\u5b9e\u5bf9\u8bdd\u6570\u636e\u538b\u6d4b\u540e\u88c1\u51b3\u3002"
)


def test_t1_positive_plus_arousal_precise_values() -> None:
    out = to_delta({"agreeableness": 0.5, "arousal": 0.5, "confidence": 0.0})
    assert out == {
        "pulse.dopamine": 4.0,
        "pulse.oxytocin": 3.0,
        "ipc.affiliation": 3.0,
        "pulse.cortisol": 2.0,
    }


def test_t2_positive_arousal_below_threshold() -> None:
    out = to_delta({"agreeableness": 0.525, "arousal": 0.225, "confidence": 0.75})
    assert out == {"pulse.dopamine": 4.2, "pulse.oxytocin": 3.15, "ipc.affiliation": 3.15}


def test_t3_negative_plus_arousal() -> None:
    out = to_delta({"agreeableness": -0.675, "arousal": 0.6, "confidence": 0.75})
    assert out == {"pulse.cortisol": 8.4, "ipc.affiliation": -4.05}


def test_t4_all_zero_returns_empty_dict() -> None:
    assert to_delta({"agreeableness": 0.0, "arousal": 0.0, "confidence": 0.0}) == {}


def test_t5_negative_below_threshold() -> None:
    out = to_delta({"agreeableness": -0.4, "arousal": 0.2, "confidence": 0.5})
    assert out == {"pulse.cortisol": 3.2, "ipc.affiliation": -2.4}


def test_t6_zero_affection_high_arousal_still_cortisol() -> None:
    assert to_delta({"agreeableness": 0.0, "arousal": 1.0, "confidence": 0.5}) == {
        "pulse.cortisol": 7.0
    }


def test_t7_missing_key_raises_key_error() -> None:
    with pytest.raises(KeyError):
        to_delta({"happiness": 1.0})


def test_t8_input_not_mutated_and_return_is_new_dict() -> None:
    arb = {"agreeableness": 0.5, "arousal": 0.8, "confidence": 0.5}
    snapshot = arb.copy()
    out = to_delta(arb)
    assert arb == snapshot
    assert out is not arb


def test_t9_keys_within_delta_eligible_paths() -> None:
    out = to_delta({"agreeableness": 0.5, "arousal": 0.5, "confidence": 0.0})
    assert set(out) <= set(DELTA_ELIGIBLE_PATHS)


EXPECTED_CONSTANTS = {
    "PLACEHOLDER_STIMULUS_POS_DOPAMINE_RATE": 8.0,
    "PLACEHOLDER_STIMULUS_POS_OXYTOCIN_RATE": 6.0,
    "PLACEHOLDER_STIMULUS_POS_AFFILIATION_RATE": 6.0,
    "PLACEHOLDER_STIMULUS_NEG_CORTISOL_RATE": 8.0,
    "PLACEHOLDER_STIMULUS_AROUSAL_THRESHOLD": 0.3,
    "PLACEHOLDER_STIMULUS_AROUSAL_CORTISOL_RATE": 10.0,
}

# 旧裸名 (t95 交卷态误用名): 必须不存在, 防实现回退到缺前缀命名.
OLD_BARE_NAMES = (
    "POS_DOPAMINE_RATE",
    "POS_OXYTOCIN_RATE",
    "POS_AFFILIATION_RATE",
    "NEG_CORTISOL_RATE",
    "AROUSAL_THRESHOLD",
    "AROUSAL_CORTISOL_RATE",
)


def test_t10_constants_exist_with_legal_comment_six_times() -> None:
    for name, value in EXPECTED_CONSTANTS.items():
        assert name in dir(mod)
        assert getattr(mod, name) == value
    for bare in OLD_BARE_NAMES:
        assert bare not in dir(mod)
    source = inspect.getsource(mod)
    assert source.count(LEGAL_COMMENT) == 6


def test_t11_to_delta_is_pure_by_ast() -> None:
    source = inspect.getsource(mod)
    tree = ast.parse(source)
    func = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "to_delta"
    )
    io_names = {"open", "__import__", "importlib"}
    for node in ast.walk(func):
        assert not (isinstance(node, ast.Name) and node.id in io_names)
        assert not (isinstance(node, ast.Attribute) and node.attr in io_names)
        assert not isinstance(node, ast.Global)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                # 只禁写回入参 arb: 目标若是形参名 (或其下标/属性) 即违规;
                # 局部容器 out[...] = ... 是函数构建返回值的正常手段, 放行.
                base = target
                while isinstance(base, (ast.Subscript, ast.Attribute)):
                    base = base.value
                if isinstance(base, ast.Name) and base.id == "arb":
                    raise AssertionError(
                        f"to_delta must not assign back to the input arb: {ast.dump(target)}"
                    )
    # 顶层同样不得出现 IO 调用名 (import 语句除外)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for sub in ast.walk(node):
            assert not (isinstance(sub, ast.Name) and sub.id in io_names)


def test_t12_arousal_exactly_at_threshold_does_not_trigger() -> None:
    # 0.3 恰等于阈值: 严格大于语义下不产 cortisol
    assert to_delta({"agreeableness": 0.0, "arousal": 0.3, "confidence": 0.0}) == {}
    # 反向: 0.31 超阈值必产 cortisol (防把判据改成 >= 后又把常量改大来凑绿)
    assert to_delta({"agreeableness": 0.0, "arousal": 0.31, "confidence": 0.0}) == {
        "pulse.cortisol": 0.1
    }
