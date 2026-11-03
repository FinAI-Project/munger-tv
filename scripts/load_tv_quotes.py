#!/usr/bin/env python3
"""把 TradingView 日线和截止日快照写入 pangu。

日线走登录后的行情图接口，一次拉一只。快照另取收盘、股本、市值，日期和收盘同一天。
这张表用来交叉核对、把研究截止日对齐到最近交易日。不覆盖雅虎的 quote_daily。

用法：
  python3 scripts/load_tv_quotes.py --full
  python3 scripts/load_tv_quotes.py --daily
  python3 scripts/load_tv_quotes.py --only INTC.US
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import socket
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "data" / "tv_quotes.lock"
FULL_BARS = 2500
DAILY_BARS = 20
SNAPSHOT_FIELDS = (
    "lp",
    "lp_time",
    "volume",
    "market_cap_basic",
    "market_cap_calc",
    "total_shares_outstanding",
    "float_shares_outstanding",
    "currency_code",
    "open_price",
    "high_price",
    "low_price",
    "prev_close_price",
)


def load_financials():
    path = Path(__file__).with_name("load_tv_financials.py")
    spec = importlib.util.spec_from_file_location("load_tv_financials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIN = load_financials()


class StopRun(RuntimeError):
    pass


def bar_date(raw) -> str | None:
    if raw in (None, ""):
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts < 1000000000:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def bar_row(ticker: str, tv_symbol: str, values: list, when: str) -> dict | None:
    if not isinstance(values, list) or len(values) < 5:
        return None
    day = bar_date(values[0])
    close = FIN.num(values[4])
    if not day or close is None:
        return None
    volume = FIN.num(values[5]) if len(values) > 5 else None
    return {
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "as_of_date": day,
        "open": FIN.num(values[1]),
        "high": FIN.num(values[2]),
        "low": FIN.num(values[3]),
        "close": close,
        "volume": volume if volume is not None else 0.0,
        "updated_at": when,
    }


def changed_bars(existing: dict[str, tuple[float, float]], rows: list[dict]) -> list[dict]:
    changed = []
    for row in rows:
        old = existing.get(row["as_of_date"])
        if old is None:
            changed.append(row)
            continue
        old_close, old_volume = old
        if not FIN.nearly_same(old_close, row["close"]) or not FIN.nearly_same(old_volume, row["volume"]):
            changed.append(row)
    return changed


def snapshot_row(ticker: str, tv_symbol: str, values: dict, fallback_date: str, when: str) -> dict | None:
    close = FIN.num(values.get("lp"))
    day = bar_date(values.get("lp_time")) or fallback_date
    if close is None or not day:
        return None
    market_cap = FIN.num(values.get("market_cap_basic"))
    if market_cap is None:
        market_cap = FIN.num(values.get("market_cap_calc"))
    return {
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "as_of_date": day,
        "close": close,
        "market_cap": market_cap,
        "shares_outstanding": FIN.num(values.get("total_shares_outstanding")),
        "float_shares": FIN.num(values.get("float_shares_outstanding")),
        "volume": FIN.num(values.get("volume")),
        "currency": str(values.get("currency_code") or ""),
        "updated_at": when,
    }


def ensure_tables(env: dict[str, str]) -> None:
    FIN.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_quote_daily
        (
            ticker String,
            tv_symbol String,
            as_of_date Date,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (ticker, as_of_date)
        """,
    )
    FIN.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_quote_snapshot
        (
            ticker String,
            tv_symbol String,
            as_of_date Date,
            close Float64,
            market_cap Nullable(Float64),
            shares_outstanding Nullable(Float64),
            float_shares Nullable(Float64),
            volume Nullable(Float64),
            currency String,
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY ticker
        """,
    )
    FIN.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_quote_sync
        (
            ticker String,
            tv_symbol String,
            last_date String,
            bar_count UInt32,
            status String,
            detail String,
            checked_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(checked_at)
        ORDER BY ticker
        """,
    )
    comments = [
        ("tv_quote_daily", "MODIFY COMMENT", "TradingView 日线。一行是一只股票一个交易日。收盘已按拆分调整、未按分红调整。用来交叉核对，不覆盖雅虎 quote_daily"),
        ("tv_quote_daily", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同，例如 INTC.US"),
        ("tv_quote_daily", "COMMENT COLUMN tv_symbol", "TradingView 代码，例如 NASDAQ:INTC"),
        ("tv_quote_daily", "COMMENT COLUMN as_of_date", "交易日。按 K 线时间戳的 UTC 日期，美股通常就是当地交易日"),
        ("tv_quote_daily", "COMMENT COLUMN open", "开盘价，已按拆分调整"),
        ("tv_quote_daily", "COMMENT COLUMN high", "最高价，已按拆分调整"),
        ("tv_quote_daily", "COMMENT COLUMN low", "最低价，已按拆分调整"),
        ("tv_quote_daily", "COMMENT COLUMN close", "收盘价，已按拆分调整、未按分红调整。例：INTC.US 2026-10-16 为 89.85"),
        ("tv_quote_daily", "COMMENT COLUMN volume", "成交量"),
        ("tv_quote_daily", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、as_of_date 保留更晚的一行"),
        ("tv_quote_snapshot", "MODIFY COMMENT", "TradingView 最近一个交易日的收盘、股本、市值。三个数字同一天，用来对齐研究截止日"),
        ("tv_quote_snapshot", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同"),
        ("tv_quote_snapshot", "COMMENT COLUMN tv_symbol", "TradingView 代码"),
        ("tv_quote_snapshot", "COMMENT COLUMN as_of_date", "快照对应的最近交易日，与 close 同一天"),
        ("tv_quote_snapshot", "COMMENT COLUMN close", "该日收盘。例：INTC.US 2026-10-16 为 89.85"),
        ("tv_quote_snapshot", "COMMENT COLUMN market_cap", "该日市值，货币个位。没有时为空"),
        ("tv_quote_snapshot", "COMMENT COLUMN shares_outstanding", "已发行股本。没有时为空"),
        ("tv_quote_snapshot", "COMMENT COLUMN float_shares", "流通股本。没有时为空"),
        ("tv_quote_snapshot", "COMMENT COLUMN volume", "该日成交量。没有时为空"),
        ("tv_quote_snapshot", "COMMENT COLUMN currency", "价格和市值的货币，ISO 4217"),
        ("tv_quote_snapshot", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker 保留更晚的一行"),
        ("tv_quote_sync", "MODIFY COMMENT", "TradingView 日线抓取进度。一行是一只股票最近一次检查的结果"),
        ("tv_quote_sync", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同"),
        ("tv_quote_sync", "COMMENT COLUMN tv_symbol", "解析到的 TradingView 代码。对不上时为空字符串"),
        ("tv_quote_sync", "COMMENT COLUMN last_date", "这次看到的最近交易日。没有时为空字符串"),
        ("tv_quote_sync", "COMMENT COLUMN bar_count", "这次新写入或改写的日线条数"),
        ("tv_quote_sync", "COMMENT COLUMN status", "最近一次结果。ok 已写入；unchanged 检查后没有新交易日；empty 对上了代码但没有日线；unresolved 对不上 TradingView 代码；error 连接中断"),
        ("tv_quote_sync", "COMMENT COLUMN detail", "失败或对不上时的原因。成功时为空字符串"),
        ("tv_quote_sync", "COMMENT COLUMN checked_at", "最近一次检查时间，UTC。同一 ticker 保留更晚的一行"),
    ]
    for table, action, comment in comments:
        FIN.ch_exec(env, "ALTER TABLE %s %s %s" % (table, action, FIN.sql_str(comment)))


def existing_bars(env: dict[str, str], ticker: str) -> dict[str, tuple[float, float]]:
    rows = FIN.ch_query(
        env,
        "SELECT toString(as_of_date) AS as_of_date, close, volume FROM tv_quote_daily FINAL WHERE ticker = %s"
        % FIN.sql_str(ticker),
    )
    found = {}
    for row in rows:
        close = FIN.num(row.get("close"))
        volume = FIN.num(row.get("volume"))
        if close is None:
            continue
        found[str(row["as_of_date"])] = (close, volume if volume is not None else 0.0)
    return found


def last_stored_date(existing: dict[str, tuple[float, float]]) -> str:
    return max(existing) if existing else ""


class ChartClient:
    def __init__(self, token: str):
        self.token = token
        self.sock: socket.socket | None = None
        self.serial = 0

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self) -> None:
        quote = FIN.QuoteClient(self.token)
        quote.connect()
        self.sock = quote.sock
        quote.sock = None

    def fetch_bars(self, tv_symbol: str, count: int) -> list[list]:
        bars: list[list] = []
        for attempt in (1, 2):
            try:
                bars = self._fetch_once(tv_symbol, count)
            except (OSError, EOFError, socket.timeout):
                self.close()
                if attempt == 2:
                    raise
                continue
            if bars or attempt == 2:
                return bars
            self.close()
        return bars

    def _fetch_once(self, tv_symbol: str, count: int) -> list[list]:
        if self.sock is None:
            self.connect()
        assert self.sock is not None
        self.serial += 1
        session = "cs_" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(10))
        resolver = "sym_%d" % self.serial
        series = "sds_%d" % self.serial
        symbol = json.dumps({"symbol": tv_symbol, "adjustment": "splits"}, separators=(",", ":"))
        FIN.send_text(self.sock, FIN.wrap("chart_create_session", [session, ""]))
        FIN.send_text(self.sock, FIN.wrap("resolve_symbol", [session, resolver, "=" + symbol]))
        FIN.send_text(self.sock, FIN.wrap("create_series", [session, series, "s1", resolver, "1D", int(count)]))
        bars: list[list] = []
        deadline = time.time() + 25
        done = False
        try:
            while time.time() < deadline and not done:
                opcode, payload = FIN.recv_frame(self.sock)
                if opcode == 8:
                    raise EOFError("对方关掉了连接")
                if opcode == 9:
                    mask = os.urandom(4)
                    pong = bytearray([0x8A, 0x80 | len(payload)]) + mask
                    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                    self.sock.sendall(bytes(pong) + masked)
                    continue
                if opcode != 1:
                    continue
                for packet in FIN.parse_packets(payload.decode("utf-8", "replace")):
                    if packet.startswith("~h~"):
                        FIN.send_text(self.sock, "~m~%d~m~%s" % (len(packet), packet))
                        continue
                    try:
                        message = json.loads(packet)
                    except json.JSONDecodeError:
                        continue
                    method = message.get("m")
                    if method in ("timescale_update", "du"):
                        body = message.get("p") or []
                        block = body[1] if len(body) >= 2 and isinstance(body[1], dict) else {}
                        for series_block in block.values():
                            if not isinstance(series_block, dict):
                                continue
                            for item in series_block.get("s") or []:
                                values = item.get("v") if isinstance(item, dict) else None
                                if isinstance(values, list) and len(values) >= 5:
                                    bars.append(values)
                    elif method == "series_completed":
                        body = message.get("p") or []
                        if body and body[0] == session:
                            done = True
                    elif method in ("critical_error", "protocol_error", "symbol_error"):
                        text = json.dumps(message, ensure_ascii=False)[:240]
                        lowered = text.lower()
                        if "ban" in lowered or "captcha" in lowered or "rate" in lowered:
                            raise StopRun("行情接口限流，停下来")
                        done = True
            try:
                FIN.send_text(self.sock, FIN.wrap("chart_delete_session", [session]))
            except OSError:
                self.close()
            return bars
        except (OSError, EOFError, socket.timeout):
            self.close()
            raise


def write_sync(env, ticker, tv_symbol, last_date, bar_count, status, detail, when):
    FIN.insert_rows(
        env,
        "tv_quote_sync",
        [
            {
                "ticker": ticker,
                "tv_symbol": tv_symbol,
                "last_date": last_date,
                "bar_count": bar_count,
                "status": status,
                "detail": detail,
                "checked_at": when,
            }
        ],
    )


def sync_state(env: dict[str, str]) -> dict[str, dict]:
    try:
        rows = FIN.ch_query(env, "SELECT ticker, status, checked_at FROM tv_quote_sync FINAL")
    except RuntimeError:
        return {}
    return {row["ticker"]: row for row in rows}


def resolve_symbol(code: str, exchange: str, ticker: str = "") -> tuple[str | None, str]:
    return FIN.resolve_symbol(
        code,
        exchange,
        lambda: time.sleep(random.uniform(0.8, 1.6)),
        ticker,
    )


def run(mode: str, only: set[str], limit: int, force: bool) -> None:
    env = FIN.load_env()
    ensure_tables(env)
    token = FIN.login(env)
    items = FIN.listings(env)
    if only:
        items = [item for item in items if item["ticker"] in only]
    if limit:
        items = items[:limit]
    state = sync_state(env)
    known = FIN.known_symbols(env)
    quotes = FIN.QuoteClient(token)
    charts = ChartClient(token)
    count = FULL_BARS if mode == "full" else DAILY_BARS
    print("名单 %d，日线 %d 根" % (len(items), count), flush=True)
    updated = skipped = unresolved = empty = 0
    failures = 0
    try:
        for index, item in enumerate(items, start=1):
            ticker = item["ticker"]
            previous = state.get(ticker) or {}
            if (
                mode == "full"
                and not force
                and previous.get("status") == "ok"
                and FIN.fresh_enough(str(previous.get("checked_at") or ""), 18)
            ):
                skipped += 1
                print("%d/%d %s 这次已经写入过，跳过" % (index, len(items), ticker), flush=True)
                continue
            if (
                mode == "daily"
                and not force
                and previous.get("status") == "unresolved"
                and FIN.fresh_enough(str(previous.get("checked_at") or ""), 24 * 7)
            ):
                skipped += 1
                continue
            tv_symbol = known.get(ticker) or ""
            try:
                if not tv_symbol:
                    code, _suffix = FIN.ticker_code(ticker)
                    tv_symbol, reason = resolve_symbol(code, item.get("exchange") or "", ticker)
                    time.sleep(random.uniform(0.8, 1.6))
                    if not tv_symbol:
                        unresolved += 1
                        write_sync(env, ticker, "", "", 0, "unresolved", reason, FIN.stamp())
                        print("%d/%d %s 对不上 %s" % (index, len(items), ticker, reason), flush=True)
                        failures = 0
                        time.sleep(random.uniform(1.0, 2.0))
                        continue
                bars = charts.fetch_bars(tv_symbol, count)
                snap = quotes.fetch(tv_symbol, list(SNAPSHOT_FIELDS), require_statement=False)
            except (StopRun, FIN.StopRun):
                raise
            except (OSError, EOFError, socket.timeout, RuntimeError) as exc:
                failures += 1
                if not FIN.token_still_good(quotes.token):
                    token = FIN.login(env)
                    quotes.token = token
                    charts.token = token
                    quotes.close()
                    charts.close()
                write_sync(env, ticker, tv_symbol, "", 0, "error", "连接中断", FIN.stamp())
                print("%d/%d %s 连接中断" % (index, len(items), ticker), flush=True)
                if failures >= 8:
                    raise StopRun("连续 8 次连不上，停下来") from exc
                time.sleep(min(60, 5 * failures))
                continue
            when = FIN.stamp()
            rows = []
            for values in bars:
                row = bar_row(ticker, tv_symbol, values, when)
                if row:
                    rows.append(row)
            if not rows:
                empty += 1
                write_sync(env, ticker, tv_symbol, "", 0, "empty", "", when)
                print("%d/%d %s 没有日线" % (index, len(items), ticker), flush=True)
                failures = 0
                time.sleep(random.uniform(1.5, 3.0))
                continue
            stored = existing_bars(env, ticker)
            fresh = changed_bars(stored, rows)
            FIN.insert_rows(env, "tv_quote_daily", fresh)
            last_date = max(row["as_of_date"] for row in rows)
            shot = snapshot_row(ticker, tv_symbol, snap, last_date, when)
            if shot:
                FIN.insert_rows(env, "tv_quote_snapshot", [shot])
                last_date = shot["as_of_date"] or last_date
            status = "unchanged" if mode == "daily" and not fresh else "ok"
            write_sync(env, ticker, tv_symbol, last_date, len(fresh), status, "", when)
            known[ticker] = tv_symbol
            failures = 0
            if status == "unchanged":
                skipped += 1
            else:
                updated += 1
            print(
                "%d/%d %s %s 最近 %s 新日线 %d" % (index, len(items), ticker, tv_symbol, last_date, len(fresh)),
                flush=True,
            )
            time.sleep(random.uniform(1.6, 3.2))
    finally:
        quotes.close()
        charts.close()
    print("结束 写入 %d，没变化或已完成 %d，对不上 %d，空 %d" % (updated, skipped, unresolved, empty), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 universe 的 TradingView 日线和截止日快照")
    parser.add_argument("--daily", action="store_true", help="只补最近约 20 个交易日")
    parser.add_argument("--full", action="store_true", help="第一次把日线拉全。18 小时内已经成功的会跳过")
    parser.add_argument("--force", action="store_true", help="不跳过最近已检查的股票")
    parser.add_argument("--only", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    mode = "daily" if args.daily and not args.full else "full"
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
        run(mode, only, args.limit, args.force)
    except (StopRun, FIN.StopRun) as exc:
        print("停下：%s" % exc, flush=True)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
