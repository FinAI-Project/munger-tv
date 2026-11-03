#!/bin/bash
# 在 28 上每 45 分钟续一次 TradingView 短会话。不弹窗。续不上只记日志，不走密码登录。
set -u
cd "$(dirname "$0")/.."
if [[ -x /home/honghua/Projects/munger/.venv/bin/python ]]; then
  PY="${TV_KEEP_PYTHON:-/home/honghua/Projects/munger/.venv/bin/python}"
elif [[ -x "$HOME/Projects/munger/.venv/bin/python" ]]; then
  PY="${TV_KEEP_PYTHON:-$HOME/Projects/munger/.venv/bin/python}"
else
  PY="${TV_KEEP_PYTHON:-${TV_PYTHON:-/usr/bin/python3}}"
fi
mkdir -p data
LOG=data/tv_keep_alive.log
{
  echo "=== $(date -Is) keep-alive ==="
  "$PY" -u scripts/tv_keep_alive.py
  echo "exit $?"
} >> "$LOG" 2>&1
