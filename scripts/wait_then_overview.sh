#!/bin/bash
# 等财务这一轮结束，再全量拉概览，避免两条登录通道一起打。
set -u
cd "$(dirname "$0")/.."
PY="${TV_PYTHON:-/usr/bin/python3}"
while true; do
  if pgrep -af python3 | grep -F 'load_tv_financials.py' | grep -vq grep; then
    sleep 30
    continue
  fi
  break
done
echo "=== $(date -Is) overview full after financials ==="
exec "$PY" -u scripts/load_tv_overview.py --full --force
