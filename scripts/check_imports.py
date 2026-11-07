#!/usr/bin/env python
r"""依赖方向体检：用 ast 解析 sys-mcp-whisper 主代码树下全部 .py，逐条判定 import 是否越界。

2026-11-05（task-FFW-P3-00）迁移：PACKAGES 从 Nov 3 的四条旧包目录
（详见 git 历史/门禁失效核查报告-20261105.md）整体切换到
P1/P2 真实主代码树 30_沙盒区/sys-mcp-whisper/。旧树目录仍在磁盘上（历史代码，
删除需人类单独授权），但本脚本不再扫描它们——门禁只对活代码树生效。

规则（唯一真相，来自 TASK-000 / TASK-002 / TASK-006 / TASK-023 与 P3 卡裁决）：
  packages.core.domain  显式白名单 + 禁止上层（engine/legacy_bridge/apps）与
                        old 前缀旧模块 / sqlite3
  packages.core.engine  显式白名单（见下方规则 0），白名单外一律禁止（含全部 stdlib），
                        另禁 packages.legacy_bridge / apps / old 前缀 / sqlite3 /
                        os / socket / requests
  packages.core.ports / packages.legacy_bridge
                        2026-11-07 起**进入** STRICT_PACKAGES（两包已有真实代码，
                        「当前全空所以暂缓加表」的窗口期结束）：各自一张显式白名单，
                        白名单外一律 FAIL —— ``import sqlite3`` / ``os`` / ``requests``
                        从「靠 stdlib 放行蒙混过关」变成硬 FAIL。
  apps                  同样不进 STRICT_PACKAGES，但有一条专属放行（P3-GATE）：
                        允许 import packages.core.domain.* 与 packages.core.ports.*，
                        禁止 packages.core.engine.*。
                        理由：DTO 是 Inbound 边界对象定义，必须依赖 Core 的 Pydantic
                        契约；Outbound 适配器实现 Core 的端口协议。两者都是六边形
                        架构里「适配器知晓领域」的合法方向（依赖指向内部）。
                        engine 是应用服务层，适配器绕过 UseCase 直连 engine 等于把
                        编排权交给传输层，会话状态机与事务边界会被拆散——这条是
                        红线，不是「暂未放开」。
                        判定用**完整模块名前缀**（== prefix 或 prefix + "."），
                        不是 startswith(prefix)，否则 packages.core.domainXYZ 会被
                        误放行。
                        **P3-GATE2 起 engine 部分改为文件路径级判定**（人类 m15688/m15689
                        裁决 U-G2），见下条。
  apps 对 engine（P3-GATE2）
                        与 domain/ports 不同，``apps`` 对 ``packages.core.engine.*``
                        的放行**不看模块名，只看发起 import 的文件路径**：仅
                        ``apps/use_cases/`` 与 ``apps/composition/`` 下的文件放行，
                        其余 apps 子目录（含 ``apps/adapters/inbound`` /
                        ``apps/adapters/outbound`` / ``apps/entrypoints``）一律禁止。
                        人类裁决原文：「✅ 允许：apps/use_cases/ 和 apps/composition/
                        下的文件 import packages.core.engine.*。❌ 严禁：
                        apps/adapters/inbound/ 和 apps/adapters/outbound/ 下的文件
                        import packages.core.engine.*（这条红线不能破，否则传输层会
                        绕过 UseCase 直连引擎）。」
                        为什么两类不能共用一个判据：``apps/`` 内部并非铁板一块 ——
                        ``apps/use_cases`` 与 ``apps/composition`` 是编排层，职责就是
                        把 engine 纯函数串成主链路；``apps/adapters/*`` 是传输层边界，
                        直连 engine 等于把编排权交给传输层。同样一句
                        ``import packages.core.engine.dynamics``，在两个目录里性质完全
                        相反，只靠模块名区分不了，必须把「是哪个文件在 import」纳入
                        判据。若只是把 ``packages.core.engine`` 塞进
                        ``APPS_ALLOWED_CORE_PREFIXES``，``apps/adapters/**`` 会连带被
                        放行 —— 正好是要禁止的方向。
                        fail-closed：``APPS_ENGINE_ALLOWED_DIR_PREFIXES`` 之外的 apps
                        子目录**不放行** engine；新增目录必须显式登记，未登记即拒绝。

判定方式：
  1. 用 ast 遍历 Import / ImportFrom 节点（不做正则匹配，避免注释与字符串误报）。
  2. 相对 import（level > 0）视为包内自引用，跳过。
  3. 第三方白名单 = 该包 pyproject.toml 里 [project].dependencies 声明的名字，
     再加上 EXTRA_ALLOW；标准库用 sys.stdlib_module_names 判定。
     sys-mcp-whisper 是单 pyproject 树（根上唯一一份，声明 pydantic），
     declared_deps 沿包目录向上找最近的 pyproject.toml 后返回同一份声明。
  4. 禁止项按**首段或全名**做正则全匹配（old 前缀旧模块如 old0003 / old0007 一并覆盖）。
     2026-11-07 修复：此前只匹配首段，``packages\.core\.engine`` 这类多段模式把首段
     ``packages`` 拿去 fullmatch 永不命中，整条规则是装饰性的；同时 FORBIDDEN 与
     check_composition 的 old 前缀模式都多写了一层反斜杠，把「数字」写成了
     「字面反斜杠 + d」，永远匹配不到 old0002。两类回归都已被
     tests/adapters/test_gate_strict_containment.py 的用例钉住。

严格包的硬约束扫描（TASK-006 / TASK-016 / TASK-023，对 STRICT_PACKAGES 生效）：
  STRICT_PACKAGES = {packages.core.domain, packages.core.engine,
                     packages.core.ports, packages.legacy_bridge}
  —— 四个「零 IO 纯函数」包（domain 纯数据形状 / engine 纯数学与拼装 /
  ports 只有 Protocol 声明 / legacy_bridge 只有正则校验这类纯函数）。
  0. import 显式白名单：**按包查表** ALLOWED_TOP，四包各一张，绝不共用：
     domain = __future__ / json / dataclasses / typing / datetime / pydantic / types；
     engine = __future__ / dataclasses / typing / datetime / pydantic / math；
     ports  = __future__ / collections / typing（``collections.abc`` 的首段是 collections）；
     legacy_bridge = __future__ / re / typing。
     旧模块 old0002 / old0003 **故意不在 legacy_bridge 白名单里**：桥包目前只做纯函数
     校验，将来真要接线，必须在 diff 里显式加一行——那次改动会被 review 看见。
     「旧模块允许出现在哪个目录」由 check_composition 的 legacy-isolation 管，
     「这个目录现在准不准 import 它」由本表管，两道门禁各管一段。
     **白名单外一律 FAIL，包括全部 stdlib**。判定排在 STDLIB 放行之前，反转成白名单。
     为什么反转：黑名单在数学上不可能完备。与其不断补名单，不如只放行明确论证过
     是纯的模块，新增依赖必须先改白名单表，改动可见、可 review。
     例外：datetime 在白名单内是允许**类型**（如类型注解写 datetime.datetime），
     仍禁止调用 .now() / .utcnow() / .today() / .time() —— 由规则 C 继续守。
  B. 函数体 IO 扫描：只判裸调用（ast.Name）与 builtins.xxx 形态，
     被调名属于 {open, print, input, exec, eval, compile} 即违规。
     print 亦禁：输出是返回值，不是副作用。re.compile("x") 这类属性方法不再误报。
  C. 时间读取扫描：属性名 ∈ {now, utcnow, today, time, monotonic, perf_counter}，
     且接收者**根名**为 time / datetime 才判违规（datetime.datetime.now() 照样拦住，
     state.now 这类普通属性读取不再误报）；或裸调用名为 time / datetime。
  D. 动态导入 / 反射扫描（TASK-016）：__import__("time")、
     importlib.import_module("os")、getattr(builtins, "open") 三种绕过形态。
     白名单只管得住 import 语句，这三种必须单独在 AST 层识别。
  B/C/D 对 STRICT_PACKAGES 里的每个包生效；
  其余包（目前只有 apps）判定逻辑与输出格式保持不变（P3 起 adapters 合法使用
  sqlite3 / time / open，不能被误杀）。

组合根唯一性（TASK-007，对主代码树全量 .py 生效）：
  LegacyEvaluator / LegacyHits / SqliteHormoneStore / SystemClock 的实例化
  只允许出现在 sys-mcp-whisper/apps/composition/composition.py（P4 才建立，
  check_composition.py 对该文件不存在时输出 [skip]）。
  该路径是硬编码常量 COMPOSITION_FILE，不提供配置项——组合根本身就是架构契约，
  若可配置，改配置即可绕过检查。

输出：每包一行 + 组合根一行
  <包名> PASS
  <包名> FAIL <文件>:<行> <import|违规说明>[; <文件>:<行> ...]
  composition-rule PASS / FAIL / [skip]
全 PASS 退出码 0，任一 FAIL 退出码 1。
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 主代码树根：PACKAGES 与 COMPOSITION_FILE 的路径都相对它解析。
MAIN_TREE = "sys-mcp-whisper"

# 包目录（相对 ROOT）-> 包的 top-level 模块名（二者不一致，必须显式映射）。
# 2026-11-05（task-FFW-P3-00）：从旧四包树整体迁移到 sys-mcp-whisper 单包多模块布局。
# 旧四包目录仍留在磁盘上（历史代码，删除需
# 人类单独授权），但门禁不再扫描它们——对死代码做体检只会制造「全 PASS」的假象。
PACKAGES: list[tuple[str, str]] = [
    (f"{MAIN_TREE}/packages/core/domain", "packages.core.domain"),
    (f"{MAIN_TREE}/packages/core/engine", "packages.core.engine"),
    (f"{MAIN_TREE}/packages/core/ports", "packages.core.ports"),
    (f"{MAIN_TREE}/packages/legacy_bridge", "packages.legacy_bridge"),
    (f"{MAIN_TREE}/apps", "apps"),
]

#: domain 允许的顶层模块 —— **显式白名单**，其余一律 FAIL（含全部 stdlib）。
#:
#: 依据新树实测 import 清单 + 策略允许：
#:   config.py    : __future__ / json / dataclasses / typing
#:   contracts.py : __future__ / datetime / typing / pydantic
#:   state.py     : __future__ / datetime / typing / pydantic
#:   exceptions.py: __future__
#:
#: ``json`` 仅限 ``json.dumps``（config.py 的 config_fingerprint 用它做纯序列化）：
#: 若出现 ``json.load`` / ``open``，由规则 B（IO 扫描）与 FORBIDDEN 兜底拦截，
#: 本白名单只放行模块名，不背书其全部用法。
_DOMAIN_ALLOWED_TOP: frozenset[str] = frozenset(
    {
        "__future__",
        "json",  # 仅限 dumps（见上方注释）；load/open 由规则 B 与 IO 扫描拦截
        "dataclasses",
        "typing",
        "datetime",
        "pydantic",
        # task-FFW-P28-C1：仅在 config.py 用于 MappingProxyType —— 标准库不可变
        # 映射包装，无 IO、无时钟、无网络。裸 dict 是可变模块级状态，任何调用方
        # 都能改掉 PLACEHOLDER_PERSONA_HEAT_MODIFIER 且不留痕迹；这里放行的是
        # 「把只读视图交给模块级常量」这一种用法，不是整个 types 模块。
        "types",
    }
)

#: engine 允许的顶层模块 —— 与 domain **分开一张表**，绝不共用。
#:
#: 依据新树实测 import 清单 + 策略允许：
#:   judge.py     : __future__ / dataclasses / typing / pydantic
#:   dynamics.py  : __future__ / dataclasses / datetime
#:   assembler.py : __future__ / 相对导入
#:
#: ``math`` 在新树暂无人 import（主控实测 2026-11-04），但按既有策略保留：
#: 将来 engine 需要数值运算（如 clamp 的边界计算）时属于本层职责，放行合理且可见。
#:
#: ``datetime`` 在白名单内属**例外**：engine 只允许 import 它的类型（如类型注解里写
#: ``datetime.datetime``），仍禁止调用 ``.now()`` / ``.utcnow()`` / ``.today()`` /
#: ``.time()`` —— 时间必须由参数注入，这条由规则 C 继续守。
_ENGINE_ALLOWED_TOP: frozenset[str] = frozenset(
    {
        "__future__",
        "dataclasses",
        "typing",
        "datetime",
        "pydantic",
        "math",
    }
)

#: ``packages.core.ports``：只有 Protocol 声明，零实现、零 IO。
#:
#: 为什么 ``collections`` 在白名单里：``from collections.abc import Mapping, Sequence``
#: 的 top 段是 ``collections``（不是 ``collections.abc``）—— 白名单按首段判定，
#: 写 ``collections.abc`` 会永远不命中，属于已知的表述陷阱。
_PORTS_ALLOWED_TOP: frozenset[str] = frozenset(
    {
        "__future__",
        "collections",
        "typing",
    }
)

#: ``packages.legacy_bridge``：旧实现的隔离层，当前只有纯函数 ``guards``。
#:
#: **旧模块（old0002 / old0003）故意不在这张表里。** 桥包将来接线时必然要 import
#: 旧模块，但那是「一次显式裁决」，不是「桥包所以默认放行」：真到 P3 接线那年，
#: 必须在这里加一行 ``"old0002"``（或按 check_composition 的旧模块前缀规则处理），
#: 让这次放行出现在 diff 里。check_composition 的 legacy-isolation 规则管的是
#: 「旧模块允许出现在**哪个目录**」，本表管的是「这个目录**现在**准不准 import 它」，
#: 两道门禁各管一段，松绑时两边都得改，这正是我们要的可见性。
_LEGACY_BRIDGE_ALLOWED_TOP: frozenset[str] = frozenset(
    {
        "__future__",
        "re",
        "typing",
    }
)

#: 白名单**按包查表**：四个包的职责各不相同，共用一张必然要么放宽 domain、
#: 要么收紧 engine。缺任何一个键都会被下面的启动断言拦下。
ALLOWED_TOP: dict[str, frozenset[str]] = {
    "packages.core.domain": _DOMAIN_ALLOWED_TOP,
    "packages.core.engine": _ENGINE_ALLOWED_TOP,
    "packages.core.ports": _PORTS_ALLOWED_TOP,
    "packages.legacy_bridge": _LEGACY_BRIDGE_ALLOWED_TOP,
}

#: 执行「白名单反转判定 + B/C/D 扫描」的包。
#:
#: 为什么这四个该严格：它们都是「零 IO 纯函数」包 —— engine 是纯数学 / 拼装，
#: domain 是纯数据形状 + 纯序列化指纹，ports 只有 Protocol 声明，legacy_bridge
#: 目前只有正则校验这类纯函数。
#:
#: 2026-11-07 起 ports / legacy_bridge **进入**本集合（此前为空所以暂缓，现在
#: 两包已有真实代码，P3/P4 施工时按需加表的窗口期结束）。加进来的直接后果：
#: ``import sqlite3`` / ``import os`` / ``import requests`` 这类越权导入在 ports
#: 与 legacy_bridge 里从「靠 stdlib 放行蒙混过关」变成硬 FAIL。
#:
#: apps **仍然不在此集合**：适配器合法使用 sqlite3 / time / open，套用白名单反转
#: 会把它误杀；apps 有自己的前缀级规则（APPS_ALLOWED_CORE_PREFIXES /
#: APPS_ENGINE_ALLOWED_DIR_PREFIXES）与声明依赖放行，本卡不动它。
STRICT_PACKAGES: frozenset[str] = frozenset(
    {
        "packages.core.domain",
        "packages.core.engine",
        "packages.core.ports",
        "packages.legacy_bridge",
    }
)

# 表与集合必须同步：表少一个键，严格分支就取不到白名单（KeyError 或静默全 FAIL）。
# 启动即校验，别等到 CI 才红。
if set(STRICT_PACKAGES) != set(ALLOWED_TOP):
    raise RuntimeError(
        "STRICT_PACKAGES 与 ALLOWED_TOP 不同步："
        f"{sorted(STRICT_PACKAGES)} vs {sorted(ALLOWED_TOP)}"
    )

FORBIDDEN: dict[str, list[re.Pattern[str]]] = {
    # domain 是最底层：禁止依赖任何上层，禁止旧模块与持久化。
    "packages.core.domain": [
        re.compile(r"packages\.core\.engine(\..*)?"),
        re.compile(r"packages\.legacy_bridge(\..*)?"),
        re.compile(r"apps(\..*)?"),
        re.compile(r"old\d+"),
        re.compile(r"sqlite3"),
    ],
    # engine 只许向下依赖 domain（相对导入），禁止横向及以上层、旧模块与 IO。
    "packages.core.engine": [
        re.compile(r"packages\.legacy_bridge(\..*)?"),
        re.compile(r"apps(\..*)?"),
        re.compile(r"old\d+"),
        re.compile(r"sqlite3"),
        re.compile(r"os"),
        re.compile(r"socket"),
        re.compile(r"requests"),
        # 注：stdlib 不再在这里逐个拉黑 —— engine 已改走 _ENGINE_ALLOWED_TOP 显式白名单，
        # 白名单之外的一切（含全部 stdlib）都在 check_package 里被反转判定拦下。
    ],
    # ports / legacy_bridge 自 2026-11-07 起进入 STRICT_PACKAGES：白名单反转已经能
    # 拦住 sqlite3 / os / requests（三者都不在 ALLOWED_TOP 里），这里再显式列一遍是
    # **第二道锁 + 更清楚的报错文案**：将来若有人放宽白名单，FORBIDDEN 仍然拦得住，
    # 而且违规行会直接点名 sqlite3，而不是一句泛泛的「不在白名单内」。
    "packages.core.ports": [
        re.compile(r"old\d+"),
        re.compile(r"sqlite3"),
        re.compile(r"os"),
        re.compile(r"requests"),
        re.compile(r"socket"),
    ],
    "packages.legacy_bridge": [
        # 旧模块本身不在这里拉黑：legacy_bridge 的**唯一职责**就是接旧模块，
        # 旧模块只在哪个目录可 import 由 check_composition 的 legacy-isolation 判定。
        # 本表管的是「桥包自己也不许直接碰 IO」。
        re.compile(r"sqlite3"),
        re.compile(r"os"),
        re.compile(r"requests"),
        re.compile(r"socket"),
    ],
    "apps": [],
}

# 允许“标准库之外、依赖声明之外”的少数通用辅助包。
# 2026-11-05 迁移后：sys-mcp-whisper 是单 pyproject 树（根 pyproject.toml 声明
# pydantic），declared_deps 对每个包都返回同一份声明；EXTRA_ALLOW 目前为空集，
# 留空是为了「显式写空」而不是「忘了迁移」——将来某包需要白名单外的辅助包，
# 在这里加一行并附理由。
EXTRA_ALLOW: dict[str, set[str]] = {
    "packages.core.domain": set(),
    "packages.core.engine": set(),
    "packages.core.ports": set(),
    "packages.legacy_bridge": set(),
    "apps": set(),
}

#: ``apps``（Inbound/Outbound 适配层）允许依赖的 Core 子模块前缀 —— 精确到子模块，
#: 不是「整个 packages.core」。
#:
#: 为什么这条规则必须新写代码而不是改 FORBIDDEN 表：FORBIDDEN 的判定对**首段与
#: 全名**都要试（2026-11-07 起），但全名匹配只在「整个模块名恰好等于模式」时成立，
#: 表达不了「同属 packages.core 下，domain 与 ports 放行、engine 禁止」这种前缀级规则。
#:
#: 为什么适配器依赖 domain 与 ports 是合法的（人类 m14846 要求写明）：
#: ``MCPRequestDTO`` / ``MCPResponseDTO`` 是 **Inbound 边界对象定义**，必须依赖 Core
#: 的 Pydantic 契约才能做字段映射；``SqliteStateStore`` 实现 ``GlobalStatePort`` /
#: ``StateRepositoryPort`` 必须 import 端口协议。这是六边形架构中「适配器知晓领域」
#: 的合法方向（依赖指向内部）。
#:
#: 为什么 engine 必须排除：engine 是**应用服务层**（编排 Judge/Dynamics/Assembler），
#: 适配器绕过 UseCase 直连 engine 等于把编排权交给传输层，会话状态机与事务边界都会
#: 被拆散。这条是本卡的红线，不是「暂未放开」。
APPS_ALLOWED_CORE_PREFIXES: tuple[str, ...] = (
    "packages.core.domain",
    "packages.core.ports",
)

#: ``apps`` 内允许 import ``packages.core.engine.*`` 的**目录**前缀（P3-GATE2）。
#:
#: 为什么必须按目录而不是按模块名判（人类 2026-11-07 裁决 U-G2）：
#: ``apps/`` 内部并非铁板一块 —— ``apps/use_cases`` 与 ``apps/composition`` 是
#: **编排层**，其职责就是把 engine 纯函数串成主链路；而 ``apps/adapters/*`` 是
#: **传输层边界**，直连 engine 等于绕过 UseCase 把编排权交给传输层。同样一句
#: ``import packages.core.engine.dynamics``，在两个目录里的性质完全相反，只能靠
#: 发起 import 的文件路径区分。
#:
#: 值里**不**写 ``sys-mcp-whisper/`` 前缀：真实树的 ``rel`` 带这一层
#: （``ROOT`` = ``30_沙盒区/``），测试假树不带，``_apps_engine_import_allowed``
#: 会先剥掉再看，两份形态因此共用同一张表。
#:
#: fail-closed：未列出的 apps 子目录不放行 engine；新增目录必须显式登记。
APPS_ENGINE_ALLOWED_DIR_PREFIXES: tuple[str, ...] = (
    "apps/use_cases/",
    "apps/composition/",
)

STDLIB = set(sys.stdlib_module_names) | {"__future__", "__main__"}

# --- 组合根唯一性（TASK-007）----------------------------------------------------
# 具体适配器只允许在唯一组合根里实例化。白名单是**硬编码常量**（不可配置）：
# 组合根路径本身属于架构契约，若允许配置，改一行配置就能绕过检查，等于没有检查。
# 2026-11-05（task-FFW-P3-00）：组合根从旧树组合根路径迁到主树
# apps/composition/composition.py；该文件 P4 才建立，main() 对「目标文件不存在」
# 输出 [skip]（不判 PASS 也不判 FAIL——对不存在的文件判 PASS 是假绿）。
COMPOSITION_FILE = "sys-mcp-whisper/apps/composition/composition.py"
COMPOSITION_CLASSES = frozenset(
    {"LegacyEvaluator", "LegacyHits", "SqliteHormoneStore", "SystemClock"}
)

# --- 严格包的扫描规则（B / C / D，对 STRICT_PACKAGES 生效）-----------------------
# 常量名保留 ``ENGINE_`` 前缀：规则最初为 engine 而写，改名会让 blame / 历史记录断档；
# 语义已由「engine 专属」变为「所有零 IO 纯函数包共用」。
ENGINE_IO_CALLS = frozenset({"open", "print", "input", "exec", "eval", "compile"})
ENGINE_TIME_ATTRS = frozenset(
    {"now", "utcnow", "today", "time", "monotonic", "perf_counter"}
)
#: 规则 C 的接收者根名：只有 ``time.*`` / ``datetime.*`` 的属性访问才算读时钟。
ENGINE_TIME_ROOTS = frozenset({"time", "datetime"})


def normalize(dist_name: str) -> str:
    """PEP 503 规范化：distribution 名里的连字符/点统一成下划线小写。"""
    bare = re.split(r"[<>=!~;\[\s]", dist_name.strip(), maxsplit=1)[0]
    return bare.replace("-", "_").replace(".", "_").lower()


def declared_deps(pkg_dir: Path, *, include_extras: bool = False) -> set[str]:
    """读包目录（或其最近祖先）pyproject.toml 的 [project].dependencies。

    ``include_extras=True`` 时同时并入 ``[project.optional-dependencies]`` 的全部组
    （P3-DEPS，人类 m16532 Q1-A）。这样调用方可以显式要求「这个目录可以用 Web 入口
    依赖」，而默认路径仍只返回核心声明 —— 默认值保守，避免任何人无意间放开。

    2026-11-05 迁移：旧树每包自带 pyproject；新树 sys-mcp-whisper 是单 pyproject
    树（根上唯一一份），五个包目录下都没有 pyproject.toml，因此沿祖先向上找，
    找到 30_沙盒区/sys-mcp-whisper/pyproject.toml 为止。所有包共享同一份声明
    （pydantic）——这正是单包多模块布局的声明式依赖语义。
    """
    pyproject = pkg_dir / "pyproject.toml"
    if not pyproject.is_file():
        # 沿祖先向上找最近的 pyproject.toml（最多到 ROOT，即 30_沙盒区/）。
        for parent in pkg_dir.resolve().parents:
            if not parent.is_relative_to(ROOT.resolve()):
                break
            candidate = parent / "pyproject.toml"
            if candidate.is_file():
                pyproject = candidate
                break
        else:
            return set()
        if pyproject == pkg_dir / "pyproject.toml":
            return set()
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", []) or []
    if include_extras:
        # 只在显式要求时并入 extras：默认路径保持「核心声明」语义，谁都能读到的
        # 默认值不叫隔离。extras 是 dict[组名 -> 依赖列表]，这里并**全部**组——
        # 当前只有 web 一组，组名本身不参与判定（判定在 check_package 的目录分流）。
        extras = data.get("project", {}).get("optional-dependencies", {}) or {}
        for group in extras.values():
            deps = deps + list(group or [])
    return {normalize(d) for d in deps}


def iter_py_files(pkg_dir: Path) -> list[Path]:
    """列出包目录下全部 .py（平铺布局，无 src/ 一层）。

    2026-11-05 迁移：旧树是 src-layout（pkg_dir/src/*.py）；新树 packages/core/
    domain/*.py 直接在包目录下，因此改为直接 rglob 包目录本身。
    """
    return sorted(
        p
        for p in pkg_dir.rglob("*.py")
        if "__pycache__" not in p.parts and p.is_file()
    )


def collect_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """返回 [(行号, 被 import 的模块全名)]；相对 import 跳过。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from . / from ..x —— 包内自引用
                continue
            if node.module:
                found.append((node.lineno, node.module))
    return found


def callee_name(node: ast.Call) -> str | None:
    """取被调函数名：open(...) -> 'open'，builtins.open(...) -> 'open'。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def callee_parts(node: ast.Call) -> tuple[str | None, bool]:
    """返回 (被调名, 是否为裸调用)。

    ``is_bare`` 为真表示 ``func`` 是 ``ast.Name``（``open(...)``），
    为假表示是属性访问（``re.compile(...)`` / ``builtins.open(...)``）。
    规则 B 只对裸调用判违规，避免把 ``re.compile`` 这类合法属性方法误杀（F8）。
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id, True
    if isinstance(func, ast.Attribute):
        return func.attr, False
    return None, False


def root_name(node: ast.AST) -> str | None:
    """把 ``datetime.datetime`` / ``time.time`` 这类链式属性拆到最左侧的名字。

    ``datetime.datetime.now()`` 的 ``.now`` 接收者是 ``datetime.datetime``（Attribute），
    若只认 ``ast.Name`` 会漏判；这里回溯到根 ``datetime``，同时保证
    ``state.now``（根为 ``state``）不会误报 —— 这正是 F8 的第二条误报。
    """
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def scan_engine_io(tree: ast.AST) -> list[tuple[int, str]]:
    """规则 B：函数体 IO / 副作用调用扫描。

    只判**裸调用**（``ast.Name``）与 ``builtins.xxx`` 形态。
    ``re.compile("x")`` 这类合法属性方法不再误报（F8 第一条）。
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name, is_bare = callee_parts(node)
        if name not in ENGINE_IO_CALLS:
            continue
        if is_bare or root_name(node.func) == "builtins":
            found.append((node.lineno, f"{name}() violates no-IO/no-side-effect rule"))
    return sorted(found)


def scan_engine_time(tree: ast.AST) -> list[tuple[int, str]]:
    """规则 C：时间读取扫描（时间必须由参数注入）。

    只在接收者根名为 ``time`` / ``datetime`` 时判定，不做全量属性匹配：
    ``state.now`` 是普通属性读取，不该被当成读时钟（F8 第二条）。
    ``datetime.datetime.now()`` 的根仍是 ``datetime``，照样拦住。
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ENGINE_TIME_ATTRS:
            if root_name(node.value) in ENGINE_TIME_ROOTS:
                found.append((node.lineno, f".{node.attr} violates time-injection rule"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ENGINE_TIME_ROOTS
        ):
            found.append((node.lineno, f"{node.func.id}() violates time-injection rule"))
    return sorted(found)


def scan_engine_dynamic_import(tree: ast.AST) -> list[tuple[int, str]]:
    """规则 D：动态导入 / 反射取内建。

    白名单只管得住 ``import`` 语句；以下三种形态能绕过它，必须在 AST 层面单独识别：

    * ``__import__("time")``
    * ``importlib.import_module("os")``（含 ``import_module`` 的任何属性形态）
    * ``getattr(builtins, "open")``

    ``eval`` / ``exec`` / ``compile`` 已由规则 B 覆盖，此处不重复。
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name, _is_bare = callee_parts(node)
        if name == "__import__":
            found.append((node.lineno, "__import__ violates no-dynamic-import rule"))
        elif name == "import_module":
            found.append((node.lineno, "import_module violates no-dynamic-import rule"))
        elif name == "getattr" and node.args and root_name(node.args[0]) == "builtins":
            found.append(
                (node.lineno, "getattr(builtins, ...) violates no-reflection rule")
            )
    return sorted(found)


def scan_composition(tree: ast.AST) -> list[tuple[int, str]]:
    """组合根唯一性：找出具体适配器的实例化点。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = callee_name(node)
            if name in COMPOSITION_CLASSES:
                found.append((node.lineno, name))
    return sorted(found)


def check_composition_rule() -> list[str]:
    """扫描主代码树全量 .py：适配器实例化只允许出现在 COMPOSITION_FILE。"""
    violations: list[str] = []
    for pkg_dir_name, _module_name in PACKAGES:
        for path in iter_py_files(ROOT / pkg_dir_name):
            rel = path.relative_to(ROOT).as_posix()
            if rel == COMPOSITION_FILE:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError):
                continue  # 语法错误已由各包判定报出，避免重复噪音
            for lineno, name in scan_composition(tree):
                violations.append(
                    f"{rel}:{lineno} {name}() 必须只在 composition.py 实例化"
                )
    return violations


def _apps_engine_import_allowed(file_rel_path: str) -> bool:
    """编排层（``apps/use_cases`` / ``apps/composition``）才允许 import engine。

    ``file_rel_path`` 有两种形态，必须都吃得住：
      * 真实树（``check_package`` 里由 ``path.relative_to(ROOT)`` 得到）：
        ``sys-mcp-whisper/apps/composition/use_case.py``
      * 测试假树（tmp 根即 workspace 根）：
        ``apps/composition/use_case.py``

    做法：剥掉开头的 ``sys-mcp-whisper/``（若存在）后按目录前缀比对。
    故意**不**用 ``in`` / ``str.find``：``apps/adapters/inbound/apps/use_cases/x.py``
    这种路径若用子串判定会被误放行。未列出的目录一律拒绝（fail-closed）。
    """
    rel = file_rel_path
    main_prefix = f"{MAIN_TREE}/"
    rel = rel.removeprefix(main_prefix)
    return any(rel.startswith(prefix) for prefix in APPS_ENGINE_ALLOWED_DIR_PREFIXES)


def _is_allowed_core_import(modname: str, file_rel_path: str) -> bool:
    """``apps`` 是否允许 import 该模块（判定 = 目标模块 + 发起 import 的文件路径）。

    * ``packages.core.domain.*`` / ``packages.core.ports.*``：**不看路径**，全 apps 放行
      （P3-GATE 既定，adapters 必须能 import 契约与端口协议）。
    * ``packages.core.engine.*``：**只看路径**，仅 ``APPS_ENGINE_ALLOWED_DIR_PREFIXES``
      下放行（P3-GATE2 新增，适配器仍严禁）。
    * 其余（legacy_bridge / stdlib / 第三方 / 文本前缀兄弟）：一律 False。

    不用 ``str.startswith(prefix)`` 裸判 ``modname``：那会把
    ``packages.core.domainXYZ`` 当成 ``packages.core.domain`` 放行（测试 S4 反证）。
    ``file_rel_path`` 无默认值：省略即 ``TypeError``，不留后门。
    """
    if any(modname == p or modname.startswith(p + ".") for p in APPS_ALLOWED_CORE_PREFIXES):
        return True
    if modname == "packages.core.engine" or modname.startswith("packages.core.engine."):
        return _apps_engine_import_allowed(file_rel_path)
    return False


def check_package(pkg_dir_name: str, module_name: str) -> list[str]:
    pkg_dir = ROOT / pkg_dir_name
    forbidden = FORBIDDEN[module_name]
    # 依赖声明分流（P3-DEPS，人类 m16532 Q1-A）：只有 apps/ 能用 Web 入口依赖
    # （[project.optional-dependencies].web），packages/ 一律只认核心 dependencies。
    # 判据用 module_name == "apps" 而不是路径字符串，与 FORBIDDEN / APPS_ALLOWED_CORE_PREFIXES
    # 的既有口径一致。
    #
    # 影响面只有非 strict 包（ports / legacy_bridge / apps）：packages.core.domain 与
    # packages.core.engine 走下面的 strict 分支、由 ALLOWED_TOP 判定，这条改动不会让它们
    # 多拿到任何东西。正因如此，本卡真正堵住的是 ports 与 legacy_bridge 这两个
    # 「非 strict、无白名单」的口子——别以为改动无效。
    allowed = (
        declared_deps(pkg_dir, include_extras=True)
        if module_name == "apps"
        else declared_deps(pkg_dir)
    ) | EXTRA_ALLOW[module_name] | {module_name}
    # 严格判定（白名单反转 + B/C/D 扫描）对 STRICT_PACKAGES 生效：
    # 2026-11-07 起 = {domain, engine, ports, legacy_bridge}。
    strict = module_name in STRICT_PACKAGES
    # 白名单**按包取**：四个包职责不同，共用一张要么放宽 domain、要么收紧 engine。
    allowed_top = ALLOWED_TOP[module_name] if strict else frozenset()

    violations: list[str] = []
    for path in iter_py_files(pkg_dir):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            violations.append(f"{rel}:0 <unparsable: {exc.__class__.__name__}>")
            continue
        for lineno, modname in collect_imports(tree):
            top = modname.split(".")[0]
            # 同时按**首段**与**全名**判定（2026-11-07 修复）：只判首段时，
            # ``packages\.core\.engine`` 这类多段模式拿首段 ``packages`` 去 fullmatch
            # 永远不命中 —— 它此前是一条装饰性规则，从没拦下过任何东西。
            if any(pat.fullmatch(top) or pat.fullmatch(modname) for pat in forbidden):
                violations.append(f"{rel}:{lineno} {modname}")
                continue
            # apps 专属放行（P3-GATE）：放在 FORBIDDEN 之后 —— 显式禁令永远优先；
            # 放在 stdlib / 声明依赖放行之前 —— 这条规则自己就能判定，不必借道。
            # 只对 module_name == "apps" 生效：其余包的前缀级约束走各自表。
            if module_name == "apps" and _is_allowed_core_import(modname, rel):
                continue
            if strict:
                # 反转判定（TASK-016 / TASK-023）：白名单之外一律违规，**不再依赖 STDLIB 放行**。
                # 这一句必须排在下面的 `top in STDLIB` 之前，否则 stdlib 会先被放行，
                # 等于把黑名单的窟窿原样留着（这正是 TASK-023 要补的洞）。
                if top not in allowed_top:
                    violations.append(f"{rel}:{lineno} {modname}")
                continue
            # 非 strict 包（目前只有 apps）：声明式依赖 + stdlib 放行。
            if top in STDLIB or top in allowed:
                continue
            violations.append(f"{rel}:{lineno} {modname}")
        if strict:
            for lineno, label in scan_engine_io(tree):
                violations.append(f"{rel}:{lineno} {label}")
            for lineno, label in scan_engine_time(tree):
                violations.append(f"{rel}:{lineno} {label}")
            for lineno, label in scan_engine_dynamic_import(tree):
                violations.append(f"{rel}:{lineno} {label}")
    return violations


def main() -> int:
    # 违规文案含中文；Windows 控制台默认 GBK 会乱码，这里固定 UTF-8。
    # errors="replace" 保证重定向到任意管道时也不会因编码异常而崩掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    failed = False
    for pkg_dir_name, module_name in PACKAGES:
        violations = check_package(pkg_dir_name, module_name)
        if violations:
            failed = True
            print(f"{module_name} FAIL " + "; ".join(violations))
        else:
            print(f"{module_name} PASS")

    # 组合根文件 P4 才建立：不存在时输出 [skip]（不是 PASS——对不存在的文件
    # 判 PASS 会制造「组合根已合规」的假绿；也不是 FAIL——没写代码不算违规）。
    if (ROOT / COMPOSITION_FILE).is_file():
        composition = check_composition_rule()
        if composition:
            failed = True
            print("composition-rule FAIL " + "; ".join(composition))
        else:
            print("composition-rule PASS")
    else:
        print("composition-rule [skip] apps/composition/composition.py 尚未建立（P4 交付）")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
