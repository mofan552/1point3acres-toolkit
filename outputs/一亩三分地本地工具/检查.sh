#!/bin/sh
# macOS / Linux 离线检查入口，对应 检查.cmd。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../../work/cf-probe-venv/bin/python"
export PYTHONUTF8=1
exec "$PY" "$DIR/check.py" "$@"
