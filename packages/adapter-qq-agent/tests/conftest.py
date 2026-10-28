"""pytest 全局配置：注入 adapter 包路径，消除测试执行顺序耦合。

背景（F-21）
------------
``packages/adapter-qq-agent/tests/`` 下原本没有 ``conftest.py``，各测试文件
各自内联一段 ``sys.path`` shim：

- ``tests/test_adapter.py:15-18``
- ``tests/test_sanitize.py:11-14``
- ``tests/test_sanitize_integration.py:20-23``

形态统一（``_ADAPTER = os.path.dirname(_HERE)`` + ``sys.path.insert``），但
``tests/test_no_receipt.py`` 没有这段 shim，只能靠 pytest 按文件名字母序先
收集 ``test_adapter.py`` 才能让 ``fatfish_adapter`` 可导入——单跑该文件直接
``ModuleNotFoundError: No module named 'fatfish_adapter'``（collect error）。

修法
----
与 ``packages/core-engine/tests/conftest.py`` 同一模式：在这里统一把 adapter
包目录注入 ``sys.path``，所有测试文件都不再依赖收集顺序。

与 core-engine 的差异
---------------------
core-engine 的 conftest 还做三件额外的事：写 ``persona`` 中性 traits 文件、
设 ``CORE_EXTRA_HOSTS``、导出 ``engine_factory`` 夹具。adapter 侧都不需要——
它只做**纯路径注入**，不 import ``fatfish_adapter``，不写任何环境变量文件，
不引入任何夹具，因此没有除 ``sys.path`` 之外的副作用。

既有 shim 保留
--------------
三个测试文件内的 shim 一律**不动**：它们自带 ``in`` 守卫，重复注入是幂等的，
保留为防御性冗余（单个测试文件被拷走单独执行时仍能工作）。
"""
from __future__ import annotations

import os
import sys

#: 本文件所在目录：packages/adapter-qq-agent/tests
_HERE = os.path.dirname(os.path.abspath(__file__))
#: adapter 包根目录：packages/adapter-qq-agent（其下是 fatfish_adapter/ 包）
_ADAPTER = os.path.dirname(_HERE)

# 纯路径注入：fatfish_adapter 是平铺包，无需 src 布局。in 守卫保证幂等。
if _ADAPTER not in sys.path:
    sys.path.insert(0, _ADAPTER)
