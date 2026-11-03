#!/usr/bin/env python3
"""把 TradingView 概览页写成截止日快照，写入 pangu。

一页里同时有公司是做什么的、行业、规模，以及当天的估值和日历。
给研究当第一张地图用，不是已经核过的事实。CEO、行业分类以来源原文为准。

用法：
  python3 scripts/load_tv_overview.py --full
  python3 scripts/load_tv_overview.py --daily
  python3 scripts/load_tv_overview.py --only AAPL.US
"""

from __future__ import annotations

import argparse
import importlib.util
import random
import socket
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "data" / "tv_overview.lock"

OVERVIEW_FIELDS = (
    "description",
    "short_name",
    "business_description",
    "sector",
    "industry",
    "country",
    "country_code",
    "location",
    "ceo",
    "founded",
    "number_of_employees",
    "web_site_url",
    "listed_exchange",
    "isin",
    "cusip",
    "figi",
    "currency_code",
    "lp",
    "lp_time",
    "market_cap_basic",
    "market_cap_calc",
    "enterprise_value_fq",
    "total_shares_outstanding",
    "volume",
    "average_volume",
    "price_52_week_high",
    "price_52_week_low",
    "price_earnings_ttm",
    "price_book_current",
    "price_sales_current",
    "enterprise_value_ebitda_ttm",
    "earnings_per_share_basic_ttm",
    "earnings_per_share_diluted_ttm",
    "earnings_per_share_forecast_next_fq",
    "book_value_per_share_fq",
    "dividends_yield",
    "dividends_yield_current",
    "dividend_payout_ratio_fy",
    "dps_common_stock_prim_issue_fy",
    "beta_1_year",
    "beta_3_year",
    "beta_5_year",
    "total_revenue_ttm",
    "net_income_ttm",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "return_on_equity",
    "return_on_assets",
    "current_ratio",
    "debt_to_equity_fq",
    "earnings_release_date",
    "earnings_release_next_date",
    "ex_dividend_date_recent",
)


