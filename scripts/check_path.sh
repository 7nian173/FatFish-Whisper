#!/usr/bin/env bash
# 双仓物理隔离门禁（宪法 §12.4）
#
# 研发区（本仓库）是唯一施工区；新仓库 E:\AI Projects\FatFish-Whisper 是只读交付区。
# 本脚本扫描研发区源码 / 测试 / 脚本 / CI 配置中出现的新仓库路径字面量，命中即失败。
#
# 判据：任何一处出现 "AI Projects" 都算命中——只读交付区不应在研发代码里以任何形式
# 被引用（连注释里也不该有，因为一旦有人照着注释补一行 open(...)，就成真写入了）。
#
# 例外：本脚本自身（被禁字面量在脚本里用变量拼装，源码中不出现完整串）。
#
# 扫描面为 0 时判 FAIL，不判 PASS——扫描面为 0 时的 PASS 不是绿，是没在看（铁律 1）。
#
# 退出码：0 = 干净；1 = 命中或扫描面为 0。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF_REL="scripts/check_path.sh"

# 被禁字面量拆成两段，避免本文件自身在源码里写出完整串。
NEEDLE="AI"" Projects"

cd "$ROOT"

FILES=()
while IFS= read -r f; do
  [ -n "$f" ] || continue
  [ "$f" = "$SELF_REL" ] && continue
  FILES+=("$f")
done < <(
  find sys-mcp-whisper/packages sys-mcp-whisper/apps sys-mcp-whisper/tests scripts \
    -type f \( -name '*.py' -o -name '*.sh' -o -name '*.yml' -o -name '*.yaml' \
                -o -name '*.toml' -o -name '*.ini' -o -name '*.cfg' \) \
    -not -path '*/__pycache__/*' \
    2>/dev/null | sort
)

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "check_path [FAIL] 扫描面为 0，无法判定"
  exit 1
fi

HITS=0
for f in "${FILES[@]}"; do
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    echo "  $f:$hit"
    HITS=$((HITS + 1))
  done < <(grep -n -F "$NEEDLE" "$f" 2>/dev/null || true)
done

if [ "$HITS" -gt 0 ]; then
  echo "check_path [FAIL] 研发区代码中引用了只读交付区路径（宪法 §12）"
  echo "check_path [FAIL] 共 $HITS 处命中；施工路径必须在研发区 30_沙盒区"
  exit 1
fi

echo "check_path [PASS] 扫描 ${#FILES[@]} 个文件，零处引用只读交付区"
exit 0
