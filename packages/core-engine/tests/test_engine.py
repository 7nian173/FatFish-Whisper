"""补充回归：Track A 契约 / 等级映射 / 模型结构 / 入口可导入 / 主流程。

这些是"阶段二不能把阶段一搞坏"的护栏，也是 main.py / web 看板依赖的兼容层。
"""
from __future__ import annotations

import asyncio
import os

import pytest

import _helpers as H
import models as models_module
from engine import level_of


# ---------------------------------------------------------------- Track A 契约

def test_track_a_风险词命中risk(engine_factory):
    engine = engine_factory({})
    for message in ("我想死", "我不想活了", "活着没意思"):
        result = engine.evaluate_track_a(message)
        assert result.get("risk") is True, "{0} -> {1}".format(message, result)


def test_track_a_转折词命中semantic_conflict(engine_factory):
    engine = engine_factory({})
    result = engine.evaluate_track_a("其实我很难过")
    assert result.get("semantic_conflict") is True, result
    assert "risk" not in result


@pytest.mark.parametrize(
    "message,expected,label",
    [
        ("好难过，好累", 35, "负面（普通）"),
        ("我崩溃了，想哭", 20, "负面（极端）"),
        ("今天真开心", 75, "正面（普通）"),
        ("太棒了，哈哈哈", 90, "正面（极端）"),
        # 零命中不再判为正面（旧契约是 80，会让「没说什么」也被当成开心）
        ("今天天气如何", 50, "中性（无信号）"),
    ],
)
def test_track_a_target(engine_factory, message, expected, label):
    """五档打分：极端负 20 / 普通负 35 / 中性 50 / 普通正 75 / 极端正 90。"""
    engine = engine_factory({})
    result = engine.evaluate_track_a(message)
    assert result.get("target") == expected, "{0}: {1} -> {2}".format(
        label, message, result
    )
    if label.startswith("中性"):
        assert result.get("undefined") is True, result
    else:
        assert result.get("undefined") is False, result


# ---------------------------------------------------------------- 等级映射

@pytest.mark.parametrize(
    "score,expected",
    [
        (0, "CRISIS"), (15, "CRISIS"),
        (16, "L2"), (35, "L2"),
        (36, "L1"), (45, "L1"),
        (46, "L0"), (65, "L0"),
        (66, "P1"), (85, "P1"),
        (86, "P2"), (100, "P2"),
    ],
)
def test_等级映射边界(score, expected):
    """<=30 L2；31..45 L1；>=46 L0。"""
    assert level_of(score) == expected


def test_risk更新落库为CRISIS(engine_factory):
    engine = engine_factory({})
    result = asyncio.run(engine.update_emotion("u-risk", 80, risk=True))
    assert result["emotion_level"] == "CRISIS"
    assert H.get_state(engine, "u-risk")["level"] == "CRISIS"


# ---------------------------------------------------------------- 好感度不被覆盖

def test_更新情绪不覆盖affinity_raw(engine_factory):
    engine = engine_factory({})
    H.seed_state(engine, "u-aff", emotion=50, affinity=42)

    result = asyncio.run(engine.update_emotion("u-aff", 80))

    assert result["affinity_raw"] == 42, "返回值应带原好感度"
    assert H.get_state(engine, "u-aff")["affinity"] == 42, "DB 里的好感度不得被覆盖"


# ---------------------------------------------------------------- 模型结构

