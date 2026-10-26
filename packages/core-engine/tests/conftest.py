"""pytest 全局配置：注入 src 路径 + 提供完全隔离的引擎夹具。

铁律
----
1. **绝不触碰真实 ``packages/core-engine/app.db``**：每个用例都用 ``tmp_path``
   下的独立 db 文件与独立 cache 目录，通过 ``EmotionEngine(db_path=..., cache_dir=...)``
   注入。
2. **绝不发真实外网请求**：Track B 用例要么指向不可达地址（127.0.0.1:9），
   要么 monkeypatch ``TrackB._request``。
3. 每个用例独立：夹具 function 级，用完即关闭 diskcache 与 SQLAlchemy 引擎。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")

# Starlette TestClient 的默认 Host 是 ``testserver``；security 中间件只放行回环 Host，
# 这里把测试主机显式加进允许列表（仅本测试进程内生效，不影响生产默认值）。
os.environ["CORE_EXTRA_HOSTS"] = "testserver,core"

# 性格向量隔离：测试固定用「无偏向」的 0.5，避免读真实 persona.txt 造成用例数值漂移
_NEUTRAL_TRAITS = os.path.join(tempfile.gettempdir(), "ffw_test_traits.json")
with open(_NEUTRAL_TRAITS, "w", encoding="utf-8") as _fh:
    json.dump(
        {"neuroticism": 0.5, "extraversion": 0.5, "agreeableness": 0.5,
         "resilience": 0.5, "source": "test"},
        _fh,
    )
os.environ["FFW_TRAITS_PATH"] = _NEUTRAL_TRAITS

# src 必须在最前：engine.py 内部是 ``from models import ...`` 的平铺导入
for _path in (_SRC, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from engine import EmotionEngine  # noqa: E402  (必须在 sys.path 注入之后)


@pytest.fixture
def engine_factory(tmp_path):
    """返回一个工厂：按需构造使用临时 db / cache 的 ``EmotionEngine``。

    用法::

        engine = engine_factory({...env...})
    """
    created: list[EmotionEngine] = []
    counter = {"n": 0}

    def _factory(env=None):
        counter["n"] += 1
        name = "engine{0}".format(counter["n"])
        engine = EmotionEngine(
            db_path=str(tmp_path / (name + ".db")),
            cache_dir=str(tmp_path / ("cache_" + name)),
            env=dict(env or {}),
        )
        created.append(engine)
        return engine

    yield _factory

    # 清理：Windows 下 diskcache / SQLite 文件句柄需要显式释放
    for engine in created:
        try:
            engine.cache.close()
        except Exception:
            pass
        try:
            engine.db_engine.dispose()
        except Exception:
            pass


@pytest.fixture
def engine(engine_factory):
    """最常用场景：一个未配置 Track B 的干净引擎。"""
    return engine_factory({})


@pytest.fixture
def tmp_engine(engine):
    """``engine`` 的别名（语义更明确：一定是临时 db / cache）。"""
    return engine
