#!/usr/bin/env bash
# =============================================================================
# ascend_P3.sh —— P3 闭环「飞升」辅助脚本
# =============================================================================
# 用途：把研发区（30_沙盒区）的代码与提纯文档同步进只读交付区（新仓），
#       并在新仓内跑完 P3 验收清单。
#
# 铁律：
#   1. 本脚本只允许**人类主控**执行，子智能体一律不得运行（宪法 §12.1）。
#   2. 脚本不读研发区以外的任何路径做写操作；唯一写入目标是 -t 指定的新仓。
#   3. 每步都打印将要发生什么，并在 §2 覆盖前要求输入 yes 确认。
#   4. 任何一步失败立即退出（set -euo pipefail），绝不"跳过继续"。
#
# 用法：
#   bash ascend_P3.sh --check-only          # 只做体检，不写任何文件（推荐先跑）
#   bash ascend_P3.sh --apply              # 执行覆盖 + 验收（交互确认）
#   bash ascend_P3.sh --apply --yes         # 跳过交互确认（谨慎）
#   bash ascend_P3.sh --apply -s <源目录> -t <目标仓>
# =============================================================================

set -euo pipefail

DEV_ROOT_DEFAULT="E:/AI开发/FatFish-Whisper 开发区"
NEW_REPO_DEFAULT="E:/AI Projects/FatFish-Whisper"

DEV_ROOT="$DEV_ROOT_DEFAULT"
NEW_REPO="$NEW_REPO_DEFAULT"
APPLY=0
ASSUME_YES=0
EXPECTED_PASSED=853
EXPECTED_SCANNED=109

# --- 参数解析 ---------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) APPLY=0; shift ;;
    --apply)      APPLY=1; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    -s|--source)  DEV_ROOT="${2:?--source 需要一个路径}"; shift 2 ;;
    -t|--target)  NEW_REPO="${2:?--target 需要一个路径}"; shift 2 ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（--help 查看用法）" >&2; exit 2 ;;
  esac
done

SANDBOX="$DEV_ROOT/30_沙盒区"
PURIFIED="$DEV_ROOT/00_纯净交付文档"

# --- 小工具 -----------------------------------------------------------------
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_blue()  { printf '\033[36m%s\033[0m\n' "$*"; }
c_warn()  { printf '\033[33m%s\033[0m\n' "$*"; }

step() { echo; c_blue "=== $* ==="; }
ok()   { c_green "  [OK] $*"; }
die()  { c_red "  [FAIL] $*"; exit 1; }

confirm() {
  [ "$ASSUME_YES" = "1" ] && { c_warn "  --yes 已给出，跳过确认：$1"; return 0; }
  printf '\033[33m  %s [yes/NO] \033[0m' "$1"
  read -r ans
  [ "$ans" = "yes" ] || die "用户未确认，终止"
}

# --- 0. 前置体检 ------------------------------------------------------------
step "0. 前置体检"

[ -d "$SANDBOX" ]  || die "研发区沙盒不存在：$SANDBOX"
[ -d "$PURIFIED" ] || die "提纯文档目录不存在：$PURIFIED"
[ -d "$NEW_REPO" ] || die "目标仓库不存在：$NEW_REPO"

# 双仓必须是两个不同的物理路径
real_dev="$(cd "$SANDBOX" && pwd -P)"
real_new="$(cd "$NEW_REPO" && pwd -P)"
[ "$real_dev" != "$real_new" ] || die "源与目标解析到同一路径，拒绝执行"
ok "源：$real_dev"
ok "目标：$real_new"

# 目标必须是 git 仓库（新仓的版本真相源）
[ -d "$NEW_REPO/.git" ] || die "目标不是 git 仓库（缺 .git）——新仓是版本真相源，必须有 Git"
ok "目标含 .git"

# --check-only 时不允许目标目录被本脚本写入，这里只提示
if [ "$APPLY" = "0" ]; then
  c_warn "  当前为 --check-only 模式：只体检，不写任何文件"
