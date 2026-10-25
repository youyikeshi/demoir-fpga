#!/usr/bin/env bash
# PC 端接收程序的启动脚本（自动带上工作区 venv 的 PYTHONPATH）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(cd "$HERE/../.." && pwd)"
SP="$WS/env/receiver/lib/python3.10/site-packages"

if [ ! -d "$SP" ]; then
  echo "缺少依赖目录: $SP"
  echo "请先执行："
  echo "  python3 -m venv --without-pip $WS/env/receiver"
  echo "  python3 -m pip install --target $SP opencv-python numpy"
  exit 1
fi

export PYTHONPATH="$SP${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$HERE/receiver.py" "$@"
