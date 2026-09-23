#!/bin/sh
# macOS / Linux 入口，对应 运行.cmd。无参数时查询状态。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../../work/cf-probe-venv/bin/python"
export PYTHONUTF8=1
[ $# -eq 0 ] && exec "$PY" "$DIR/cli.py" status
exec "$PY" "$DIR/cli.py" "$@"
