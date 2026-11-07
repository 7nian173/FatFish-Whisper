#!/usr/bin/env python
r"""架构机检：组合根唯一性 + 旧模块隔离（TASK-007）。

2026-11-05（task-FFW-P3-00）迁移：扫描范围从 Nov 3 的四条旧包目录切换到
sys-mcp-whisper 主代码树。旧树目录仍留在磁盘上（历史代码，删除需人类单独授权），
但本脚本不再扫描它们。

两条规则，全部用 ast 解析源码（不做正则文本匹配 —— 注释与字符串会误报）：

  1. composition-rule  组合根唯一性
     LegacyEvaluator / LegacyHits / SqliteHormoneStore / SystemClock 的**实例化**
     （ast.Call 的被调名，ast.Name.id 或 ast.Attribute.attr）只允许出现在
     sys-mcp-whisper/apps/composition/composition.py（COMPOSITION_FILE，P4 交付）。
     该文件尚未建立时输出 [skip]（不是 PASS——对不存在的文件判 PASS 是假绿；
     也不是 FAIL——没写代码不算违规）。
     白名单路径是硬编码常量 COMPOSITION_FILE（见 check_imports.py），**不可配置**：
     组合根路径本身是架构契约，若允许配置，改一行配置就能绕过检查。

  2. legacy-isolation  旧模块隔离
     扫描顶层模块名为 old\d+ 的 import（ast.Import / ast.ImportFrom，跳过相对 import）；
     命中文件若不在 sys-mcp-whisper/packages/legacy_bridge/ 下即违规 —— 旧模块在新树的
     唯一合法归属是 legacy_bridge（P3 交付：旧实现被隔离封装在桥内，主树其余部分
     只许通过 ports 协议访问）。迁移前旧树语义是「只能被旧适配层 import」，
     迁移后主树等价物是 packages/legacy_bridge/。

     判定范围**只有** sys-mcp-whisper/packages + sys-mcp-whisper/apps + scripts/
     （见 LEGACY_SCAN_ROOTS）。为什么不是全仓：旧模块隔离约束的是主代码树的
     依赖图——只有树内代码才可能把 old0003 带进运行时依赖链。工作区根下的临时
     验证脚本、镜像副本、旧四包目录（已冻结的历史代码）不属于主树，
     它们 import 旧模块不构成主树架构违规。

     包外文件不判违规，但**保留一次兜底扫描**，命中降级为 WARN 行（不影响退出码）：
     包外脚本 import 旧模块说明旧模块的依赖正在向主树之外扩散，值得提示，但不应阻断构建。

输出：
  composition-rule PASS / FAIL / [skip]
  composition-rule FAIL <文件>:<行> <类名>() 必须只在 composition.py 实例化[; ...]
  legacy-isolation PASS
  legacy-isolation FAIL <文件>:<行> import <模块名>（旧模块只能被 legacy_bridge import）[; ...]
  legacy-isolation WARN <文件>:<行> import <模块名>（包外文件，不影响退出码）[; ...]
全 PASS rc=0，任一 FAIL rc=1；WARN 行不改变 rc。

与 check_imports.py 的关系：规则常量与 ast 辅助函数从 check_imports 直接复用，
保证两个脚本对同一条架构规则只有**一份**定义，不会各自漂移。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# 同目录导入：保证与 check_imports.py 共用同一份规则定义
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_imports import (
    COMPOSITION_FILE,
    PACKAGES,
    ROOT,
    collect_imports,
    iter_py_files,
    scan_composition,
)

LEGACY_PATTERN = re.compile(r"old\d+")

# 扫描时跳过的目录（虚拟环境 / 缓存 / VCS，不是架构代码）
SKIP_DIRS = {".venv", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".snapshots"}

# 旧模块在新树的唯一合法归属：packages/legacy_bridge/（P3 交付）。
# 迁移前（旧树）语义是「只能被旧适配层 import」；迁移后主树的等价物是
# legacy_bridge —— 旧实现的隔离封装层，主树其余部分只通过 ports 协议访问旧模块。
# 约束的是**包**，不是包内某个子目录：legacy_bridge 的 tests/ 里 import old0003
# 做逐字对照是桥层的核心证据，若只放行主代码子目录会把它误判成越界。
LEGACY_ALLOWED_PREFIX = "sys-mcp-whisper/packages/legacy_bridge/"


#: legacy-isolation 的判定范围：主代码树 packages/apps + scripts/。
#: 只有这些目录里的代码属于主树架构的依赖图，旧模块隔离约束才适用。
#: sys-mcp-whisper 自带独立 .git（历史遗留），_is_skipped 的「嵌套 git 仓库」规则
#: 会把它整个跳掉——因此这里对 LEGACY_SCAN_ROOTS 显式列出的目录**豁免**该跳过
#: 规则（见 _is_skipped 的 explicit_scope 参数）：门禁既然迁移到了主树，
#: 就必须真的扫主树，不能被「这是个嵌套仓库」整树放过。
LEGACY_SCAN_ROOTS: tuple[str, ...] = (
    "sys-mcp-whisper/packages",
    "sys-mcp-whisper/apps",
    "scripts",
)


def _is_skipped(path: Path, *, explicit_scope: bool = False) -> bool:
    """环境 / 缓存 / VCS 目录，以及嵌套的独立 git 仓库，一律不扫。

    ``explicit_scope=True`` 表示路径来自 LEGACY_SCAN_ROOTS 显式列出的目录：
    此时嵌套 git 仓库不再跳过（sys-mcp-whisper 本身就是带独立 .git 的目录，
    若跳过它，主树门禁就名存实亡）。SKIP_DIRS（__pycache__ / .git 内部文件等）
    在显式范围内**仍然**跳过——.git 目录本身不是架构代码。
    """
    if SKIP_DIRS & set(path.parts):
        return True
    if explicit_scope:
        return False
    # 兜底扫描中：嵌套独立 git 仓库不属于扫描目标的架构
    return any(
        (parent / ".git").exists()
        for parent in path.parents
        if parent != ROOT and ROOT in parent.parents
    )


def iter_scope_py_files() -> list[Path]:
    """legacy-isolation 的**判定**范围：LEGACY_SCAN_ROOTS 下全部 .py。

    收窄理由见模块 docstring：旧模块隔离约束的是主代码树的依赖图，
    包外的临时脚本不在任何包的依赖链里，把它们判违规只会制造噪音。
    """
    files: list[Path] = []
    for root_name in LEGACY_SCAN_ROOTS:
        root = ROOT / root_name
        if not root.is_dir():
            continue
        files.extend(
            p for p in root.rglob("*.py") if p.is_file() and not _is_skipped(p, explicit_scope=True)
        )
    return sorted(set(files))


def iter_out_of_scope_py_files() -> list[Path]:
    """包外兜底范围：工作区根下**不属于** LEGACY_SCAN_ROOTS 的 .py。

    这些文件不参与判定（命中只报 WARN），但保留扫描以便发现旧模块依赖向包外扩散。
    """
    scope_dirs = {ROOT / name for name in LEGACY_SCAN_ROOTS}
    return sorted(
        p
        for p in ROOT.rglob("*.py")
        if p.is_file()
        and not _is_skipped(p)
        and not any(scope in p.parents for scope in scope_dirs)
    )


def check_composition_rule() -> list[str]:
    """规则 1：具体适配器只允许在唯一组合根实例化。

    范围与 check_imports.py 一致：PACKAGES 各包目录下全部 .py（不是全仓）。
    """
    violations: list[str] = []
    for pkg_dir_name, _module_name in PACKAGES:
        for path in iter_py_files(ROOT / pkg_dir_name):
            rel = path.relative_to(ROOT).as_posix()
            if rel == COMPOSITION_FILE:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError) as exc:
                violations.append(f"{rel}:0 <unparsable: {exc.__class__.__name__}>")
                continue
            for lineno, name in scan_composition(tree):
                violations.append(
                    f"{rel}:{lineno} {name}() 必须只在 composition.py 实例化"
                )
    return violations


def _legacy_imports_in(path: Path) -> tuple[str, list[str], list[str]]:
    """解析单个文件，返回 (相对路径, 旧模块 import 描述, 解析失败描述)。"""
    rel = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return rel, [], [f"{rel}:0 <unparsable: {exc.__class__.__name__}>"]
    hits = [
        f"{rel}:{lineno} import {modname}"
        for lineno, modname in collect_imports(tree)
        if LEGACY_PATTERN.fullmatch(modname.split(".")[0])
    ]
    return rel, hits, []


def check_legacy_isolation() -> tuple[list[str], list[str]]:
    r"""规则 2：旧模块（old\d+）只允许被 legacy_bridge/ 下的文件 import。

    返回 (violations, warnings)：
      * violations —— 主树 + scripts/ 内的旧模块 import（或解析失败）→ 决定 rc
      * warnings   —— 包外文件的旧模块 import → 只提示，不决定 rc
    """
    violations: list[str] = []
    warnings: list[str] = []

    # 1) 判定范围：LEGACY_SCAN_ROOTS（主树 packages/apps + scripts/）
    for path in iter_scope_py_files():
        rel, hits, unparsable = _legacy_imports_in(path)
        violations.extend(unparsable)
        if rel.startswith(LEGACY_ALLOWED_PREFIX):
            continue
        violations.extend(f"{h}（旧模块只能被 legacy_bridge import）" for h in hits)

    # 2) 兜底范围：包外文件，命中降级为 WARN（**不** append 到 violations）
    for path in iter_out_of_scope_py_files():
        _rel, hits, _unparsable = _legacy_imports_in(path)
        warnings.extend(f"{h}（包外文件，不影响退出码）" for h in hits)

    return violations, warnings


def report(name: str, violations: list[str]) -> bool:
    """打印一行结论；返回是否违规。"""
    if violations:
        print(f"{name} FAIL " + "; ".join(violations))
        return True
    print(f"{name} PASS")
    return False


def warn(name: str, warnings: list[str]) -> None:
    """打印一行 WARN；**无返回值**——WARN 不参与退出码计算，这是刻意的。

    包外脚本 import 旧模块不属于架构违规（它们不在任何包的依赖图里），
    只提示、不阻断，避免架构检查退化成工作区整洁度检查。
    """
    if warnings:
        print(f"{name} WARN " + "; ".join(warnings))


def main() -> int:
    # 违规文案含中文；Windows 控制台默认 GBK 会乱码，这里固定 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    # 组合根文件 P4 才建立：不存在时输出 [skip]（理由见模块 docstring 规则 1）。
    if (ROOT / COMPOSITION_FILE).is_file():
        failed = report("composition-rule", check_composition_rule())
    else:
        print("composition-rule [skip] apps/composition/composition.py 尚未建立（P4 交付）")
        failed = False

    legacy_violations, legacy_warnings = check_legacy_isolation()
    failed |= report("legacy-isolation", legacy_violations)
    # WARN 在 PASS/FAIL 行之后输出；它不写 failed，故 rc 只由上面两行决定。
    warn("legacy-isolation", legacy_warnings)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
