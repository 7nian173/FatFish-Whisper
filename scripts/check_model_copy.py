#!/usr/bin/env python
"""架构机检：禁止无校验的 ``model_copy(update=...)``（task-FFW-P28-C4）。

pydantic v2 的 ``model_copy(update=...)`` **不跑字段校验**：它把新值直接塞进
``__dict__``，``Field(ge=..., le=...)``、``Literal[...]``、类型检查全部静默跳过。
实测（C4 卡）：``TensionState(value=50).model_copy(update={"value": 110})`` 得到
``value=110``，不抛异常，而 ``model_validate({"value": 110})`` 正常报
``Input should be less than or equal to 100``。同一个 API 下，一个绕过模型自己的
边界、另一个不绕过 —— 一旦生产路径用前者写回状态，模型就不再是唯一权威。

因此本脚本把 ``model_copy`` 分成两类：

  * ``model_copy()`` / ``model_copy(deep=True)`` —— **允许**。不带 ``update`` 时
    没有新值进入模型，复制出来的对象一定仍然合法（它是原对象的逐字段副本）。
  * ``model_copy(update={...})`` —— **禁止**。改写用
    ``SomeModel.model_validate(obj.model_dump() | {...})`` 往返一次，让模型自己的
    validator 表态；``engine.reconciliation`` 从一开始就是这么写的，是仓库里的先例。

白名单 ``ALLOWED_UPDATE_SITES`` 是硬编码常量，**不可配置**：允许某处绕过校验，
必须是一次有记录的架构裁决，而不是改一行配置就能做到的事。白名单元素形如
``"sys-mcp-whisper/packages/core/engine/xxx.py:42"``（相对 ROOT 的 POSIX 路径 + 行号）。

扫描范围：``sys-mcp-whisper/packages`` + ``sys-mcp-whisper/apps``。
``tests/`` 不在范围内 —— 测试里构造非法对象来验证拒绝行为是正常手段，
把它纳入判违规会逼着测试绕开边界。

输出：
  model-copy-rule PASS
  model-copy-rule FAIL <文件>:<行> model_copy(update=...) 绕过字段校验，请改用
      model_validate(obj.model_dump() | {...})
全 PASS rc=0，任一 FAIL rc=1。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 允许 ``model_copy(update=...)`` 的站点（相对 ROOT 的 POSIX 路径 + 行号）。
#: 目前为空：仓库里每一处写回都已改走 ``model_validate`` 往返。新增条目必须
#: 附带一句为什么该处可以免检 —— 一个只有路径和行号的条目，下一个人无法判断
#: 它是否还成立。
ALLOWED_UPDATE_SITES: frozenset[str] = frozenset()

#: 扫描根（相对 ROOT）。tests/ 刻意不在内，理由见模块 docstring。
SCAN_ROOTS: tuple[str, ...] = (
    "sys-mcp-whisper/packages",
    "sys-mcp-whisper/apps",
)

#: 与 check_imports.py / check_composition.py 保持一致的跳过目录。
SKIP_DIRS = {".venv", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".snapshots"}


def iter_py_files(root: Path) -> list[Path]:
    """按 SKIP_DIRS 过滤后返回 root 下全部 .py。

    嵌套 git 仓库的跳过规则**必须带 ``ROOT in parent.parents`` 限定**，与
    ``check_composition._is_skipped`` 保持同一判据。少了这半个条件，凡是工作区
    根（``.../FatFish-Whisper 开发区``）被 git 追踪的，每个文件的祖先里都存在
    那个根级 ``.git``，于是全部被当成"嵌套仓库"跳过 —— 扫描面归零、门禁恒 PASS。

    真实事故（task-FFW-P28-C4 复验）：本函数曾写成无 ROOT 限定的
    ``any((parent / ".git").exists() for parent in path.parents if parent != ROOT)``，
    实测 ``iter_py_files`` 对 packages/ 与 apps/ 均返回 0 个文件，而主树实际有
    25 个 .py；注入 ``snap.model_copy(update={...})`` 到 delay.py 后门禁仍 PASS。
    """
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        # 只跳过 ROOT 内部嵌套的独立仓库；ROOT 之外的祖先 .git 不属于扫描目标。
        if any(
            (parent / ".git").exists()
            for parent in path.parents
            if parent != ROOT and ROOT in parent.parents
        ):
            continue
        files.append(path)
    return sorted(files)


def _is_model_copy_call(node: ast.Call) -> bool:
    """``x.model_copy(...)`` —— 只认属性名为 model_copy 的调用。"""
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "model_copy"


def find_unvalidated_updates(tree: ast.AST, rel: str) -> list[str]:
    """返回该文件里 ``model_copy(update=...)`` 的违规描述（空表示干净）。"""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_model_copy_call(node)):
            continue
        if not any(kw.arg == "update" for kw in node.keywords):
            continue
        site = f"{rel}:{node.lineno}"
        if site in ALLOWED_UPDATE_SITES:
            continue
        violations.append(
            f"{site} model_copy(update=...) 绕过字段校验，"
            "请改用 model_validate(obj.model_dump() | {...})"
        )
    return violations


def scan() -> list[str]:
    """扫全部目标文件，返回违规列表。"""
    violations: list[str] = []
    for root_name in SCAN_ROOTS:
        for path in iter_py_files(ROOT / root_name):
            rel = path.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError) as exc:
                violations.append(f"{rel}:0 <unparsable: {exc.__class__.__name__}>")
                continue
            violations.extend(find_unvalidated_updates(tree, rel))
    return violations


def main() -> int:
    # 违规文案含中文；Windows 控制台默认 GBK 会乱码，这里固定 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 非 TTY 环境
        pass

    violations = scan()
    if violations:
        print("model-copy-rule FAIL " + "; ".join(violations))
        return 1
    print("model-copy-rule PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
