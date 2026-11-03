#!/usr/bin/env python3
"""把 TradingView 文档页上的历史文件写入 pangu。

列表和文字稿走公开接口 news-mediator，不翻网页。
PDF 原件该接口拒绝下载，只记下「有这份 PDF」，不存文件本身。
文字稿和摘要会把正文存下来。

一只一只拉，文档正文之间再停一会儿。被限流就停，不反复猛打。

用法：
  .venv/bin/python scripts/load_tv_documents.py
  .venv/bin/python scripts/load_tv_documents.py --only AAPL.US
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "data" / "tv_documents.lock"
LIST_URL = "https://news-mediator.tradingview.com/public/doc-screener/v1/documents"
VIEW_URL = "https://news-mediator.tradingview.com/public/doc-screener/v1/views/"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://cn.tradingview.com",
    "Referer": "https://cn.tradingview.com/",
}


def load_financials():
    path = Path(__file__).with_name("load_tv_financials.py")
    spec = importlib.util.spec_from_file_location("load_tv_financials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIN = load_financials()


class StopRun(RuntimeError):
    pass


def ast_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(ast_text(item) for item in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    body = ast_text(node.get("children"))
    if kind == "p":
        text = body.strip()
        return (text + "\n") if text else ""
    if kind == "*":
        text = body.strip()
        return ("- " + text + "\n") if text else ""
    return body


def match_listing(rows: list[dict], code: str, source_id: str) -> str | None:
    hits = []
    for item in rows:
        if str(item.get("symbol") or "") != code:
            continue
        if str(item.get("source_id") or "") != source_id:
            continue
        if item.get("type") not in (None, "", "stock", "dr"):
            continue
        hits.append(item)
    if not hits:
        return None
    stocks = [item for item in hits if item.get("type") in (None, "", "stock")]
    pool = stocks or hits
    primary = [item for item in pool if item.get("is_primary_listing")]
    pool = primary or pool
    prefixes = {(item.get("prefix") or item.get("source_id") or "") for item in pool}
    if len(prefixes) != 1:
        return None
    prefix = prefixes.pop()
    if not prefix:
        return None
    return "%s:%s" % (prefix, code)


def ensure_tables(env: dict[str, str]) -> None:
    fin = FIN
    fin.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_document
        (
            ticker String,
            tv_symbol String,
            doc_id String,
            correlation_id String,
            category String,
            category_title String,
            event_kind String,
            form_id String,
            form_title String,
            fiscal_year Nullable(Int16),
            fiscal_period String,
            title String,
            reported_at DateTime64(3, 'UTC'),
            status String,
            provider_id String,
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (ticker, doc_id)
        """,
    )
    fin.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_document_view
        (
            ticker String,
            tv_symbol String,
            doc_id String,
            view_id String,
            view_type String,
            content_status String,
            body_text String,
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (ticker, doc_id, view_id)
        """,
    )
    fin.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_document_sync
        (
            ticker String,
            tv_symbol String,
            doc_count UInt32,
            text_count UInt32,
            status String,
            detail String,
            checked_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(checked_at)
        ORDER BY ticker
        """,
    )
    comments = [
        ("tv_document", "MODIFY COMMENT", "TradingView 历史文档目录。一行是一只股票的一份文件，例如 10-K、财报新闻稿、电话会文字稿。正文在 tv_document_view"),
        ("tv_document", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同，例如 AAPL.US"),
        ("tv_document", "COMMENT COLUMN tv_symbol", "TradingView 代码，例如 NASDAQ:AAPL。网页地址把冒号写成连字符"),
        ("tv_document", "COMMENT COLUMN doc_id", "文件编号，来源给出的 urn，例如 urn:report:quartr.com:2220254"),
        ("tv_document", "COMMENT COLUMN correlation_id", "同一场活动的编号。一场财报会同时挂 10-Q、新闻稿和电话会文字稿，它们的 correlation_id 相同"),
        ("tv_document", "COMMENT COLUMN category", "文件种类。annual_report 年度报告；quarterly_report 季度报告；interim_report 中期报告；earnings_release 财报发布；call_transcript 业绩电话会文字稿；event_transcript 其他活动文字稿；slides 演示材料；press_release 新闻稿。来源可能增加新值"),
        ("tv_document", "COMMENT COLUMN category_title", "文件种类的英文显示名，来源原文"),
        ("tv_document", "COMMENT COLUMN event_kind", "活动种类。earning 业绩发布；corporate_event 公司活动，如产品发布会。来源可能增加新值"),
        ("tv_document", "COMMENT COLUMN form_id", "监管表格代号。form_10k 是 10-K 年报；form_10q 是 10-Q 季报；form_8k 是 8-K 临时报告；form_40f 是 40-F；form_6k 是 6-K。没有对应表格时为空字符串。来源可能增加新值"),
        ("tv_document", "COMMENT COLUMN form_title", "监管表格显示名，如 10-K。没有时为空字符串"),
        ("tv_document", "COMMENT COLUMN fiscal_year", "财年，如 2025。活动文字稿常常没有财年，这时为空"),
        ("tv_document", "COMMENT COLUMN fiscal_period", "财政期间。Q1、Q2、Q3、Q4 为季度；FY 为全年。没有时为空字符串"),
        ("tv_document", "COMMENT COLUMN title", "文件标题，来源原文，如 Q3 2026、Product Launch"),
        ("tv_document", "COMMENT COLUMN reported_at", "文件或活动的公布时间，UTC"),
        ("tv_document", "COMMENT COLUMN status", "文件状态。usable 表示可以打开。来源可能增加新值"),
        ("tv_document", "COMMENT COLUMN provider_id", "内容提供方代号。目前是 quartr"),
        ("tv_document", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、doc_id 保留更晚的一行"),
        ("tv_document_view", "MODIFY COMMENT", "TradingView 文档正文。一行是一份文件的一种打开方式。文字稿和摘要保存正文，PDF 原件公开接口不提供"),
        ("tv_document_view", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同"),
        ("tv_document_view", "COMMENT COLUMN tv_symbol", "TradingView 代码，例如 NASDAQ:AAPL"),
        ("tv_document_view", "COMMENT COLUMN doc_id", "所属文件编号，对应 tv_document.doc_id"),
        ("tv_document_view", "COMMENT COLUMN view_id", "这一打开方式的编号，来源给出的 urn"),
        ("tv_document_view", "COMMENT COLUMN view_type", "打开方式。transcript 文字稿全文；summary 摘要；pdf 原件 PDF。来源可能增加新值"),
        ("tv_document_view", "COMMENT COLUMN content_status", "正文是否拿到。fetched 已写入 body_text；empty 接口没有正文；unavailable 是 PDF，公开接口拒绝下载原件"),
        ("tv_document_view", "COMMENT COLUMN body_text", "正文纯文本。content_status 不是 fetched 时为空字符串"),
        ("tv_document_view", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、doc_id、view_id 保留更晚的一行"),
        ("tv_document_sync", "MODIFY COMMENT", "TradingView 文档抓取进度。一行是一只股票最近一次抓取的结果"),
        ("tv_document_sync", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同"),
        ("tv_document_sync", "COMMENT COLUMN tv_symbol", "解析到的 TradingView 代码。对不上时为空字符串"),
        ("tv_document_sync", "COMMENT COLUMN doc_count", "这次看到的文件份数"),
        ("tv_document_sync", "COMMENT COLUMN text_count", "这次写入了正文的文字稿和摘要份数"),
        ("tv_document_sync", "COMMENT COLUMN status", "最近一次结果。ok 已写入新文件；unchanged 检查后没有新文件或新正文；empty 对上了代码但没有文档；unresolved 对不上 TradingView 代码；error 请求失败"),
        ("tv_document_sync", "COMMENT COLUMN detail", "失败或对不上时的原因。成功时为空字符串"),
        ("tv_document_sync", "COMMENT COLUMN checked_at", "最近一次检查时间，UTC。同一 ticker 保留更晚的一行"),
    ]
    for table, action, comment in comments:
        FIN.ch_exec(env, "ALTER TABLE %s %s %s" % (table, action, FIN.sql_str(comment)))


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise StopRun("文档接口限流，停下来") from exc
        detail = exc.read().decode("utf-8", "replace")[:180]
        raise RuntimeError("HTTP %s %s" % (exc.code, detail)) from exc


def list_documents(tv_symbol: str) -> list[dict]:
    query = urllib.parse.urlencode({"filter": "symbol:" + tv_symbol}, safe=":")
    data = http_json(LIST_URL + "?" + query)
    items = data.get("items") or []
    return items if isinstance(items, list) else []


def view_text(view_id: str) -> str:
    url = VIEW_URL + urllib.parse.quote(view_id, safe="")
    data = http_json(url)
    return ast_text(data.get("astDescription")).strip()


def reported_at(raw) -> str:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return datetime.fromtimestamp(raw, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return FIN.stamp()


def document_row(ticker: str, tv_symbol: str, item: dict, when: str) -> dict:
    category = item.get("category") or {}
    form = item.get("form") or {}
    year = item.get("fiscal_year")
    provider = item.get("provider") or {}
    return {
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "doc_id": item.get("id") or "",
        "correlation_id": item.get("correlation_id") or "",
        "category": category.get("id") or "",
        "category_title": category.get("title") or "",
        "event_kind": item.get("event") or "",
        "form_id": form.get("id") or "",
        "form_title": form.get("title") or "",
        "fiscal_year": int(year) if isinstance(year, int) else None,
        "fiscal_period": item.get("fiscal_period") or "",
        "title": item.get("title") or "",
        "reported_at": reported_at(item.get("reported")),
        "status": item.get("status") or "",
        "provider_id": provider.get("id") or "",
        "updated_at": when,
    }


def one_view_row(ticker: str, tv_symbol: str, doc_id: str, view: dict, when: str) -> dict:
    view_id = view.get("id") or ""
    view_type = view.get("type") or ""
    row = {
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "doc_id": doc_id,
        "view_id": view_id,
        "view_type": view_type,
        "content_status": "unavailable",
        "body_text": "",
        "updated_at": when,
    }
    if view_type == "pdf" or not view_id:
        return row
    try:
        text = view_text(view_id)
    except StopRun:
        raise
    except RuntimeError:
        row["content_status"] = "empty"
        time.sleep(random.uniform(0.12, 0.28))
        return row
    row["content_status"] = "fetched" if text else "empty"
    row["body_text"] = text
    time.sleep(random.uniform(0.12, 0.28))
    return row


def view_rows(ticker: str, tv_symbol: str, item: dict, when: str) -> list[dict]:
    rows = []
    for view in item.get("views") or []:
        if not view.get("id"):
            continue
        rows.append(one_view_row(ticker, tv_symbol, item.get("id") or "", view, when))
    return rows


def insert_rows(env: dict[str, str], table: str, rows: list[dict]) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
    FIN.ch_exec(env, "INSERT INTO %s FORMAT JSONEachRow\n" % table, payload)


def known_symbols(env: dict[str, str]) -> dict[str, str]:
    return FIN.known_symbols(env)


def sync_state(env: dict[str, str]) -> dict[str, dict]:
    rows = FIN.ch_query(env, "SELECT ticker, status, checked_at FROM tv_document_sync FINAL")
    return {row["ticker"]: row for row in rows}


def resolve_symbol(code: str, exchange: str, ticker: str = "") -> tuple[str | None, str]:
    return FIN.resolve_symbol(
        code,
        exchange,
        lambda: time.sleep(random.uniform(0.8, 1.6)),
        ticker,
        match_listing,
    )


def write_sync(env, ticker, tv_symbol, doc_count, text_count, status, detail, when):
    insert_rows(env, "tv_document_sync", [{
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "doc_count": doc_count,
        "text_count": text_count,
        "status": status,
        "detail": detail,
        "checked_at": when,
    }])


RECENT_SECONDS = 14 * 24 * 3600


def select_document_work(documents, known_doc_ids, known_view_ids, now_ts, recent_seconds=RECENT_SECONDS):
    """新文件整份抓。最近 14 天的旧文件只补库里还没有的文字稿或摘要。"""
    new_docs = []
    extra_views = []
    for document in documents:
        doc_id = document.get("id")
        if not doc_id:
            continue
        if doc_id not in known_doc_ids:
            new_docs.append(document)
            continue
        reported = document.get("reported") or 0
        if not isinstance(reported, (int, float)) or now_ts - reported > recent_seconds:
            continue
        for view in document.get("views") or []:
            view_id = view.get("id")
            if view_id and view_id not in known_view_ids:
                extra_views.append((document, view))
    return new_docs, extra_views


def fresh_ok(row: dict) -> bool:
    if row.get("status") != "ok":
        return False
    return FIN.fresh_enough(str(row.get("checked_at") or ""), 18)


def hold_unresolved(row: dict) -> bool:
    return row.get("status") == "unresolved" and FIN.fresh_enough(str(row.get("checked_at") or ""), 24 * 7)


def stored_doc_ids(env: dict[str, str], ticker: str) -> set[str]:
    rows = FIN.ch_query(
        env,
        "SELECT doc_id FROM tv_document FINAL WHERE ticker = %s" % FIN.sql_str(ticker),
    )
    return {row["doc_id"] for row in rows}


def stored_view_ids(env: dict[str, str], ticker: str) -> set[str]:
    rows = FIN.ch_query(
        env,
        "SELECT view_id FROM tv_document_view FINAL WHERE ticker = %s" % FIN.sql_str(ticker),
    )
    return {row["view_id"] for row in rows}


def run(only: set[str], limit: int, daily: bool) -> None:
    env = FIN.load_env()
    ensure_tables(env)
    items = FIN.listings(env)
    if only:
        items = [item for item in items if item["ticker"] in only]
    if limit:
        items = items[:limit]
    state = sync_state(env)
    known = known_symbols(env)
    print("名单 %d" % len(items), flush=True)
    updated = skipped = unresolved = empty = 0
    for index, item in enumerate(items, start=1):
        ticker = item["ticker"]
        previous = state.get(ticker) or {}
        if daily and hold_unresolved(previous):
            skipped += 1
            continue
        if not daily and fresh_ok(previous):
            skipped += 1
            print("%d/%d %s 这次已经写入过，跳过" % (index, len(items), ticker), flush=True)
            continue
        tv_symbol = known.get(ticker) or ""
        try:
            if not tv_symbol:
                code, _suffix = FIN.ticker_code(ticker)
                tv_symbol, reason = resolve_symbol(code, item.get("exchange") or "", ticker)
                time.sleep(random.uniform(0.8, 1.6))
                if not tv_symbol:
                    unresolved += 1
                    write_sync(env, ticker, "", 0, 0, "unresolved", reason, FIN.stamp())
                    print("%d/%d %s 对不上 %s" % (index, len(items), ticker, reason), flush=True)
                    time.sleep(random.uniform(1.0, 2.0))
                    continue
            documents = list_documents(tv_symbol)
        except (StopRun, FIN.StopRun):
            raise
        except (RuntimeError, urllib.error.URLError) as exc:
            write_sync(env, ticker, tv_symbol, 0, 0, "error", str(exc)[:180], FIN.stamp())
            print("%d/%d %s 失败 %s" % (index, len(items), ticker, str(exc)[:120]), flush=True)
            time.sleep(random.uniform(2.0, 4.0))
            continue
        when = FIN.stamp()
        if not documents:
            empty += 1
            write_sync(env, ticker, tv_symbol, 0, 0, "empty", "", when)
            print("%d/%d %s 没有文档" % (index, len(items), ticker), flush=True)
            time.sleep(random.uniform(1.0, 2.0))
            continue
        if daily:
            new_docs, extra_views = select_document_work(
                documents,
                stored_doc_ids(env, ticker),
                stored_view_ids(env, ticker),
                time.time(),
            )
        else:
            new_docs, extra_views = [document for document in documents if document.get("id")], []
        doc_rows = []
        text_rows = []
        text_count = 0
        try:
            for document in new_docs:
                doc_rows.append(document_row(ticker, tv_symbol, document, when))
                for row in view_rows(ticker, tv_symbol, document, when):
                    text_rows.append(row)
                    if row["content_status"] == "fetched":
                        text_count += 1
            for document, view in extra_views:
                row = one_view_row(ticker, tv_symbol, document.get("id") or "", view, when)
                text_rows.append(row)
                if row["content_status"] == "fetched":
                    text_count += 1
        except (StopRun, FIN.StopRun):
            raise
        insert_rows(env, "tv_document", doc_rows)
        insert_rows(env, "tv_document_view", text_rows)
        status = "unchanged" if daily and not doc_rows and not text_rows else "ok"
        write_sync(env, ticker, tv_symbol, len(doc_rows), text_count, status, "", when)
        if status == "unchanged":
            skipped += 1
        else:
            updated += 1
        print(
            "%d/%d %s %s 新文件 %d 正文 %d" % (index, len(items), ticker, tv_symbol, len(doc_rows), text_count),
            flush=True,
        )
        time.sleep(random.uniform(1.0, 2.2))
    print("结束 写入 %d，已完成 %d，对不上 %d，没有文档 %d" % (updated, skipped, unresolved, empty), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 universe 的 TradingView 历史文档")
    parser.add_argument("--daily", action="store_true", help="只补新文件，以及最近 14 天里新挂上的文字稿或摘要")
    parser.add_argument("--only", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a")
    try:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("已有一轮在跑，这次退出")
        return
    try:
        run(only, args.limit, args.daily)
    except (StopRun, FIN.StopRun) as exc:
        print("停下：%s" % exc, flush=True)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
