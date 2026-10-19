#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "找不到 $PYTHON_BIN。请先安装 Python 3.9+。" >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("需要 Python 3.9 或更高版本")
PY

cd "$ROOT_DIR"
if [ ! -x .venv/bin/python ]; then
  echo "创建虚拟环境: $ROOT_DIR/.venv"
  "$PYTHON_BIN" -m venv .venv
fi

echo "安装/更新 Python 依赖..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo
echo "依赖安装完成。下一步："
if [ -x "$ROOT_DIR/work/android-tools/platform-tools/adb" ]; then
  echo "  ADB 已准备在项目内：$ROOT_DIR/work/android-tools/platform-tools/adb"
else
  echo "  1. macOS 若尚未安装 ADB：brew install android-platform-tools"
fi
echo "  2. 连接手机并允许 USB 调试"
echo "  3. 执行：.venv/bin/python preflight.py"