def load_quotes():
    path = Path(__file__).with_name("load_tv_quotes.py")
    spec = importlib.util.spec_from_file_location("load_tv_quotes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Q = load_quotes()
FIN = Q.FIN


class StopRun(RuntimeError):
    pass


def as_int(value):
    number = FIN.num(value)
    if number is None:
        return None
    return int(number)


def figi_of(raw) -> str:
    if isinstance(raw, dict):
        return str(raw.get("country-composite") or raw.get("exchange-level") or "")
    return str(raw or "")


def text_of(raw) -> str:
    if raw in (None, ""):
        return ""
    return str(raw).strip()


def overview_row(ticker: str, tv_symbol: str, values: dict, when: str) -> dict | None:
    day = Q.bar_date(values.get("lp_time"))
    name = text_of(values.get("description"))
    about = text_of(values.get("business_description"))
    close = FIN.num(values.get("lp"))
    if not day and close is None and not name and not about:
        return None
    if not day:
        day = when[:10]
    market_cap = FIN.num(values.get("market_cap_basic"))
    if market_cap is None:
        market_cap = FIN.num(values.get("market_cap_calc"))
    return {
        "ticker": ticker,
        "tv_symbol": tv_symbol,
        "as_of_date": day,
        "name": name,
        "short_name": text_of(values.get("short_name")),
        "business_description": about,
        "sector": text_of(values.get("sector")),
        "industry": text_of(values.get("industry")),
        "country": text_of(values.get("country")),
        "country_code": text_of(values.get("country_code")),
        "location": text_of(values.get("location")),
        "ceo": text_of(values.get("ceo")),
        "founded": as_int(values.get("founded")),
        "employees": as_int(values.get("number_of_employees")),
        "website": text_of(values.get("web_site_url")),
        "listed_exchange": text_of(values.get("listed_exchange")),
        "isin": text_of(values.get("isin")),
        "cusip": text_of(values.get("cusip")),
        "figi": figi_of(values.get("figi")),
        "currency": text_of(values.get("currency_code")),
        "close": close,
        "market_cap": market_cap,
        "enterprise_value": FIN.num(values.get("enterprise_value_fq")),
        "shares_outstanding": FIN.num(values.get("total_shares_outstanding")),
        "volume": FIN.num(values.get("volume")),
        "average_volume": FIN.num(values.get("average_volume")),
        "high_52w": FIN.num(values.get("price_52_week_high")),
        "low_52w": FIN.num(values.get("price_52_week_low")),
        "pe_ttm": FIN.num(values.get("price_earnings_ttm")),
        "pb": FIN.num(values.get("price_book_current")),
        "ps": FIN.num(values.get("price_sales_current")),
        "ev_ebitda": FIN.num(values.get("enterprise_value_ebitda_ttm")),
        "eps_ttm": FIN.num(values.get("earnings_per_share_basic_ttm")),
        "eps_diluted_ttm": FIN.num(values.get("earnings_per_share_diluted_ttm")),
        "eps_next_fq": FIN.num(values.get("earnings_per_share_forecast_next_fq")),
        "book_value_per_share": FIN.num(values.get("book_value_per_share_fq")),
        "dividend_yield": FIN.num(values.get("dividends_yield")),
        "dividend_yield_current": FIN.num(values.get("dividends_yield_current")),
        "payout_ratio": FIN.num(values.get("dividend_payout_ratio_fy")),
        "dps_fy": FIN.num(values.get("dps_common_stock_prim_issue_fy")),
        "beta_1y": FIN.num(values.get("beta_1_year")),
        "beta_3y": FIN.num(values.get("beta_3_year")),
        "beta_5y": FIN.num(values.get("beta_5_year")),
        "revenue_ttm": FIN.num(values.get("total_revenue_ttm")),
        "net_income_ttm": FIN.num(values.get("net_income_ttm")),
        "gross_margin": FIN.num(values.get("gross_margin")),
        "operating_margin": FIN.num(values.get("operating_margin")),
        "net_margin": FIN.num(values.get("net_margin")),
        "roe": FIN.num(values.get("return_on_equity")),
        "roa": FIN.num(values.get("return_on_assets")),
        "current_ratio": FIN.num(values.get("current_ratio")),
        "debt_to_equity": FIN.num(values.get("debt_to_equity_fq")),
        "last_earnings_at": Q.bar_date(values.get("earnings_release_date")),
        "next_earnings_at": Q.bar_date(values.get("earnings_release_next_date")),
        "ex_dividend_date": Q.bar_date(values.get("ex_dividend_date_recent")),
        "updated_at": when,
    }


def ensure_tables(env: dict[str, str]) -> None:
    FIN.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_overview
        (
            ticker String,
            tv_symbol String,
            as_of_date Date,
            name String,
            short_name String,
            business_description String,
            sector String,
            industry String,
            country String,
            country_code String,
            location String,
            ceo String,
            founded Nullable(Int32),
            employees Nullable(Int32),
            website String,
            listed_exchange String,
            isin String,
            cusip String,
            figi String,
            currency String,
            close Nullable(Float64),
            market_cap Nullable(Float64),
            enterprise_value Nullable(Float64),
            shares_outstanding Nullable(Float64),
            volume Nullable(Float64),
            average_volume Nullable(Float64),
            high_52w Nullable(Float64),
            low_52w Nullable(Float64),
            pe_ttm Nullable(Float64),
            pb Nullable(Float64),
            ps Nullable(Float64),
            ev_ebitda Nullable(Float64),
            eps_ttm Nullable(Float64),
            eps_diluted_ttm Nullable(Float64),
            eps_next_fq Nullable(Float64),
            book_value_per_share Nullable(Float64),
            dividend_yield Nullable(Float64),
            dividend_yield_current Nullable(Float64),
            payout_ratio Nullable(Float64),
            dps_fy Nullable(Float64),
            beta_1y Nullable(Float64),
            beta_3y Nullable(Float64),
            beta_5y Nullable(Float64),
            revenue_ttm Nullable(Float64),
            net_income_ttm Nullable(Float64),
            gross_margin Nullable(Float64),
            operating_margin Nullable(Float64),
            net_margin Nullable(Float64),
            roe Nullable(Float64),
            roa Nullable(Float64),
            current_ratio Nullable(Float64),
            debt_to_equity Nullable(Float64),
            last_earnings_at Nullable(Date),
            next_earnings_at Nullable(Date),
            ex_dividend_date Nullable(Date),
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (ticker, as_of_date)
        """,
    )
    FIN.ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_overview_sync
        (
            ticker String,
            tv_symbol String,
            last_date String,
            status String,
            detail String,
            checked_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(checked_at)
        ORDER BY ticker
        """,
    )
    comments = [
        ("tv_overview", "MODIFY COMMENT", "TradingView 概览快照。一行是一只股票一个交易日看到的介绍、规模和估值。给研究当第一张地图，不是已经核过的事实"),
        ("tv_overview", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同，例如 AAPL.US"),
        ("tv_overview", "COMMENT COLUMN tv_symbol", "TradingView 代码，例如 NASDAQ:AAPL"),
        ("tv_overview", "COMMENT COLUMN as_of_date", "这张快照对应的最近交易日，与 close 同一天。研究按 as_of_date <= 截止日取最后一行"),
        ("tv_overview", "COMMENT COLUMN name", "公司显示名，来源原文，如 Apple Inc."),
        ("tv_overview", "COMMENT COLUMN short_name", "行情简称，如 AAPL"),
        ("tv_overview", "COMMENT COLUMN business_description", "概览上的公司介绍。来源摘要，不是年报原文，不能当独立证据"),
        ("tv_overview", "COMMENT COLUMN sector", "来源行业大类，如 Electronic Technology。分类法与雅虎、彭博不同"),
        ("tv_overview", "COMMENT COLUMN industry", "来源行业小类，如 Telecommunications Equipment"),
        ("tv_overview", "COMMENT COLUMN country", "注册或归属国家英文名"),
        ("tv_overview", "COMMENT COLUMN country_code", "国家代码，如 US"),
        ("tv_overview", "COMMENT COLUMN location", "总部城市，来源原文，如 Cupertino"),
        ("tv_overview", "COMMENT COLUMN ceo", "来源写的负责人。可能不是现任 CEO，未核实。例：苹果曾写成 John Ternus"),
        ("tv_overview", "COMMENT COLUMN founded", "成立年份。没有时为空"),
        ("tv_overview", "COMMENT COLUMN employees", "员工数。没有时为空"),
        ("tv_overview", "COMMENT COLUMN website", "官网，来源原文"),
        ("tv_overview", "COMMENT COLUMN listed_exchange", "上市交易所，如 NASDAQ"),
        ("tv_overview", "COMMENT COLUMN isin", "ISIN。没有时为空字符串"),
        ("tv_overview", "COMMENT COLUMN cusip", "CUSIP。没有时为空字符串"),
        ("tv_overview", "COMMENT COLUMN figi", "FIGI 国家综合代码。没有时为空字符串"),
        ("tv_overview", "COMMENT COLUMN currency", "价格和市值货币，ISO 4217"),
        ("tv_overview", "COMMENT COLUMN close", "该日收盘。例：AAPL.US 2026-10-16 为 343.46"),
        ("tv_overview", "COMMENT COLUMN market_cap", "该日市值，货币个位"),
        ("tv_overview", "COMMENT COLUMN enterprise_value", "企业价值，货币个位。口径以来源为准"),
        ("tv_overview", "COMMENT COLUMN shares_outstanding", "已发行股本"),
        ("tv_overview", "COMMENT COLUMN volume", "该日成交量"),
        ("tv_overview", "COMMENT COLUMN average_volume", "近期日均成交量，来源计算"),
        ("tv_overview", "COMMENT COLUMN high_52w", "52 周最高价"),
        ("tv_overview", "COMMENT COLUMN low_52w", "52 周最低价"),
        ("tv_overview", "COMMENT COLUMN pe_ttm", "来源当前市盈率，TTM 口径，不是彭博 1BF PE"),
        ("tv_overview", "COMMENT COLUMN pb", "来源当前市净率"),
        ("tv_overview", "COMMENT COLUMN ps", "来源当前市销率"),
        ("tv_overview", "COMMENT COLUMN ev_ebitda", "来源当前 EV/EBITDA"),
        ("tv_overview", "COMMENT COLUMN eps_ttm", "基本每股收益，TTM"),
        ("tv_overview", "COMMENT COLUMN eps_diluted_ttm", "稀释每股收益，TTM"),
        ("tv_overview", "COMMENT COLUMN eps_next_fq", "下一季 EPS 一致预期"),
        ("tv_overview", "COMMENT COLUMN book_value_per_share", "最近季报每股账面。例：AAPL 约 7.36"),
        ("tv_overview", "COMMENT COLUMN dividend_yield", "来源股息率。0.314 表示 0.314%，不是小数 0.00314"),
        ("tv_overview", "COMMENT COLUMN dividend_yield_current", "来源当前股息率，单位与 dividend_yield 相同"),
        ("tv_overview", "COMMENT COLUMN payout_ratio", "来源最近财年派息率。13.66 表示 13.66%"),
        ("tv_overview", "COMMENT COLUMN dps_fy", "最近财年每股股息，货币个位"),
        ("tv_overview", "COMMENT COLUMN beta_1y", "一年 Beta"),
        ("tv_overview", "COMMENT COLUMN beta_3y", "三年 Beta"),
        ("tv_overview", "COMMENT COLUMN beta_5y", "五年 Beta"),
        ("tv_overview", "COMMENT COLUMN revenue_ttm", "最近十二个月营收，货币个位"),
        ("tv_overview", "COMMENT COLUMN net_income_ttm", "最近十二个月净利润，货币个位"),
        ("tv_overview", "COMMENT COLUMN gross_margin", "毛利率，小数。0.486 表示 48.6%"),
        ("tv_overview", "COMMENT COLUMN operating_margin", "营业利润率，小数"),
        ("tv_overview", "COMMENT COLUMN net_margin", "净利率，小数"),
        ("tv_overview", "COMMENT COLUMN roe", "净资产收益率，小数。1.49 表示 149%"),
        ("tv_overview", "COMMENT COLUMN roa", "总资产收益率，小数"),
        ("tv_overview", "COMMENT COLUMN current_ratio", "流动比率"),
        ("tv_overview", "COMMENT COLUMN debt_to_equity", "产权比率，来源最近季报"),
        ("tv_overview", "COMMENT COLUMN last_earnings_at", "最近一次财报日，UTC 日期"),
        ("tv_overview", "COMMENT COLUMN next_earnings_at", "预计下一次财报日，UTC 日期，可能变动"),
        ("tv_overview", "COMMENT COLUMN ex_dividend_date", "最近除息日"),
        ("tv_overview", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、as_of_date 保留更晚的一行"),
        ("tv_overview_sync", "MODIFY COMMENT", "TradingView 概览抓取进度。一行是一只股票最近一次检查的结果"),
        ("tv_overview_sync", "COMMENT COLUMN ticker", "公司库代码，与 universe.ticker 相同"),
        ("tv_overview_sync", "COMMENT COLUMN tv_symbol", "解析到的 TradingView 代码。对不上时为空字符串"),
        ("tv_overview_sync", "COMMENT COLUMN last_date", "这次写入的交易日。没有时为空字符串"),
        ("tv_overview_sync", "COMMENT COLUMN status", "最近一次结果。ok 已写入；unchanged 这一天已经有快照；empty 对上了代码但没有概览；unresolved 对不上代码；error 连接中断"),
        ("tv_overview_sync", "COMMENT COLUMN detail", "失败或对不上时的原因。成功时为空字符串"),
        ("tv_overview_sync", "COMMENT COLUMN checked_at", "最近一次检查时间，UTC。同一 ticker 保留更晚的一行"),
    ]
    for table, action, comment in comments:
        FIN.ch_exec(env, "ALTER TABLE %s %s %s" % (table, action, FIN.sql_str(comment)))


def latest_date(env: dict[str, str], ticker: str) -> str:
    rows = FIN.ch_query(
        env,
        "SELECT toString(max(as_of_date)) AS d FROM tv_overview FINAL WHERE ticker = %s" % FIN.sql_str(ticker),
    )
    if not rows:
        return ""
    return str(rows[0].get("d") or "")


def write_sync(env, ticker, tv_symbol, last_date, status, detail, when):
    FIN.insert_rows(
        env,
        "tv_overview_sync",
        [
            {
                "ticker": ticker,
                "tv_symbol": tv_symbol,
                "last_date": last_date,
                "status": status,
                "detail": detail,
                "checked_at": when,
            }
        ],
    )


def sync_state(env: dict[str, str]) -> dict[str, dict]:
    try:
        rows = FIN.ch_query(env, "SELECT ticker, status, checked_at FROM tv_overview_sync FINAL")
    except RuntimeError:
        return {}
    return {row["ticker"]: row for row in rows}


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
    client = FIN.QuoteClient(token)
    print("名单 %d" % len(items), flush=True)
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
                    tv_symbol, reason = Q.resolve_symbol(code, item.get("exchange") or "", ticker)
                    time.sleep(random.uniform(0.8, 1.6))
                    if not tv_symbol:
                        unresolved += 1
                        write_sync(env, ticker, "", "", "unresolved", reason, FIN.stamp())
                        print("%d/%d %s 对不上 %s" % (index, len(items), ticker, reason), flush=True)
                        failures = 0
                        time.sleep(random.uniform(1.0, 2.0))
                        continue
                values = client.fetch(tv_symbol, list(OVERVIEW_FIELDS), require_statement=False)
            except (StopRun, Q.StopRun, FIN.StopRun):
                raise
            except (OSError, EOFError, socket.timeout, RuntimeError) as exc:
                failures += 1
                if not FIN.token_still_good(client.token):
                    client.token = FIN.login(env)
                    client.close()
                write_sync(env, ticker, tv_symbol, "", "error", "连接中断", FIN.stamp())
                print("%d/%d %s 连接中断" % (index, len(items), ticker), flush=True)
                if failures >= 8:
                    raise StopRun("连续 8 次连不上，停下来") from exc
                time.sleep(min(60, 5 * failures))
                continue
            when = FIN.stamp()
            row = overview_row(ticker, tv_symbol, values, when)
            if not row:
                empty += 1
                write_sync(env, ticker, tv_symbol, "", "empty", "", when)
                print("%d/%d %s 没有概览" % (index, len(items), ticker), flush=True)
                failures = 0
                time.sleep(random.uniform(1.2, 2.4))
                continue
            already = latest_date(env, ticker)
            if mode == "daily" and not force and already == row["as_of_date"]:
                skipped += 1
                write_sync(env, ticker, tv_symbol, already, "unchanged", "", when)
                print("%d/%d %s %s 没有新交易日" % (index, len(items), ticker, tv_symbol), flush=True)
                failures = 0
                time.sleep(random.uniform(1.2, 2.4))
                continue
            FIN.insert_rows(env, "tv_overview", [row])
            write_sync(env, ticker, tv_symbol, row["as_of_date"], "ok", "", when)
            known[ticker] = tv_symbol
            failures = 0
            updated += 1
            print(
                "%d/%d %s %s %s %s" % (index, len(items), ticker, tv_symbol, row["as_of_date"], row["name"] or row["short_name"]),
                flush=True,
            )
            time.sleep(random.uniform(1.4, 2.8))
    finally:
        client.close()
    print("结束 写入 %d，没变化或已完成 %d，对不上 %d，空 %d" % (updated, skipped, unresolved, empty), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 universe 的 TradingView 概览快照")
    parser.add_argument("--daily", action="store_true", help="只在最近交易日还没有快照时写入")
    parser.add_argument("--full", action="store_true", help="把名单拉全。18 小时内已经成功的会跳过")
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
    except (StopRun, Q.StopRun, FIN.StopRun) as exc:
        print("停下：%s" % exc, flush=True)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
