#!/bin/bash
# 全量：先日线，再概览，再强制重拉财务。新闻和文档库里已经齐的，只补增量。
set -u
cd "$(dirname "$0")/.."
PY="${TV_PYTHON:-/usr/bin/python3}"
mkdir -p data
LOG=data/tv_full.log
{
  echo "=== $(date -Is) quotes full ==="
  "$PY" -u scripts/load_tv_quotes.py --full --force
  echo "quotes exit $?"
  echo "=== $(date -Is) overview full ==="
  "$PY" -u scripts/load_tv_overview.py --full --force
  echo "overview exit $?"
  echo "=== $(date -Is) financials full ==="
  "$PY" -u scripts/load_tv_financials.py --full --force
  echo "financials exit $?"
  echo "=== $(date -Is) news daily ==="
  "$PY" -u scripts/load_tv_news.py --daily
  echo "news exit $?"
  echo "=== $(date -Is) documents daily ==="
  "$PY" -u scripts/load_tv_documents.py --daily
  echo "documents exit $?"
} >> "$LOG" 2>&1