fi

# --- 1. 研发区基线体检 ------------------------------------------------------
step "1. 研发区基线（$SANDBOX）"

cd "$SANDBOX"

echo "  --- pytest（沙盒根）---"
set +e
out="$(python -m pytest -q 2>&1)"; rc=$?
set -e
echo "$out" | tail -2
[ $rc -eq 0 ] || die "沙盒根 pytest 失败（rc=$rc）"
echo "$out" | grep -q "${EXPECTED_PASSED} passed" || die "未达 ${EXPECTED_PASSED} passed"
ok "沙盒根：${EXPECTED_PASSED} passed"

echo "  --- pytest（成员树）---"
cd "$SANDBOX/sys-mcp-whisper"
set +e
out="$(python -m pytest -q 2>&1)"; rc=$?
set -e
echo "$out" | tail -2
[ $rc -eq 0 ] || die "成员树 pytest 失败（rc=$rc）"
echo "$out" | grep -q "${EXPECTED_PASSED} passed" || die "未达 ${EXPECTED_PASSED} passed"
ok "成员树：${EXPECTED_PASSED} passed"

# --- 2. 研发区五道门禁 ------------------------------------------------------
step "2. 研发区五道门禁"

cd "$SANDBOX"
for g in check_imports.py check_composition.py check_model_copy.py check_demo_runner.py; do
  set +e
  python "scripts/$g" >/tmp/_ffw_gate.log 2>&1; rc=$?
  set -e
  [ $rc -eq 0 ] || { cat /tmp/_ffw_gate.log; die "$g rc=$rc"; }
  ok "$g rc=0"
done

set +e
bash scripts/check_path.sh >/tmp/_ffw_path.log 2>&1; rc=$?
set -e
tail -1 /tmp/_ffw_path.log
[ $rc -eq 0 ] || die "check_path.sh rc=$rc"
grep -q "扫描 ${EXPECTED_SCANNED} 个文件" /tmp/_ffw_path.log \
  || c_warn "  扫描面不是 ${EXPECTED_SCANNED}（可能新增/删除了被扫文件），请人工确认"
ok "check_path.sh rc=0"

# --- 3. 目标仓待删对象 ------------------------------------------------------
step "3. 目标仓清理清单（噪音）"

TARGETS_TO_REMOVE=(
  "sys-mcp-whisper/tmp_mt6ine_"
  "sys-mcp-whisper/tmpeadzzx26"
  "sys-mcp-whisper/tmpebsdxp5l"
  "sys-mcp-whisper/tmpoqjeybpg"
  "pytest-of-zero"
  "脚本区"
  ".pycache"
  "__pycache__"
  ".pytest_cache"
  ".ruff_cache"
)
found_any=0
for t in "${TARGETS_TO_REMOVE[@]}"; do
  if [ -e "$NEW_REPO/$t" ]; then
    echo "  待删：$t"
    found_any=1
  fi
done
[ "$found_any" = "1" ] || ok "目标仓无噪音残留"

# --- 4. 覆盖（仅在 --apply）-------------------------------------------------
step "4. 覆盖动作"

if [ "$APPLY" = "0" ]; then
  c_warn "  --check-only：跳过全部写入"
  echo
  c_green "体检完成。要真正执行请加 --apply"
  exit 0
fi

confirm "即将【删除】目标仓的 sys-mcp-whisper/ 与 scripts/ 并整树替换，继续？"
confirm "即将【覆盖】目标仓 docs/ 下的 3 份文档，继续？"

# 4.1 先删噪音
for t in "${TARGETS_TO_REMOVE[@]}"; do
  if [ -e "$NEW_REPO/$t" ]; then
    rm -rf "${NEW_REPO:?}/$t"
    echo "  已删：$t"
  fi
done