def test_models_表结构():
    user_columns = set(models_module.UserState.__table__.columns.keys())
    # I-26 分房改造：新增 chat_scope（复合主键第二列），affinity_raw 拆到全局表。
    # 原断言要求 UserState 含 affinity_raw；改为主键改造后它必须**不在**这里，
    # 否则同一用户每个群一行会让好感度被意外分房（违反「锚点全局」）。
    assert user_columns == {
        "user_id",
        "chat_scope",
        "emotion_current",
        "emotion_level",
        "last_update",
        "last_event_key",
        "arrival_ts",
    }, "UserState 必须是 (user_id, chat_scope) 复合主键 + 乱序水位线与状态字段"

    table = models_module.UserState.__table__
    assert table.c.emotion_current.default.arg == 50
    assert table.c.emotion_level.default.arg == "L0"
    assert table.c.arrival_ts.default.arg == 0
    assert table.c.arrival_ts.server_default.arg.text == "0"
    assert table.c.arrival_ts.nullable is False
    assert list(table.primary_key.columns.keys()) == ["user_id", "chat_scope"], (
        "UserState 必须是复合主键，否则同一个人在不同群无法分房"
    )

    # 好感度拆表后的形状：全局单键，**不许**有 chat_scope
    affinity_columns = set(models_module.UserAffinity.__table__.columns.keys())
    assert affinity_columns == {"user_id", "affinity_raw", "updated_at"}
    assert "chat_scope" not in affinity_columns, "好感度是全局锚点，不得分房"
    assert models_module.UserAffinity.__table__.c.affinity_raw.default.arg == 0

    token_columns = set(models_module.TokenLog.__table__.columns.keys())
    assert "module" in token_columns, "TokenLog 必须有 module 字段"
    assert "module_type" not in token_columns, "旧列名 module_type 不应出现在模型里"
    assert {
        "id",
        "user_id",
        "prompt_tokens",
        "completion_tokens",
        "created_at",
    } <= token_columns


def test_import_models解析到模块文件():
    """models 必须是 ``src/models.py`` 文件，不能是包（包会遮蔽同名模块）。"""
    assert not hasattr(models_module, "__path__"), "models 不应是包"
    normalized = models_module.__file__.replace("\\", "/")
    assert normalized.endswith("/src/models.py"), normalized

    src_dir = os.path.dirname(os.path.abspath(models_module.__file__))
    assert not os.path.isdir(os.path.join(src_dir, "models")), "src/models 目录不应存在"


def test_main可导入():
    """``from main import app`` 必须成功（sys.path 含 src，cwd 无关）。"""
    from main import app

    assert app is not None
    assert getattr(app, "title", "") != ""


# ---------------------------------------------------------------- 兼容层 + 主流程

def test_兼容层属性与reload_config(engine_factory):
    engine = engine_factory(
        {"LLM_API_BASE": "http://127.0.0.1:9/v1", "LLM_MODEL": "deepseek-chat"}
    )
    assert engine.track_b.enabled is False, "缺 Key 时应禁用"
    assert engine.track_b.model == "deepseek-chat"
    assert isinstance(engine.env, dict)

    engine.env["LLM_API_KEY"] = "sk-live-123456789"
    engine.reload_config()

    assert engine.track_b.enabled is True, "补齐 Key 后热更新应启用"


def test_handle_message返回结构(engine_factory):
    """主入口返回结构：actions / inner_os / debug（含 emotion、level、path）。"""
    engine = engine_factory({})

    payload = asyncio.run(engine.handle_message("u-main", "其实我很难过"))

    assert isinstance(payload.get("actions"), list) and payload["actions"]
    assert isinstance(payload.get("inner_os"), str) and payload["inner_os"]
    debug = payload.get("debug")
    assert isinstance(debug, dict)
    for key in ("emotion", "level", "path"):
        assert key in debug, "debug 缺少字段 {0}".format(key)
    assert 0 <= debug["emotion"] <= 100, debug["emotion"]
    assert debug["path"] == "fast", "Track B 未配置时不应走 deep 路径"
    assert isinstance(payload.get("final_prompt"), str)
    assert "---" in payload["final_prompt"]


def test_handle_message风险消息进CRISIS(engine_factory):
    """安全词走主流程：debug 应反映 CRISIS 与 risk 结果。"""
    engine = engine_factory({})

    payload = asyncio.run(engine.handle_message("u-crisis", "我不想活了"))

    assert payload["debug"]["level"] == "CRISIS"
    assert payload["debug"]["crisis"] is True
    assert 0 <= payload["debug"]["emotion"] <= 100


def test_list_states返回列表(engine_factory):
    engine = engine_factory({})
    asyncio.run(engine.update_emotion("u-list", 80))

    rows = engine.list_states(limit=10)

    assert isinstance(rows, list)
    assert any(row["user_id"] == "u-list" for row in rows)
    sample = next(row for row in rows if row["user_id"] == "u-list")
    assert set(sample.keys()) >= {"user_id", "emotion", "affinity", "level", "last_update"}
