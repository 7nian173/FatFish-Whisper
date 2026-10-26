"""验收 1 · Track B 熔断容错（最关键）。

契约：``evaluate_track_b`` **永不抛异常**。
- 未配置 Key / 占位 Key  -> 50，且**不发请求**；
- 网络异常 / 4xx / 5xx / 坏 Key -> 50；
- 非 JSON / 字段缺失 / 非数值 / NaN / bool -> 50；
- 合法响应能解析；数值越界 **clamp 到 100/0**（不是 50）。
"""
from __future__ import annotations

import asyncio

import pytest

import _helpers as H

#: 有 Key 但地址不可达（discard 端口），用于验证真实网络失败路径
BAD_ENV = {
    "LLM_API_BASE": "http://127.0.0.1:9/v1",
    "LLM_API_KEY": "sk-bad-key-123456",
    "LLM_MODEL": "deepseek-chat",
    "LLM_TIMEOUT": "1",
}

#: 合法配置（请求会被 monkeypatch 掉，不会真的出网）
GOOD_ENV = {
    "LLM_API_BASE": "http://127.0.0.1:9/v1",
    "LLM_API_KEY": "sk-live-abcdef123456",
    "LLM_MODEL": "deepseek-chat",
    "LLM_TIMEOUT": "1",
}


# ---------------------------------------------------------------- 网络层熔断

def test_坏key加不可达base返回50且不抛异常(engine_factory):
    """真实走 httpx：坏 Key + 不可达地址 -> 必须 50，不能把异常抛给调用方。"""
    engine = engine_factory(BAD_ENV)
    assert engine.track_b.enabled is True, "坏 Key 不应被判为未配置"

    result = asyncio.run(engine.evaluate_track_b("其实我挺好的", []))

    assert result == 50, "网络失败必须兜底 50，实际 {0}".format(result)


def test_内部请求抛RuntimeError仍返回50(engine_factory):
    """内部实现抛任意异常，也要被最后一道防线吞掉。"""
    engine = engine_factory(GOOD_ENV)
    engine.track_b._request = H.make_request_raising(RuntimeError("boom"))

    result = asyncio.run(engine.evaluate_track_b("内部异常兜底", []))

    assert result == 50


# ---------------------------------------------------------------- 响应解析

@pytest.mark.parametrize(
    "content,expected,label",
    [
        ("我真的说不清，大概还行吧。", 50, "非JSON纯文本"),
        ('{"score": 88}', 50, "字段缺失"),
        ('{"emotion_target": "abc"}', 50, "非数值字符串"),
        ('{"emotion_target": true}', 50, "布尔值"),
        ('{"emotion_target": NaN}', 50, "NaN"),
        ('```json\n{"emotion_target": 77}\n```', 77, "代码块包裹"),
        ('嗯，我觉得 {"emotion_target": 12} 大概就这样', 12, "前后多余文字"),
        ('{"emotion_target": 150}', 100, "越界上界clamp到100"),
        ('{"emotion_target": -5}', 0, "越界下界clamp到0"),
    ],
)
def test_响应解析与越界策略(engine_factory, content, expected, label):
    """逐条验证 JSON Mode 的解析容错与 clamp 语义。"""
    engine = engine_factory(GOOD_ENV)
    H.patch_request(engine, H.make_request_returning(H.llm_payload(content)))

    # 每条用不同消息，避免 diskcache 结果缓存串味
    result = asyncio.run(engine.evaluate_track_b("用例-" + label, []))

    assert result == expected, "{0}: 期望 {1}，实际 {2}".format(label, expected, result)


def test_缺少choices节点返回50(engine_factory):
    """响应体结构不对（无 choices）也要兜底。"""
    engine = engine_factory(GOOD_ENV)
    H.patch_request(engine, H.make_request_returning({"unexpected": "shape"}))

    assert asyncio.run(engine.evaluate_track_b("坏结构响应", [])) == 50


# ---------------------------------------------------------------- 未配置 Key

@pytest.mark.parametrize("api_key", ["", "sk-xxxxxx", "sk-xxxxxxxxxxxxxxxx"])
def test_未配置key时禁用且不发请求(engine_factory, api_key):
    """空 Key / 占位 Key：``enabled is False``，直接 50，请求次数必须为 0。"""
    env = dict(GOOD_ENV)
    env["LLM_API_KEY"] = api_key
    engine = engine_factory(env)

    assert engine.track_b.enabled is False

    calls = H.patch_request(
        engine, H.make_request_returning(H.llm_payload('{"emotion_target": 99}'))
    )
    result = asyncio.run(engine.evaluate_track_b("未配置key", []))

    assert result == 50
    assert calls["n"] == 0, "未配置 Key 时绝不允许发请求，实际调用 {0} 次".format(
        calls["n"]
    )


@pytest.mark.parametrize(
    "missing",
    [
        {"LLM_API_KEY": "sk-live-abcdef123456", "LLM_MODEL": "deepseek-chat"},  # 缺 base
        {"LLM_API_BASE": "http://127.0.0.1:9/v1", "LLM_API_KEY": "sk-live-abcdef123456"},  # 缺 model
    ],
)
def test_配置不完整时禁用(engine_factory, missing):
    engine = engine_factory(missing)
    assert engine.track_b.enabled is False
    assert asyncio.run(engine.evaluate_track_b("配置不完整", [])) == 50


# ---------------------------------------------------------------- 缓存与账本

def test_合法响应写入缓存_第二次不再请求(engine_factory):
    """成功结果应进 diskcache，同一消息第二次直接命中缓存。"""
    engine = engine_factory(GOOD_ENV)
    calls = H.patch_request(
        engine, H.make_request_returning(H.llm_payload('{"emotion_target": 66}'))
    )

    first = asyncio.run(engine.evaluate_track_b("缓存测试消息", []))
    second = asyncio.run(engine.evaluate_track_b("缓存测试消息", []))

    assert (first, second) == (66, 66)
    assert calls["n"] == 1, "第二次应命中缓存，实际请求 {0} 次".format(calls["n"])


def test_合法响应写入token账本(engine_factory):
    """带 usage 的成功响应应写一行 TokenLog（module=MOD_TRACK_B）。"""
    from sqlalchemy import func, select

    from models import TokenLog

    engine = engine_factory(GOOD_ENV)
    payload = H.llm_payload(
        '{"emotion_target": 70}',
        usage={"prompt_tokens": 10, "completion_tokens": 3},
    )
    H.patch_request(engine, H.make_request_returning(payload))

    assert asyncio.run(engine.evaluate_track_b("记账测试", [], user_id="u-token")) == 70

    with engine.Session() as session:
        statement = select(func.count()).select_from(TokenLog).where(
            TokenLog.user_id == "u-token"
        )
        count = int(session.execute(statement).scalar_one())
        row = session.execute(
            select(TokenLog).where(TokenLog.user_id == "u-token")
        ).scalars().first()

    assert count == 1
    assert row.module == "MOD_TRACK_B"
    assert row.prompt_tokens == 10
    assert row.completion_tokens == 3
