#!/usr/bin/env python3
"""把 TradingView 股票筛选器的全球主上市普通股写入 pangu 和 munger。

来源是 https://cn.tradingview.com/screener/ 背后的 global/scan，不登录。
一天一份快照。Munger 的临时加入下拉读最新一天、已经能映射成公司库代码的那些行。

用法：
  python3 scripts/load_tv_screener.py
  python3 scripts/load_tv_screener.py --as-of 2026-10-22
  python3 scripts/load_tv_screener.py --copy-munger
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import importlib.util
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "data" / "tv_screener.lock"
SCAN_URL = "https://scanner.tradingview.com/global/scan?label-product=screener-stock"
PAGE = 500
BATCH = 2000
SCAN_MARKETS = (
    "america",
    "china",
    "hongkong",
    "japan",
    "korea",
    "taiwan",
    "uk",
    "germany",
    "france",
    "italy",
    "spain",
    "netherlands",
    "switzerland",
    "sweden",
    "norway",
    "canada",
    "australia",
    "brazil",
    "india",
    "singapore",
    "malaysia",
    "thailand",
    "indonesia",
    "philippines",
    "mexico",
    "turkey",
    "israel",
    "uae",
    "poland",
    "vietnam",
)
SCAN_COLUMNS = (
    "name",
    "description",
    "close",
    "change",
    "volume",
    "relative_volume_10d_calc",
    "Value.Traded",
    "currency",
    "market",
    "exchange",
    "country",
    "market_cap_basic",
    "sector",
    "sector.tr",
    "is_primary",
    "typespecs",
    "logoid",
)
EXCHANGE_SUFFIX = {
    "NASDAQ": "US",
    "NYSE": "US",
    "CBOE": "US",
    "AMEX": "US",
    "NYSEARCA": "US",
    "NYSEAMERICAN": "US",
    "BATS": "US",
    "ARCA": "US",
    "SSE": "CH",
    "SZSE": "CH",
    "HKEX": "HK",
    "TSE": "JP",
    "TWSE": "TT",
    "TPEX": "TT",
    "KRX": "KS",
    "TSX": "CN",
    "XETR": "GR",
    "MIL": "IM",
    "SIX": "SW",
    "ASX": "AU",
    "BMFBOVESPA": "BZ",
    "LSE": "LN",
}
EURONEXT_MARKET = {
    "netherlands": "NA",
    "france": "FP",
}
MUNGER_KEYS = (
    "MUNGER_CH_HOST",
    "MUNGER_CH_HTTP_PORT",
    "MUNGER_CH_DATABASE",
    "MUNGER_CH_USER",
    "MUNGER_CH_PASSWORD",
)
STOCK_COLUMNS = (
    "as_of_date",
    "tv_symbol",
    "ticker",
    "bbg_ticker",
    "name",
    "short_name",
    "name_zh",
    "exchange",
    "market",
    "country",
    "currency",
    "sector",
    "close",
    "change_pct",
    "volume",
    "rel_volume",
    "turnover_usd",
    "market_cap_usd",
    "is_primary",
    "typespecs",
    "map_ok",
    "updated_at",
)
SYNC_COLUMNS = (
    "run_id",
    "as_of_date",
    "total_count",
    "written",
    "mapped",
    "unmapped",
    "detail",
    "fetched_at",
    "updated_at",
)


def load_financials():
    path = Path(__file__).with_name("load_tv_financials.py")
    spec = importlib.util.spec_from_file_location("load_tv_financials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIN = load_financials()


def session_date() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def parse_tv_symbol(tv_symbol: str) -> tuple[str, str]:
    text = str(tv_symbol or "").strip().upper()
    if ":" not in text:
        return "", text
    exchange, code = text.split(":", 1)
    return exchange.strip(), code.strip()


def ticker_from_tv(tv_symbol: str, market: str = "", known: dict[str, str] | None = None) -> dict:
    raw = str(tv_symbol or "").strip()
    exchange, code = parse_tv_symbol(raw)
    mapped = (known or {}).get(raw) or (known or {}).get(raw.upper())
    if mapped:
        suffix = mapped.rsplit(".", 1)[-1] if "." in mapped else ""
        bbg_code = mapped[: -len(suffix) - 1] if suffix else mapped
        return {
            "ticker": mapped,
            "bbg_ticker": ("%s %s Equity" % (bbg_code, suffix)).strip() if suffix else "",
            "map_ok": True,
            "exchange": exchange,
            "code": code,
        }
    suffix = EXCHANGE_SUFFIX.get(exchange, "")
    if not suffix and exchange == "EURONEXT":
        suffix = EURONEXT_MARKET.get(str(market or "").strip().lower(), "")
    if exchange == "HKEX" and code.isdigit():
        code = str(int(code))
    if suffix == "US" and "." in code:
        code = code.replace(".", "/")
    if suffix:
        ticker = "%s.%s" % (code, suffix)
        return {
            "ticker": ticker,
            "bbg_ticker": "%s %s Equity" % (code, suffix),
            "map_ok": True,
            "exchange": exchange,
            "code": code,
        }
    fallback = "%s.%s" % (code.replace("/", "_") or "UNK", exchange or "TV")
    return {
        "ticker": fallback,
        "bbg_ticker": "",
        "map_ok": False,
        "exchange": exchange,
        "code": code,
    }


def screener_payload(start: int, end: int) -> dict:
    return {
        "filter": [
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "typespecs", "operation": "has", "right": ["common"]},
            {"left": "is_primary", "operation": "equal", "right": True},
        ],
        "options": {"lang": "zh"},
        "markets": list(SCAN_MARKETS),
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": list(SCAN_COLUMNS),
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [start, end],
    }


def http_post(url: str, payload: dict) -> dict:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError("TradingView 筛选接口返回 %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("TradingView 筛选接口连不上") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TradingView 筛选接口返回的不是 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("TradingView 筛选接口格式不对")
    return data


def fetch_screener(post=http_post, page_size: int = PAGE, progress: bool = False) -> tuple[list[dict], int]:
    rows: list[dict] = []
    total = 0
    start = 0
    size = max(1, int(page_size))
    while True:
        data = post(SCAN_URL, screener_payload(start, start + size))
        total = int(data.get("totalCount") or 0)
        chunk = data.get("data") or []
        if not isinstance(chunk, list):
            raise RuntimeError("TradingView 筛选接口没有列表")
        rows.extend(item for item in chunk if isinstance(item, dict))
        start += len(chunk)
        if progress:
            print("已拉 %d / %d" % (start, total), flush=True)
        if start >= total or len(chunk) < size:
            break
    return rows, total


def num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def has_cjk(value: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in value)


def parse_row(raw: dict, known: dict[str, str] | None = None) -> dict | None:
    tv_symbol = str(raw.get("s") or "").strip()
    values = raw.get("d")
    if not tv_symbol or not isinstance(values, list):
        return None
    fields = {name: values[index] if index < len(values) else None for index, name in enumerate(SCAN_COLUMNS)}
    mapped = ticker_from_tv(tv_symbol, str(fields.get("market") or ""), known)
    description = text(fields.get("description"))
    short_name = text(fields.get("name"))
    return {
        "tv_symbol": tv_symbol,
        "ticker": mapped["ticker"],
        "bbg_ticker": mapped["bbg_ticker"],
        "name": description or short_name or mapped["ticker"],
        "short_name": short_name or mapped["code"],
        "name_zh": description if has_cjk(description) else "",
        "exchange": text(fields.get("exchange")) or mapped["exchange"],
        "market": text(fields.get("market")),
        "country": text(fields.get("country")),
        "currency": text(fields.get("currency")),
        "sector": text(fields.get("sector.tr")) or text(fields.get("sector")),
        "close": num(fields.get("close")) or 0.0,
        "change_pct": num(fields.get("change")),
        "volume": num(fields.get("volume")) or 0.0,
        "rel_volume": num(fields.get("relative_volume_10d_calc")),
        "turnover_usd": num(fields.get("Value.Traded")) or 0.0,
        "market_cap_usd": num(fields.get("market_cap_basic")),
        "is_primary": 1 if fields.get("is_primary") else 0,
        "typespecs": text(fields.get("typespecs")),
        "map_ok": 1 if mapped["map_ok"] else 0,
    }


def known_tv_to_ticker(env: dict[str, str]) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        rows = FIN.ch_query(env, "SELECT ticker, tv_symbol FROM tv_symbol FINAL")
    except RuntimeError:
        return found
    for row in rows:
        symbol = str(row.get("tv_symbol") or "").strip()
        ticker = str(row.get("ticker") or "").strip()
        if symbol and ticker:
            found[symbol] = ticker
    return found


def universe_names(env: dict[str, str]) -> dict[str, dict]:
    if not FIN.munger_ready(env):
        return {}
    try:
        rows = FIN.ch_query_munger(
            env,
            """
            SELECT ticker, name, short_name, name_zh
            FROM universe FINAL
            WHERE deleted = 0
            """,
        )
    except RuntimeError:
        return {}
    return {str(row["ticker"]): row for row in rows if row.get("ticker")}


def munger_ready(env: dict[str, str]) -> bool:
    return all(str(env.get(key) or "").strip() for key in MUNGER_KEYS)


def ch_exec_munger(env: dict[str, str], sql: str, data: bytes | None = None) -> str:
    params = urllib.parse.urlencode({"database": env["MUNGER_CH_DATABASE"], "query": sql})
    url = "http://%s:%s/?%s" % (env["MUNGER_CH_HOST"], env.get("MUNGER_CH_HTTP_PORT", "8123"), params)
    token = base64.b64encode(("%s:%s" % (env["MUNGER_CH_USER"], env["MUNGER_CH_PASSWORD"])).encode()).decode()
    body = data if data is not None else b""
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(detail) from exc


def insert_rows_munger(env: dict[str, str], table: str, rows: list[dict]) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
    ch_exec_munger(env, "INSERT INTO %s FORMAT JSONEachRow\n" % table, payload)


def insert_rows_both(env: dict[str, str], table: str, rows: list[dict]) -> None:
    FIN.insert_rows(env, table, rows)
    if munger_ready(env):
        insert_rows_munger(env, table, rows)


def apply_schema(exec_fn) -> None:
    exec_fn(
        """
        CREATE TABLE IF NOT EXISTS tv_screener_stock
        (
            as_of_date Date,
            tv_symbol String,
            ticker String,
            bbg_ticker String,
            name String,
            short_name String,
            name_zh String,
            exchange String,
            market String,
            country String,
            currency String,
            sector String,
            close Float64,
            change_pct Nullable(Float64),
            volume Float64,
            rel_volume Nullable(Float64),
            turnover_usd Float64,
            market_cap_usd Nullable(Float64),
            is_primary UInt8,
            typespecs String,
            map_ok UInt8,
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (as_of_date, tv_symbol)
        """,
    )
    exec_fn(
        """
        CREATE TABLE IF NOT EXISTS tv_screener_sync
        (
            run_id String,
            as_of_date Date,
            total_count UInt32,
            written UInt32,
            mapped UInt32,
            unmapped UInt32,
            detail String,
            fetched_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY run_id
        """,
    )
    comments = [
        ("tv_screener_stock", "MODIFY COMMENT", "TradingView 股票筛选器每日快照。一行是一只主上市普通股一个交易日。来自 global/scan，不登录。Munger 临时加入下拉读最新一天且 map_ok=1 的行"),
        ("tv_screener_stock", "COMMENT COLUMN as_of_date", "这次扫描对应的交易日，按美东日历。例：2026-10-22"),
        ("tv_screener_stock", "COMMENT COLUMN tv_symbol", "TradingView 代码。例：NASDAQ:NVDA、SSE:600519"),
        ("tv_screener_stock", "COMMENT COLUMN ticker", "映射后的公司库代码。对不上时是交易所后缀的占位，map_ok=0。例：NVDA.US、700.HK"),
        ("tv_screener_stock", "COMMENT COLUMN bbg_ticker", "彭博证券代码。对不上时为空。例：NVDA US Equity"),
        ("tv_screener_stock", "COMMENT COLUMN name", "英文名称。能从 universe 补则补，否则用 TV description"),
        ("tv_screener_stock", "COMMENT COLUMN short_name", "简称。优先 universe.short_name，否则 TV name"),
        ("tv_screener_stock", "COMMENT COLUMN name_zh", "中文名。优先 universe.name_zh；没有时若 TV 名称是中文则记下，否则为空"),
        ("tv_screener_stock", "COMMENT COLUMN exchange", "TV 交易所。例：NASDAQ、SSE、HKEX"),
        ("tv_screener_stock", "COMMENT COLUMN market", "TV 市场。例：america、china、hongkong"),
        ("tv_screener_stock", "COMMENT COLUMN country", "TV 国家。例：United States"),
        ("tv_screener_stock", "COMMENT COLUMN currency", "计价货币。例：USD、CNY、HKD"),
        ("tv_screener_stock", "COMMENT COLUMN sector", "板块。优先中文 sector.tr。例：电子科技"),
        ("tv_screener_stock", "COMMENT COLUMN close", "扫描当时的最新价"),
        ("tv_screener_stock", "COMMENT COLUMN change_pct", "当日涨跌幅，百分数。没有则为 NULL"),
        ("tv_screener_stock", "COMMENT COLUMN volume", "成交量，股"),
        ("tv_screener_stock", "COMMENT COLUMN rel_volume", "相对成交量。没有则为 NULL"),
        ("tv_screener_stock", "COMMENT COLUMN turnover_usd", "成交额美元，TV 字段 Value.Traded"),
        ("tv_screener_stock", "COMMENT COLUMN market_cap_usd", "美元市值。没有则为 NULL"),
        ("tv_screener_stock", "COMMENT COLUMN is_primary", "是否主上市。扫描时已要求为 1"),
        ("tv_screener_stock", "COMMENT COLUMN typespecs", "TV 品种标记。例：common"),
        ("tv_screener_stock", "COMMENT COLUMN map_ok", "是否已映射成公司库 ticker。1 可以进临时加入和下拉，0 不行"),
        ("tv_screener_stock", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 as_of_date、tv_symbol 保留更晚的一行"),
        ("tv_screener_sync", "MODIFY COMMENT", "TradingView 股票筛选器扫描对账。一行是一次抓取"),
        ("tv_screener_sync", "COMMENT COLUMN run_id", "这次抓取编号，通常是写入时刻"),
        ("tv_screener_sync", "COMMENT COLUMN as_of_date", "对应的交易日"),
        ("tv_screener_sync", "COMMENT COLUMN total_count", "TV 接口声称的总只数"),
        ("tv_screener_sync", "COMMENT COLUMN written", "实际写入 tv_screener_stock 的行数"),
        ("tv_screener_sync", "COMMENT COLUMN mapped", "映射成公司库代码的只数"),
        ("tv_screener_sync", "COMMENT COLUMN unmapped", "没映射成公司库代码的只数"),
        ("tv_screener_sync", "COMMENT COLUMN detail", "附加 JSON，便于对账"),
        ("tv_screener_sync", "COMMENT COLUMN fetched_at", "抓取时刻，UTC"),
        ("tv_screener_sync", "COMMENT COLUMN updated_at", "本行写入时间，UTC"),
    ]
    for table, action, comment in comments:
        exec_fn("ALTER TABLE %s %s %s" % (table, action, FIN.sql_str(comment)))


def ensure_tables(env: dict[str, str]) -> None:
    apply_schema(lambda sql: FIN.ch_exec(env, sql))
    if munger_ready(env):
        apply_schema(lambda sql: ch_exec_munger(env, sql))
    else:
        print("警告：没有 munger 写账号，只在 pangu 建表", flush=True)


def enrich(row: dict, names: dict[str, dict]) -> dict:
    found = names.get(row["ticker"]) or {}
    if found.get("name"):
        row["name"] = str(found.get("name") or row["name"])
    if found.get("short_name"):
        row["short_name"] = str(found.get("short_name") or row["short_name"])
    if found.get("name_zh"):
        row["name_zh"] = str(found.get("name_zh") or "")
    return row


def run(as_of: str | None = None, post=http_post) -> dict:
    env = FIN.load_env()
    ensure_tables(env)
    known = known_tv_to_ticker(env)
    names = universe_names(env)
    raw_rows, total = fetch_screener(post, progress=True)
    day = as_of or session_date()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    seen: set[str] = set()
    payload = []
    for raw in raw_rows:
        row = parse_row(raw, known)
        if row is None or row["tv_symbol"] in seen:
            continue
        seen.add(row["tv_symbol"])
        row = enrich(row, names)
        payload.append({
            "as_of_date": day,
            "tv_symbol": row["tv_symbol"],
            "ticker": row["ticker"],
            "bbg_ticker": row["bbg_ticker"],
            "name": row["name"],
            "short_name": row["short_name"],
            "name_zh": row["name_zh"],
            "exchange": row["exchange"],
            "market": row["market"],
            "country": row["country"],
            "currency": row["currency"],
            "sector": row["sector"],
            "close": row["close"],
            "change_pct": row["change_pct"],
            "volume": row["volume"],
            "rel_volume": row["rel_volume"],
            "turnover_usd": row["turnover_usd"],
            "market_cap_usd": row["market_cap_usd"],
            "is_primary": row["is_primary"],
            "typespecs": row["typespecs"],
            "map_ok": row["map_ok"],
            "updated_at": stamp,
        })
    if not munger_ready(env):
        print("警告：没有 munger 写账号，只写入 pangu", flush=True)
    for start in range(0, len(payload), BATCH):
        insert_rows_both(env, "tv_screener_stock", payload[start : start + BATCH])
        print("已写 %d / %d" % (min(start + BATCH, len(payload)), len(payload)), flush=True)
    mapped = sum(1 for row in payload if row["map_ok"])
    insert_rows_both(
        env,
        "tv_screener_sync",
        [{
            "run_id": stamp.replace(" ", "T") + "Z",
            "as_of_date": day,
            "total_count": total,
            "written": len(payload),
            "mapped": mapped,
            "unmapped": len(payload) - mapped,
            "detail": json.dumps({"total": total, "parsed": len(payload)}, ensure_ascii=False),
            "fetched_at": stamp,
            "updated_at": stamp,
        }],
    )
    result = {
        "as_of_date": day,
        "total_count": total,
        "written": len(payload),
        "mapped": mapped,
        "unmapped": len(payload) - mapped,
    }
    print(
        "结束 as_of=%s written=%s mapped=%s unmapped=%s total=%s"
        % (day, result["written"], result["mapped"], result["unmapped"], result["total_count"]),
        flush=True,
    )
    return result


def copy_munger(as_of: str | None = None) -> dict:
    env = FIN.load_env()
    if not munger_ready(env):
        raise RuntimeError("tradingview/.env 里没有 munger 写账号，无法同步")
    ensure_tables(env)
    day = as_of
    if not day:
        rows = FIN.ch_query(env, "SELECT max(as_of_date) AS d FROM tv_screener_stock")
        day = str(rows[0]["d"]) if rows and rows[0].get("d") else ""
    if not day or day == "1970-01-01":
        raise RuntimeError("pangu 还没有 tv_screener_stock")
    cols = ", ".join(STOCK_COLUMNS)
    stock = FIN.ch_query(
        env,
        "SELECT %s FROM tv_screener_stock FINAL WHERE as_of_date = %s" % (cols, FIN.sql_str(day)),
    )
    for start in range(0, len(stock), BATCH):
        insert_rows_munger(env, "tv_screener_stock", stock[start : start + BATCH])
        print("已同步 munger %d / %d" % (min(start + BATCH, len(stock)), len(stock)), flush=True)
    sync_cols = ", ".join(SYNC_COLUMNS)
    sync_rows = FIN.ch_query(
        env,
        """
        SELECT %s FROM tv_screener_sync FINAL
        WHERE as_of_date = %s
        ORDER BY updated_at DESC
        LIMIT 1
        """
        % (sync_cols, FIN.sql_str(day)),
    )
    if sync_rows:
        insert_rows_munger(env, "tv_screener_sync", sync_rows)
    result = {"as_of_date": day, "written": len(stock)}
    print("结束 copy as_of=%s written=%s" % (day, result["written"]), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 TradingView 股票筛选器写入 pangu 和 munger")
    parser.add_argument("--as-of", default="", help="交易日 YYYY-MM-DD，默认美东今天")
    parser.add_argument("--copy-munger", action="store_true", help="把 pangu 最新一天复制到 munger，不再抓取")
    args = parser.parse_args()
    if args.as_of:
        date.fromisoformat(args.as_of)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("已有一轮在跑，这次退出")
        return 0
    if args.copy_munger:
        copy_munger(args.as_of or None)
        return 0
    run(args.as_of or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
