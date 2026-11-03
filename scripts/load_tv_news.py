#!/usr/bin/env python3
"""把 TradingView 新闻流写入 pangu。

每只股票用公开接口取新闻列表。英文和简体中文分开请求，再按编号去重。
接口一次最多返回最近 200 条；返回里的翻页标记不能再拿来请求更早的新闻。
不足 200 条时，就是目前能检索到的全部。
每条新闻再取正文。付费墙后面的不请求正文。

一只一只拉。被限流就停。连接被掐、超时这类瞬时错误会连打三次，
三次还失败：列表失败就跳过这只，正文失败就留下标题不写正文，不把整段任务停掉。

用法：
  .venv/bin/python scripts/load_tv_news.py
  .venv/bin/python scripts/load_tv_news.py --only NVDA.US
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "data" / "tv_news.lock"
LIST_URL = "https://news-mediator.tradingview.com/public/news-flow/v2/news"
STORY_URL = "https://news-mediator.tradingview.com/public/news/v1/story"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://cn.tradingview.com",
    "Referer": "https://cn.tradingview.com/news-flow/",
}


def load_documents():
    path = Path(__file__).with_name("load_tv_documents.py")
    spec = importlib.util.spec_from_file_location("load_tv_documents", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOC = load_documents()
FIN = DOC.FIN


class StopRun(RuntimeError):
    pass


HTTP_ATTEMPTS = 3


def http_json(url: str, attempts: int = HTTP_ATTEMPTS) -> dict:
    last: BaseException | None = None
    total = max(1, int(attempts))
    for attempt in range(1, total + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise StopRun("新闻接口限流，停下来") from exc
            last = exc
            if exc.code >= 500 and attempt < total:
                time.sleep(random.uniform(0.8, 1.6) * attempt)
                continue
            detail = exc.read().decode("utf-8", "replace")[:180]
            raise RuntimeError("HTTP %s %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            if attempt >= total:
                raise
            time.sleep(random.uniform(0.8, 1.6) * attempt)
    raise last or RuntimeError("新闻接口没有响应")


def news_filter_sets(tv_symbol: str) -> list[list[str]]:
    """英文流和简体中文流要分开请求。两个语言条件写在一起是且关系，会变成空结果。"""
    symbol = "symbol:" + tv_symbol
    return [[symbol], ["lang:zh-Hans", symbol]]


def list_news(tv_symbol: str) -> tuple[list[dict], int]:
    """返回新闻列表，以及是否还有更早的新闻拿不到。1 表示到了 200 条上限。"""
    seen: dict[str, dict] = {}
    capped = 0
    for filters in news_filter_sets(tv_symbol):
        query = urllib.parse.urlencode(
            [("client", "web")] + [("filter", item) for item in filters],
            safe=":",
        )
        data = http_json(LIST_URL + "?" + query)
        items = data.get("items") or []
        if not isinstance(items, list):
            items = []
        cursor = (data.get("pagination") or {}).get("cursor")
        if cursor and len(items) >= 200:
            capped = 1
        for item in items:
            news_id = item.get("id")
            if news_id and news_id not in seen:
                seen[news_id] = item
    return list(seen.values()), capped


def story_of(news_id: str) -> dict:
    query = urllib.parse.urlencode({"id": news_id, "lang": "en", "client": "web"})
    return http_json(STORY_URL + "?" + query)


def ensure_tables(env: dict[str, str]) -> None:
    FIN.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_news
        (
            ticker String,
            tv_symbol String,
            news_id String,
            title String,
            published_at DateTime64(3, 'UTC'),
            urgency Int8,
            paywall UInt8,
            link String,
            story_path String,
            provider_id String,
            provider_name String,
            related_symbols String,
            language String,
            short_description String,
            body_text String,
            content_status String,
            list_capped UInt8,
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (ticker, news_id)
        """,
    )
    FIN.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_news_sync
        (
            ticker String,
            tv_symbol String,
            news_count UInt32,
            list_capped UInt8,
            status String,
            detail String,
            checked_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(checked_at)
        ORDER BY ticker
        """,
    )
    comments = [
        ("tv_news", "MODIFY COMMENT", "TradingView 新闻流。一行是一只股票的一条新闻。每只股票最多保存接口返回的最近 200 条；更早的历史这个接口不提供翻页"),
        ("tv_news", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同，例如 NVDA.US"),
        ("tv_news", "COMMENT COLUMN tv_symbol", "TradingView 代码，例如 NASDAQ:NVDA。新闻流地址是 /news-flow/?symbol= 加上这个代码"),
        ("tv_news", "COMMENT COLUMN news_id", "新闻编号，来源原文，例如 reuters:xxxx:0"),
        ("tv_news", "COMMENT COLUMN title", "标题"),
        ("tv_news", "COMMENT COLUMN published_at", "发布时间，UTC"),
        ("tv_news", "COMMENT COLUMN urgency", "紧急程度。1 较重要，2 普通。来源可能增加新值"),
        ("tv_news", "COMMENT COLUMN paywall", "是否在付费墙后面。0 否，1 是"),
        ("tv_news", "COMMENT COLUMN link", "原文链接，来源网站上的地址"),
        ("tv_news", "COMMENT COLUMN story_path", "TradingView 上的新闻路径，例如 /news/reuters:xxxx:0-标题/"),
        ("tv_news", "COMMENT COLUMN provider_id", "来源代号，如 reuters、dow-jones、gurufocus。不是封闭枚举，以来源实际返回为准"),
        ("tv_news", "COMMENT COLUMN provider_name", "来源显示名，如 Reuters"),
        ("tv_news", "COMMENT COLUMN related_symbols", "这条新闻关联的 TradingView 代码，多个用英文逗号分隔，如 NASDAQ:NVDA,NASDAQ:AMD"),
        ("tv_news", "COMMENT COLUMN language", "正文语言，如 en。没有取到正文时为空字符串"),
        ("tv_news", "COMMENT COLUMN short_description", "短摘要。没有取到正文时为空字符串"),
        ("tv_news", "COMMENT COLUMN body_text", "正文纯文本。content_status 不是 fetched 时为空字符串"),
        ("tv_news", "COMMENT COLUMN content_status", "正文是否拿到。fetched 已写入正文；empty 接口没有正文；paywall 在付费墙后面，不请求正文"),
        ("tv_news", "COMMENT COLUMN list_capped", "这只股票的列表是否到了上限。1 是，接口只给了最近 200 条，更早的拿不到；0 否，返回的就是目前全部"),
        ("tv_news", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、news_id 保留更晚的一行"),
        ("tv_news_sync", "MODIFY COMMENT", "TradingView 新闻抓取进度。一行是一只股票最近一次抓取的结果"),
        ("tv_news_sync", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同"),
        ("tv_news_sync", "COMMENT COLUMN tv_symbol", "解析到的 TradingView 代码。对不上时为空字符串"),
        ("tv_news_sync", "COMMENT COLUMN news_count", "这次写入的新闻条数"),
        ("tv_news_sync", "COMMENT COLUMN list_capped", "1 表示只拿到最近 200 条；0 表示拿到的是目前全部。对不上或失败时为 0"),
        ("tv_news_sync", "COMMENT COLUMN status", "最近一次结果。ok 已写入新新闻；unchanged 检查后没有新编号；empty 对上了代码但没有新闻；unresolved 对不上 TradingView 代码；error 请求失败"),
        ("tv_news_sync", "COMMENT COLUMN detail", "失败或对不上时的原因。成功时为空字符串"),
        ("tv_news_sync", "COMMENT COLUMN checked_at", "最近一次检查时间，UTC。同一 ticker 保留更晚的一行"),
    ]
    for table, action, comment in comments:
        FIN.ch_exec(env, "ALTER TABLE %s %s %s" % (table, action, FIN.sql_str(comment)))


def published_at(raw) -> str:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(raw, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return FIN.stamp()


def related_symbols(item: dict) -> str:
    symbols = []
    for entry in item.get("relatedSymbols") or []:
        symbol = entry.get("symbol") if isinstance(entry, dict) else None
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return ",".join(symbols)


def news_row(ticker: str, tv_symbol: str, item: dict, capped: int, when: str) -> dict:
    provider = item.get("provider") or {}
    row = {
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "news_id": item.get("id") or "",
        "title": item.get("title") or "",
        "published_at": published_at(item.get("published")),
        "urgency": int(item.get("urgency") or 0),
        "paywall": 1 if item.get("paywall") else 0,
        "link": item.get("link") or "",
        "story_path": item.get("storyPath") or "",
        "provider_id": provider.get("id") or "",
        "provider_name": provider.get("name") or "",
        "related_symbols": related_symbols(item),
        "language": "",
        "short_description": "",
        "body_text": "",
        "content_status": "paywall" if item.get("paywall") else "empty",
        "list_capped": capped,
        "updated_at": when,
    }
    if item.get("paywall") or not item.get("id"):
        return row
    try:
        story = story_of(item["id"])
    except StopRun:
        raise
    except (RuntimeError, urllib.error.URLError, TimeoutError, ConnectionError):
        time.sleep(random.uniform(0.15, 0.3))
        return row
    text = DOC.ast_text(story.get("ast_description")).strip()
    row["language"] = story.get("language") or ""
    row["short_description"] = story.get("short_description") or ""
    row["body_text"] = text
    row["content_status"] = "fetched" if (text or row["short_description"]) else "empty"
    time.sleep(random.uniform(0.15, 0.3))
    return row


def insert_rows(env, table, rows):
    if not rows:
        return
    payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
    FIN.ch_exec(env, "INSERT INTO %s FORMAT JSONEachRow\n" % table, payload)


def write_sync(env, ticker, tv_symbol, news_count, capped, status, detail, when):
    insert_rows(env, "tv_news_sync", [{
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "news_count": news_count,
        "list_capped": capped,
        "status": status,
        "detail": detail,
        "checked_at": when,
    }])


def known_symbols(env):
    return FIN.known_symbols(env)


def sync_state(env):
    rows = FIN.ch_query(env, "SELECT ticker, status, checked_at FROM tv_news_sync FINAL")
    return {row["ticker"]: row for row in rows}


def resolve_symbol(code, exchange, ticker=""):
    return FIN.resolve_symbol(
        code,
        exchange,
        lambda: time.sleep(random.uniform(0.8, 1.6)),
        ticker,
        DOC.match_listing,
    )


def new_headlines(headlines, known_ids):
    return [item for item in headlines if item.get("id") and item["id"] not in known_ids]


def fresh_ok(row):
    return row.get("status") == "ok" and FIN.fresh_enough(str(row.get("checked_at") or ""), 18)


def hold_unresolved(row):
    return row.get("status") == "unresolved" and FIN.fresh_enough(str(row.get("checked_at") or ""), 24 * 7)


def stored_news_ids(env, ticker):
    rows = FIN.ch_query(
        env,
        "SELECT news_id FROM tv_news FINAL WHERE ticker = %s" % FIN.sql_str(ticker),
    )
    return {row["news_id"] for row in rows}


def run(only, limit, daily):
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
            headlines, capped = list_news(tv_symbol)
        except (StopRun, FIN.StopRun):
            raise
        except (RuntimeError, urllib.error.URLError) as exc:
            write_sync(env, ticker, tv_symbol, 0, 0, "error", str(exc)[:180], FIN.stamp())
            print("%d/%d %s 失败 %s" % (index, len(items), ticker, str(exc)[:120]), flush=True)
            time.sleep(random.uniform(2.0, 4.0))
            continue
        when = FIN.stamp()
        if not headlines:
            empty += 1
            write_sync(env, ticker, tv_symbol, 0, capped, "empty", "", when)
            print("%d/%d %s 没有新闻" % (index, len(items), ticker), flush=True)
            time.sleep(random.uniform(1.0, 2.0))
            continue
        pending = new_headlines(headlines, stored_news_ids(env, ticker)) if daily else headlines
        rows = []
        try:
            for headline in pending:
                if not headline.get("id"):
                    continue
                rows.append(news_row(ticker, tv_symbol, headline, capped, when))
        except (StopRun, FIN.StopRun):
            raise
        except (RuntimeError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            write_sync(env, ticker, tv_symbol, 0, capped, "error", str(exc)[:180], when)
            print("%d/%d %s 失败 %s" % (index, len(items), ticker, str(exc)[:120]), flush=True)
            time.sleep(random.uniform(2.0, 4.0))
            continue
        insert_rows(env, "tv_news", rows)
        status = "unchanged" if daily and not rows else "ok"
        write_sync(env, ticker, tv_symbol, len(rows), capped, status, "", when)
        if status == "unchanged":
            skipped += 1
        else:
            updated += 1
        fetched = sum(1 for row in rows if row["content_status"] == "fetched")
        print(
            "%d/%d %s %s 新新闻 %d 正文 %d 到顶 %d" % (index, len(items), ticker, tv_symbol, len(rows), fetched, capped),
            flush=True,
        )
        time.sleep(random.uniform(1.2, 2.4))
    print("结束 写入 %d，已完成 %d，对不上 %d，没有新闻 %d" % (updated, skipped, unresolved, empty), flush=True)


def main():
    parser = argparse.ArgumentParser(description="抓取 universe 的 TradingView 新闻")
    parser.add_argument("--daily", action="store_true", help="只补列表里还没有的新闻编号")
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
