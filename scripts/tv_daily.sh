#!/bin/bash
# 每天的增量：先日线，再概览，再财务，再新闻，再文档。前一个跑完才开始下一个。
set -u
cd "$(dirname "$0")/.."
PY="${TV_PYTHON:-/usr/bin/python3}"
mkdir -p data
LOG=data/tv_daily.log
{
  echo "=== $(date -Is) screener ==="
  "$PY" -u scripts/load_tv_screener.py
  echo "screener exit $?"
  echo "=== $(date -Is) quotes ==="
  "$PY" -u scripts/load_tv_quotes.py --daily
  code=$?
  echo "quotes exit $code"
  echo "$code" > data/tv_quotes.last_exit
  echo "=== $(date -Is) overview ==="
  "$PY" -u scripts/load_tv_overview.py --daily
  echo "overview exit $?"
  echo "=== $(date -Is) financials ==="
  "$PY" -u scripts/load_tv_financials.py --daily
  echo "financials exit $?"
  echo "=== $(date -Is) news ==="
  "$PY" -u scripts/load_tv_news.py --daily
  echo "news exit $?"
  echo "=== $(date -Is) documents ==="
  "$PY" -u scripts/load_tv_documents.py --daily
  echo "documents exit $?"
} >> "$LOG" 2>&1