# 4.2 整树覆盖 sys-mcp-whisper / scripts
for d in sys-mcp-whisper scripts; do
  rm -rf "${NEW_REPO:?}/$d"
  cp -a "$SANDBOX/$d" "$NEW_REPO/$d"
  # 复制的树里不应带缓存
  find "$NEW_REPO/$d" -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name pytest-of-zero \) -prune -exec rm -rf {} + 2>/dev/null || true
  n=$(find "$NEW_REPO/$d" -type f | wc -l | tr -d ' ')
  ok "已覆盖 $d/（$n 文件）"
done

# 4.3 覆盖 docs
for f in 01-Architecture.md 03-Pipeline.md 02-Contracts.md; do
  [ -f "$PURIFIED/$f" ] || die "提纯文档缺失：$PURIFIED/$f"
  cp -f "$PURIFIED/$f" "$NEW_REPO/docs/$f"
  ok "已覆盖 docs/$f"
done

# 4.4 确认根配置没被带过去（研发区独有文件不应出现在新仓）
for f in pytest.ini conftest.py; do
  if [ -e "$NEW_REPO/$f" ]; then
    c_warn "  目标仓出现 $f（研发区独有文件），请人工确认是否需要删除"
  fi
done

# --- 5. 目标仓验收 ----------------------------------------------------------
step "5. 目标仓验收（在新仓内跑）"

cd "$NEW_REPO"

echo "  --- pytest（仓根）---"
set +e
out="$(python -m pytest -q 2>&1)"; rc=$?
set -e
echo "$out" | tail -2
[ $rc -eq 0 ] || die "新仓 pytest（仓根）失败"
echo "$out" | grep -q "${EXPECTED_PASSED} passed" || die "新仓未达 ${EXPECTED_PASSED} passed"
ok "仓根 ${EXPECTED_PASSED} passed"

echo "  --- pytest（成员树）---"
cd "$NEW_REPO/sys-mcp-whisper"
set +e
out="$(python -m pytest -q 2>&1)"; rc=$?
set -e
echo "$out" | tail -2
[ $rc -eq 0 ] || die "新仓 pytest（成员树）失败"
ok "成员树 ${EXPECTED_PASSED} passed"

echo "  --- 五道门禁 ---"
cd "$NEW_REPO"
for g in check_imports.py check_composition.py check_model_copy.py check_demo_runner.py; do
  set +e
  python "scripts/$g" >/tmp/_ffw_gate2.log 2>&1; rc=$?
  set -e
  [ $rc -eq 0 ] || { cat /tmp/_ffw_gate2.log; die "$g rc=$rc"; }
  ok "$g rc=0"
done

set +e
bash scripts/check_path.sh >/tmp/_ffw_path2.log 2>&1; rc=$?
set -e
tail -1 /tmp/_ffw_path2.log
[ $rc -eq 0 ] || die "check_path.sh rc=$rc"
ok "check_path.sh rc=0"

echo "  --- ruff（三口径）---"
for ctx in "." "sys-mcp-whisper" "scripts"; do
  set +e
  out="$(cd "$NEW_REPO" && python -m ruff check "$ctx" 2>&1)"; rc=$?
  set -e
  [ $rc -eq 0 ] || { echo "$out"; die "ruff $ctx rc=$rc"; }
  ok "ruff $ctx"
done

# --- 6. 收尾 ----------------------------------------------------------------
step "6. 全部通过"
c_green "新仓已就绪：${EXPECTED_PASSED} passed + 五道门禁 rc=0 + ruff 三口径全绿"
echo
echo "仍需人工完成的两件事（脚本不做）："
echo "  1. 编辑 .github/workflows/ci.yml："
echo "     - 在 check_model_copy.py 步骤之后插入 check_path.sh 步骤"
echo "     - 注释里的 846 改为 ${EXPECTED_PASSED}"
echo "  2. 确认无误后提交："
echo "       cd \"$NEW_REPO\""
echo "       git add -A && git status --short"
echo "       git commit -m \"feat: P3 端口/适配器/编排层闭环（${EXPECTED_PASSED} passed，五道门禁全绿）\""
