#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

# Prefer the project-local platform-tools downloaded during setup.
LOCAL_ADB_DIR="$ROOT_DIR/work/android-tools/platform-tools"
if [ -x "$LOCAL_ADB_DIR/adb" ]; then
  export PATH="$LOCAL_ADB_DIR:$PATH"
fi

if [ ! -x .venv/bin/python ]; then
  "$ROOT_DIR/bootstrap_mac.sh"
fi

"$ROOT_DIR/.venv/bin/python" preflight.py
exec "$ROOT_DIR/.venv/bin/python" runner.py "$@"
