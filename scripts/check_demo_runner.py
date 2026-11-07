#!/usr/bin/env python
"""机检：P2.5 演示脚本必须能跑通（P2.5 Bug 5）。

`sys-mcp-whisper/tests/observability/run_demo_p25.py` 故意不带 `test_` 前缀，
所以 pytest 永远不收集它 —— 它是「可执行文档」，不是回归防线。代价是：
脚本里任何一个被断言的数字错了，日常 `pytest -q` 依然全绿，而文档继续宣称
这些数字是对的。

本脚本把那条防线补回来，但**不放进 pytest**：每次全量回归为它多付 2-4 秒
（解释器启动 + 四幕演示）不值得，人类裁决 2026-11-06 明确要求走 CI 独立步骤。

用法（在 30_沙盒区 下）：

    python scripts/check_demo_runner.py

退出码：0 = 演示脚本 rc=0 且末行是 ALL ACTS PASSED；非 0 = 失败。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 绝对路径，不靠当前目录碰运气：CI 里工作目录是 30_沙盒区，开发机上可能不是。
HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
DEMO = WORKSPACE / "sys-mcp-whisper" / "tests" / "observability" / "run_demo_p25.py"
PKG_ROOT = WORKSPACE / "sys-mcp-whisper"

TIMEOUT_SECONDS = 120


def main() -> int:
    if not DEMO.is_file():
        print(f"[FAIL] 演示脚本不存在: {DEMO}")
        print("      它由 task-FFW-P25-DOC-N4 交付；若被删除或改名，请同步本机检。")
        return 1

    before = DEMO.read_bytes()

    result = subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=str(PKG_ROOT),
        capture_output=True,
        text=True,
        # 显式 utf-8：Windows 下默认 GBK 解不动中文输出，会把断言失败伪装成
        # UnicodeDecodeError（本会话已被这个坑咬过一次）。
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        env={
            **{k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"},
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
    )

    stdout = result.stdout or ""
    print(stdout, end="" if stdout.endswith("\n") else "\n")

    failures: list[str] = []

    if result.returncode != 0:
        failures.append(f"演示脚本退出码 {result.returncode}（期望 0）")
        if result.stderr:
            print("--- stderr ---")
            print(result.stderr)

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines or lines[-1] != "ALL ACTS PASSED":
        failures.append(f"末行不是 'ALL ACTS PASSED'，实际: {lines[-1] if lines else '<无输出>'}")

    # 演示脚本只读：断言它没有顺手改自己（改了就没法再复现）。
    if DEMO.read_bytes() != before:
        failures.append("演示脚本运行期间改写了自身")

    if failures:
        for item in failures:
            print(f"[FAIL] {item}")
        return 1

    print()
    print("[PASS] demo-runner: 演示脚本 rc=0 且末行为 ALL ACTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
