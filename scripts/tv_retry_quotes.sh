#!/bin/bash
# 早班日线失败后，中午和晚上再补一次。已经成功就跳过。
set -u
cd "$(dirname "$0")/.."
PY="${TV_PYTHON:-/usr/bin/python3}"
mkdir -p data
LAST=data/tv_quotes.last_exit
if [[ -f "$LAST" ]] && [[ "$(cat "$LAST")" == "0" ]]; then
  exit 0
fi
LOG=data/tv_daily.log
{
  echo "=== $(date -Is) quotes retry ==="
  "$PY" -u scripts/load_tv_quotes.py --daily
  code=$?
  echo "quotes exit $code"
  echo "$code" > "$LAST"
} >> "$LOG" 2>&1
