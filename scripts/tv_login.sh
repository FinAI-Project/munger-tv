#!/bin/bash
# 本机打开浏览器登录 TradingView。验证码由你点。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif [[ -x "$HOME/Projects/munger/.venv/bin/python" ]]; then
  PY="$HOME/Projects/munger/.venv/bin/python"
else
  echo "还没有 Playwright。"
  echo "先执行：python3 -m venv .venv && .venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium"
  exit 1
fi
cd "$ROOT"
exec "$PY" "$ROOT/scripts/tv_login.py" "$@"
