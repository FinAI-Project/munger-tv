#!/usr/bin/env python3
"""把 TradingView 的财务数据写入 pangu，覆盖 munger.universe 里未删除的全部股票。

每行仍是一个科目加一个期间。工业、银行、保险的科目名不一样，对不上的科目留空，不占行。

抓取方式是登录后的行情接口，不翻财务页面。一次只拉一只，拉完随机停几秒。
登录被限流、或连续失败，就停下来，不反复重试。

每天跑一次时，先看最新报告期和几条主科目有没有变。没变就跳过。
变了才拉全量，并且只把新出现或数值变了的行写进去。

用法：
  .venv/bin/python scripts/load_tv_financials.py --full
  .venv/bin/python scripts/load_tv_financials.py --daily
  .venv/bin/python scripts/load_tv_financials.py --only JPM.US,000333.CH
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
TOKEN_PATH = ROOT / "data" / "tv_auth_token"
SESSION_PATH = ROOT / "data" / "tv_session.json"
AUTH_STATUS_PATH = ROOT / "data" / "tv_auth_status.txt"
LOCK_PATH = ROOT / "data" / "tv_financials.lock"

HOST = "data.tradingview.com"
SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/v3/"
HOME_URL = "https://www.tradingview.com/"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
AUTH_TOKEN_RE = re.compile(r'"auth_token"\s*:\s*"([^"]+)"')
MUNGER_KEYS = (
    "MUNGER_CH_HOST",
    "MUNGER_CH_HTTP_PORT",
    "MUNGER_CH_DATABASE",
    "MUNGER_CH_USER",
    "MUNGER_CH_PASSWORD",
)
SUFFIX_EXCHANGES = {
    "HK": ("HKG",),
    "JP": ("JPX",),
    "TT": ("TAI",),
    "KS": ("KSC",),
    "CN": ("TOR",),
    "AU": ("ASX",),
    "LN": ("LSE",),
    "GR": ("GER",),
    "IM": ("MIL",),
    "SW": ("EBS",),
    "FP": ("PAR",),
    "NA": ("AMS",),
    "BZ": ("SAO",),
    "US": ("NMS", "NYQ", "NGM", "NCM"),
}

# 雅虎档案里的交易所代码 -> TradingView 的 source_id。只接受搜索结果里对上的那一只。
EXCHANGE_MAP = {
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NYQ": "NYSE",
    "BTS": "CBOE",
    "SHH": "SSE",
    "SHZ": "SZSE",
    "TAI": "TWSE",
    "TWO": "TPEX",
    "JPX": "TSE",
    "HKG": "HKEX",
    "KSC": "KRX",
    "TOR": "TSX",
    "GER": "XETR",
    "MIL": "MIL",
    "EBS": "SIX",
    "ASX": "ASX",
    "PAR": "EURONEXT",
    "AMS": "EURONEXT",
    "SAO": "BMFBOVESPA",
    "LSE": "LSE",
}

# (科目主干, 落在哪张表, 英文名, 父科目主干)
# 主干后面再加 _fy / _fq。银行和保险多出来的科目也放在这里，别的公司没有就跳过。
ITEMS = [
    ("total_revenue", "I", "Total revenue", ""),
    ("cost_of_goods", "I", "Cost of goods sold", ""),
    ("gross_profit", "I", "Gross profit", ""),
    ("oper_income", "I", "Operating income", ""),
    ("operating_expenses", "I", "Operating expenses (excl. COGS)", ""),
    ("total_oper_expense", "I", "Total operating expenses", ""),
    ("research_and_dev", "I", "Research & development", "sell_gen_admin_exp_total"),
    ("sell_gen_admin_exp_total", "I", "Selling/general/admin expenses (total)", "operating_expenses"),
    ("ebit", "I", "EBIT", ""),
    ("ebitda", "I", "EBITDA", ""),
    ("pretax_income", "I", "Pretax income", ""),
    ("income_tax", "I", "Taxes", ""),
    ("net_income", "I", "Net income", ""),
    ("earnings_per_share_basic", "I", "Basic earnings per share (basic EPS)", ""),
    ("earnings_per_share_diluted", "I", "Diluted earnings per share (diluted EPS)", ""),
    ("basic_shares_outstanding", "I", "Average basic shares outstanding", ""),
    ("diluted_shares_outstanding", "I", "Diluted shares outstanding", ""),
    ("net_revenue", "I", "Net revenue", ""),
    ("interest_income", "I", "Interest income", ""),
    ("interest_income_net", "I", "Net interest income", ""),
    ("interest_income_net_after_llp", "I", "Net interest Income after loan loss provision", ""),
    ("loan_loss_provision", "I", "Provision for credit losses", ""),
    ("net_revenue_after_provision", "I", "Net revenue after provisions", ""),
    ("non_interest_income", "I", "Non-interest income", ""),
    ("non_interest_expense", "I", "Non-interest expense", ""),
    ("premiums_earned", "I", "Premiums earned", ""),
    ("total_assets", "B", "Total assets", ""),
    ("total_liabilities", "B", "Total liabilities", ""),
    ("shrhldrs_equity", "B", "Shareholders' equity", "total_equity"),
    ("total_equity", "B", "Total equity", ""),
    ("cash_n_short_term_invest", "B", "Cash and short term investments", "total_current_assets"),
    ("cash_n_equivalents", "B", "Cash & equivalents", "cash_n_short_term_invest"),
    ("cash_n_due_f_banks", "B", "Total cash & due from banks", ""),
    ("total_current_assets", "B", "Total current assets", "total_assets"),
    ("total_current_liabilities", "B", "Total current liabilities", "total_liabilities"),
    ("total_debt", "B", "Total debt", ""),
    ("net_debt", "B", "Net debt", ""),
    ("short_term_debt", "B", "Short term debt", "total_current_liabilities"),
    ("long_term_debt", "B", "Long term debt", "total_non_current_liabilities"),
    ("total_non_current_liabilities", "B", "Total non-current liabilities", "total_liabilities"),
    ("total_receivables_net", "B", "Total receivables (net)", "total_current_assets"),
    ("accounts_receivables_net", "B", "Accounts receivable (trade, net)", "total_receivables_net"),
    ("common_equity_total", "B", "Total common equity", "shrhldrs_equity"),
    ("accounts_payable", "B", "Accounts payable", "total_current_liabilities"),
    ("total_inventory", "B", "Total inventory", "total_current_assets"),
    ("retained_earnings", "B", "Retained earnings", "common_equity_total"),
    ("ppe_total_net", "B", "Net property/plant/equipment", "total_non_current_assets"),
    ("goodwill", "B", "Goodwill (net)", "intangibles_net"),
    ("unearned_premiums", "B", "Unearned premiums", ""),
    ("cash_f_operating_activities", "C", "Cash flow from operating activities", ""),
    ("cash_f_investing_activities", "C", "Cash flow from investing activities", ""),
    ("cash_f_financing_activities", "C", "Cash flow from financing activities", ""),
    ("capital_expenditures", "C", "Capital expenditures", "cash_f_investing_activities"),
    ("free_cash_flow", "C", "Free cash flow", ""),
    ("total_cash_dividends_paid", "C", "Total cash dividends paid", "cash_f_financing_activities"),
    ("return_on_equity", "S", "Return on equity %", ""),
    ("return_on_assets", "S", "Return on assets %", ""),
    ("gross_margin", "S", "Gross margin %", ""),
    ("operating_margin", "S", "Operating margin %", ""),
    ("net_margin", "S", "Net margin %", ""),
    ("price_earnings", "S", "Price to earnings ratio", ""),
    ("price_book", "S", "Price to book ratio", ""),
    ("price_sales", "S", "Price to sales ratio", ""),
    ("enterprise_value_ebitda", "S", "Enterprise value to EBITDA ratio", ""),
    ("current_ratio", "S", "Current ratio", ""),
    ("debt_to_equity", "S", "Debt to equity ratio", ""),
]

TABLE = {
    "I": "tv_income_statement",
    "B": "tv_balance_sheet",
    "C": "tv_cash_flow",
    "S": "tv_statistics",
}

PROBE_KEYS = (
    "fiscal_period_fy_h",
    "fiscal_period_fq_h",
    "total_revenue_fy_h",
    "net_income_fy_h",
    "net_revenue_fy_h",
    "interest_income_net_fy_h",
    "premiums_earned_fy_h",
    "dividend_amount_h",
)

EXTRA_FIELDS = (
    "fiscal_period_fy_h",
    "fiscal_period_fq_h",
    "fundamental_currency_code",
    "revenue_seg_by_business_h",
    "revenue_seg_by_region_h",
    "dividend_amount_h",
    "dividend_ex_date_h",
    "dividend_payment_date_h",
    "dividend_type_h",
    "eps_estimates_fq_h",
    "eps_estimates_fy_h",
    "revenue_estimates_fq_h",
    "revenue_estimates_fy_h",
)


class StopRun(RuntimeError):
    """对方限流或连续失败，整批停下来。"""


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip("'").strip('"')
    return env


def ch_exec(env: dict[str, str], sql: str, data: bytes | None = None) -> str:
    params = urllib.parse.urlencode({"database": env["PANGU_CH_DATABASE"], "query": sql})
    url = "http://%s:%s/?%s" % (env["PANGU_CH_HOST"], env["PANGU_CH_HTTP_PORT"], params)
    token = base64.b64encode(("%s:%s" % (env["PANGU_CH_USER"], env["PANGU_CH_PASSWORD"])).encode()).decode()
    body = data if data is not None else b""
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(detail) from exc


def ch_query(env: dict[str, str], sql: str) -> list[dict]:
    text = ch_exec(env, sql + "\nFORMAT JSONEachRow")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


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


def ch_query_munger(env: dict[str, str], sql: str) -> list[dict]:
    text = ch_exec_munger(env, sql + "\nFORMAT JSONEachRow")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def sql_str(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def ticker_code(ticker: str) -> tuple[str, str]:
    code, sep, suffix = ticker.rpartition(".")
    if not sep:
        return ticker, ""
    return code, suffix


def inferred_exchanges(ticker: str) -> list[str]:
    code, suffix = ticker_code(ticker)
    if suffix == "CH":
        if code.startswith(("6", "9")):
            return ["SHH"]
        return ["SHZ"]
    return list(SUFFIX_EXCHANGES.get(suffix, ()))


def exchange_candidates(ticker: str, profile_exchange: str = "") -> list[str]:
    out = []
    if profile_exchange:
        out.append(profile_exchange)
    for item in inferred_exchanges(ticker):
        if item not in out:
            out.append(item)
    return out


def profile_map(query_fn) -> dict[str, dict]:
    try:
        rows = query_fn(
            """
            SELECT ticker,
                   argMax(exchange, as_of_date) AS exchange,
                   argMax(currency, as_of_date) AS currency
            FROM stock_profile
            GROUP BY ticker
            """
        )
    except RuntimeError:
        return {}
    return {str(row.get("ticker") or ""): row for row in rows if row.get("ticker")}


def ensure_tables(env: dict[str, str]) -> None:
    ch_exec(
        env,
        """
        CREATE TABLE IF NOT EXISTS tv_sync
        (
            ticker String,
            tv_symbol String,
            currency String,
            fund_view_mode String,
            last_fy String,
            last_fq String,
            probe_hash String,
            status String,
            detail String,
            checked_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(checked_at)
        ORDER BY ticker
        """,
    )
    apply_comments(env)


def apply_comments(env: dict[str, str]) -> None:
    """给已有的 tv_* 表补注释。建表语句不会改已存在的表，所以每次启动都再写一遍。"""
    mode = (
        "报表模板。industrial 工业公司；banking 银行；insurance 保险；other 以上都对不上。"
        "还没解析到 TradingView 代码时为空字符串"
    )
    currency = (
        "财务报表计价货币，ISO 4217。"
        "目前有 USD、CNY、JPY、TWD、CAD、HKD、KRW、EUR、CHF、AUD、GBP、BRL，新股票可能增加"
    )
    period_type = "期间类型。FY 年度，FQ 单季"
    period_label = "期间标签。FY 为四位年份，如 2025；FQ 为年份加季度，如 2026-Q3"
    item_id = (
        "科目代号。年度以 _fy 结尾，季度以 _fq 结尾，如 total_revenue_fy、net_income_fq。"
        "银行有 interest_income_net_fy，保险有 premiums_earned_fy"
    )
    parent_id = "父科目代号，规则与 item_id 相同。没有父科目时为空字符串"
    value = "该科目在该期的数值。金额是货币个位，不是千元。比率和每股指标用原值。现金流出在来源里常为负数"
    updated = "本行写入时间，UTC。同一主键后写入的覆盖先写入的"
    ticker = "公司库代码，与 universe.ticker 相同，例如 AAPL.US、000333.CH"
    tv_symbol = "TradingView 代码，交易所和代码用冒号连接，例如 NASDAQ:AAPL、SZSE:000333。网页地址里把冒号写成连字符"
    statements = {
        "tv_income_statement": "TradingView 利润表。一行是一只股票、一个科目、一个期间",
        "tv_balance_sheet": "TradingView 资产负债表。一行是一只股票、一个科目、一个报告期末",
        "tv_cash_flow": "TradingView 现金流量表。一行是一只股票、一个科目、一个期间",
        "tv_statistics": "TradingView 财务统计和估值比率。一行是一只股票、一个指标、一个期间",
    }
    jobs = [("tv_symbol", "MODIFY COMMENT", "TradingView 证券代码对照。一行是公司库里的一只股票，对应 TradingView 的一条行情")]
    jobs += [
        ("tv_symbol", "COMMENT COLUMN ticker", ticker),
        ("tv_symbol", "COMMENT COLUMN tv_symbol", tv_symbol),
        ("tv_symbol", "COMMENT COLUMN currency", currency),
        ("tv_symbol", "COMMENT COLUMN fund_view_mode", mode),
        ("tv_symbol", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker 保留更晚的一行"),
    ]
    for table, comment in statements.items():
        jobs.append((table, "MODIFY COMMENT", comment))
        jobs += [
            (table, "COMMENT COLUMN ticker", ticker),
            (table, "COMMENT COLUMN tv_symbol", tv_symbol),
            (table, "COMMENT COLUMN item_id", item_id),
            (table, "COMMENT COLUMN item_name", "科目英文名，与 TradingView 页面上的行名一致"),
            (table, "COMMENT COLUMN parent_id", parent_id),
            (table, "COMMENT COLUMN period_type", period_type),
            (table, "COMMENT COLUMN period_label", period_label),
            (table, "COMMENT COLUMN value", value),
            (table, "COMMENT COLUMN currency", currency),
            (table, "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、period_type、item_id、period_label 保留更晚的一行"),
        ]
    jobs += [
        ("tv_dividend", "MODIFY COMMENT", "TradingView 派息事件。一行是一次派息"),
        ("tv_dividend", "COMMENT COLUMN ticker", ticker),
        ("tv_dividend", "COMMENT COLUMN tv_symbol", tv_symbol),
        ("tv_dividend", "COMMENT COLUMN ex_date", "除息日"),
        ("tv_dividend", "COMMENT COLUMN pay_date", "发放日。来源没有发放日时，与除息日相同"),
        ("tv_dividend", "COMMENT COLUMN amount", "每股派息金额，货币个位，保留到小数点后 4 位"),
        ("tv_dividend", "COMMENT COLUMN dividend_type", "派息类型。Quarterly 季度，Annual 年度，Semi-Annual 半年度，Monthly 月度，Special 特别股息，Other 其他。来源可能增加新值"),
        ("tv_dividend", "COMMENT COLUMN currency", currency),
        ("tv_dividend", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、ex_date 保留更晚的一行"),
        ("tv_earnings", "MODIFY COMMENT", "TradingView 盈利和营收的实际值与一致预期。一行是一个指标、一个期间，含尚未公布的未来期间"),
        ("tv_earnings", "COMMENT COLUMN ticker", ticker),
        ("tv_earnings", "COMMENT COLUMN tv_symbol", tv_symbol),
        ("tv_earnings", "COMMENT COLUMN metric", "指标。eps 每股收益，revenue 营业收入"),
        ("tv_earnings", "COMMENT COLUMN period_type", period_type),
        ("tv_earnings", "COMMENT COLUMN period_label", period_label + "。含尚未公布的未来期间"),
        ("tv_earnings", "COMMENT COLUMN actual", "已公布的实际值。尚未公布时为空"),
        ("tv_earnings", "COMMENT COLUMN estimate_avg", "一致预期平均值。没有预期时为空"),
        ("tv_earnings", "COMMENT COLUMN estimate_low", "一致预期最低值。没有预期时为空"),
        ("tv_earnings", "COMMENT COLUMN estimate_high", "一致预期最高值。没有预期时为空"),
        ("tv_earnings", "COMMENT COLUMN estimate_median", "一致预期中位数。没有预期时为空"),
        ("tv_earnings", "COMMENT COLUMN n_brokers", "给出这条预期的机构数。没有时为空"),
        ("tv_earnings", "COMMENT COLUMN is_reported", "是否已经公布。1 已公布，0 尚未公布"),
        ("tv_earnings", "COMMENT COLUMN currency", currency),
        ("tv_earnings", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、metric、period_type、period_label 保留更晚的一行"),
        ("tv_segment", "MODIFY COMMENT", "TradingView 分部收入。一行是一个分部、一个期间"),
        ("tv_segment", "COMMENT COLUMN ticker", ticker),
        ("tv_segment", "COMMENT COLUMN tv_symbol", tv_symbol),
        ("tv_segment", "COMMENT COLUMN segment_kind", "分部口径。business 按产品或业务，region 按地区"),
        ("tv_segment", "COMMENT COLUMN label", "分部名称，来源原文，如 iPhone、United States"),
        ("tv_segment", "COMMENT COLUMN period_label", "期间标签，多为四位财年，如 2025"),
        ("tv_segment", "COMMENT COLUMN value", "该分部收入，货币个位"),
        ("tv_segment", "COMMENT COLUMN currency", currency),
        ("tv_segment", "COMMENT COLUMN updated_at", "本行写入时间，UTC。同一 ticker、segment_kind、period_label、label 保留更晚的一行"),
        ("tv_sync", "MODIFY COMMENT", "TradingView 抓取进度。一行是一只股票最近一次检查的结果，每天用 probe_hash 判断有没有新增或修改"),
        ("tv_sync", "COMMENT COLUMN ticker", ticker),
        ("tv_sync", "COMMENT COLUMN tv_symbol", "解析到的 TradingView 代码。对不上时为空字符串"),
        ("tv_sync", "COMMENT COLUMN currency", currency),
        ("tv_sync", "COMMENT COLUMN fund_view_mode", mode),
        ("tv_sync", "COMMENT COLUMN last_fy", "已看到的最新财年标签，如 2025。没有时为空字符串"),
        ("tv_sync", "COMMENT COLUMN last_fq", "已看到的最新季度标签，如 2026-Q3。没有时为空字符串"),
        ("tv_sync", "COMMENT COLUMN probe_hash", "最新财年、最新季度和营收、净利润、股息序列的摘要。每天用来判断有没有变化。对不上或失败时为空字符串"),
        ("tv_sync", "COMMENT COLUMN status", "最近一次结果。ok 已写入；unchanged 检查后没有变化；empty 对上了代码但接口没有报表数字；unresolved 对不上 TradingView 代码；error 连接中断"),
        ("tv_sync", "COMMENT COLUMN detail", "ok 时是各表这次写入行数的 JSON。unresolved 和 error 时是原因。没有时为空字符串"),
        ("tv_sync", "COMMENT COLUMN checked_at", "最近一次检查时间，UTC。同一 ticker 保留更晚的一行"),
    ]
    for table, action, comment in jobs:
        sql = "ALTER TABLE %s %s %s" % (table, action, sql_str(comment))
        ch_exec(env, sql)


def listings(env: dict[str, str]) -> list[dict]:
    if not munger_ready(env):
        raise StopRun("缺少 munger 库账号，读不到股票名单")
    rows = ch_query_munger(
        env,
        """
        SELECT ticker
        FROM universe FINAL
        WHERE deleted = 0
        ORDER BY rank, ticker
        """,
    )
    munger_profiles = profile_map(lambda sql: ch_query_munger(env, sql))
    pangu_profiles = profile_map(lambda sql: ch_query(env, sql))
    items = []
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        prof = munger_profiles.get(ticker) or pangu_profiles.get(ticker) or {}
        exchange = str(prof.get("exchange") or "")
        currency = str(prof.get("currency") or "")
        if not exchange:
            guessed = inferred_exchanges(ticker)
            exchange = guessed[0] if guessed else ""
        items.append({"ticker": ticker, "exchange": exchange, "currency": currency})
    return items


def sync_state(env: dict[str, str]) -> dict[str, dict]:
    rows = ch_query(
        env,
        """
        SELECT ticker, tv_symbol, currency, fund_view_mode, probe_hash, status, detail, checked_at
        FROM tv_sync FINAL
        """,
    )
    return {row["ticker"]: row for row in rows}


def match_symbol(rows: list[dict], code: str, source_id: str) -> str | None:
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
    primary = [item for item in hits if item.get("is_primary_listing")]
    pool = primary or hits
    prefixes = {(item.get("prefix") or item.get("source_id") or "") for item in pool}
    if len(prefixes) != 1:
        return None
    prefix = prefixes.pop()
    if not prefix:
        return None
    return "%s:%s" % (prefix, code)


def known_symbols(env: dict[str, str]) -> dict[str, str]:
    """财务表、文档进度、新闻进度里已经对上的代码。存托凭证通常只写在后两张表。"""
    found: dict[str, str] = {}
    queries = (
        "SELECT ticker, tv_symbol FROM tv_symbol FINAL",
        "SELECT ticker, tv_symbol FROM tv_document_sync FINAL",
        "SELECT ticker, tv_symbol FROM tv_news_sync FINAL",
    )
    for sql in queries:
        try:
            rows = ch_query(env, sql)
        except RuntimeError:
            continue
        for row in rows:
            ticker = row.get("ticker") or ""
            symbol = row.get("tv_symbol") or ""
            if ticker and symbol and ticker not in found:
                found[ticker] = symbol
    return found


def search_codes(code: str) -> list[str]:
    codes = [code]
    if "/" in code:
        codes.append(code.replace("/", "."))
    stripped = code.rstrip("/")
    if stripped and stripped not in codes:
        codes.append(stripped)
    return codes


def canon(value):
    if isinstance(value, list):
        return [canon(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) >= 1000:
            return round(number, 2)
        return round(number, 6)
    return value


def probe_hash(values: dict) -> str:
    payload = {key: canon(values.get(key)) for key in PROBE_KEYS}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def nearly_same(old: float, new: float) -> bool:
    scale = max(1.0, abs(old), abs(new))
    return abs(old - new) <= scale * 1e-6


def history_fields() -> list[str]:
    fields = []
    for base, _category, _name, _parent in ITEMS:
        fields.append(base + "_fy_h")
        fields.append(base + "_fq_h")
    fields.extend(EXTRA_FIELDS)
    return fields


def item_meta() -> dict[str, tuple[str, str, str]]:
    meta = {}
    for base, category, name, parent in ITEMS:
        for period in ("fy", "fq"):
            item_id = "%s_%s" % (base, period)
            parent_id = ("%s_%s" % (parent, period)) if parent else ""
            meta[item_id] = (category, name, parent_id)
    return meta


def num(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def has_number(values: dict, key: str) -> bool:
    series = values.get(key)
    if not isinstance(series, list):
        return False
    return any(num(item) is not None for item in series)


def has_statement(values: dict) -> bool:
    for key in (
        "fiscal_period_fy_h",
        "fiscal_period_fq_h",
        "total_revenue_fy_h",
        "net_income_fy_h",
        "net_revenue_fy_h",
    ):
        series = values.get(key)
        if isinstance(series, list) and any(item not in (None, "") for item in series):
            return True
    return False


def completion_for_us(message: dict, session: str) -> bool:
    if message.get("m") != "quote_completed":
        return False
    body = message.get("p") or []
    return bool(body) and body[0] == session


def infer_mode(values: dict) -> str:
    if has_number(values, "interest_income_net_fy_h") or has_number(values, "loan_loss_provision_fy_h"):
        return "banking"
    if has_number(values, "premiums_earned_fy_h"):
        return "insurance"
    if has_number(values, "gross_profit_fy_h") or has_number(values, "total_revenue_fy_h"):
        return "industrial"
    return "other"


def line_rows(values: dict, ticker: str, tv_symbol: str, currency: str, when: str) -> dict[str, list[dict]]:
    buckets = {name: [] for name in TABLE.values()}
    labels = {
        "fy": values.get("fiscal_period_fy_h") or [],
        "fq": values.get("fiscal_period_fq_h") or [],
    }
    meta = item_meta()
    for item_id, (category, name, parent_id) in meta.items():
        period = "fy" if item_id.endswith("_fy") else "fq"
        series = values.get(item_id + "_h")
        period_labels = labels[period]
        if not isinstance(series, list) or not isinstance(period_labels, list):
            continue
        for label, raw in zip(period_labels, series):
            value = num(raw)
            if value is None or label in (None, ""):
                continue
            buckets[TABLE[category]].append(
                {
                    "ticker": ticker,
                    "tv_symbol": tv_symbol,
                    "item_id": item_id,
                    "item_name": name,
                    "parent_id": parent_id,
                    "period_type": period.upper(),
                    "period_label": str(label),
                    "value": value,
                    "currency": currency,
                    "updated_at": when,
                }
            )
    return buckets


def as_date(raw) -> str | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if raw < 1000000000:
            return None
        return datetime.fromtimestamp(raw, timezone.utc).strftime("%Y-%m-%d")
    text = str(raw)
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    return None


def dividend_rows(values: dict, ticker: str, tv_symbol: str, currency: str, when: str) -> list[dict]:
    amounts = values.get("dividend_amount_h") or []
    exs = values.get("dividend_ex_date_h") or []
    pays = values.get("dividend_payment_date_h") or []
    kinds = values.get("dividend_type_h") or []
    rows = []
    for index in range(max(len(amounts), len(exs))):
        amount = num(amounts[index]) if index < len(amounts) else None
        ex_date = as_date(exs[index]) if index < len(exs) else None
        pay_date = as_date(pays[index]) if index < len(pays) else None
        if amount is None or ex_date is None:
            continue
        rows.append(
            {
                "ticker": ticker,
                "tv_symbol": tv_symbol,
                "ex_date": ex_date,
                "pay_date": pay_date or ex_date,
                "amount": round(amount, 4),
                "dividend_type": str(kinds[index]) if index < len(kinds) and kinds[index] is not None else "",
                "currency": currency,
                "updated_at": when,
            }
        )
    return rows


def earnings_rows(values: dict, ticker: str, tv_symbol: str, currency: str, when: str) -> list[dict]:
    rows = []
    for metric, key in (("eps", "eps_estimates"), ("revenue", "revenue_estimates")):
        for period_type, suffix in (("FQ", "_fq_h"), ("FY", "_fy_h")):
            series = values.get(key + suffix) or []
            if isinstance(series, dict):
                series = list(series.values())
            if not isinstance(series, list):
                continue
            for item in series:
                if not isinstance(item, dict):
                    continue
                label = item.get("FiscalPeriod") or item.get("fiscalPeriod") or ""
                if not label:
                    continue
                estimate = item.get("Estimate") or item.get("estimate") or {}
                if not isinstance(estimate, dict):
                    estimate = {}
                brokers = estimate.get("est_num")
                rows.append(
                    {
                        "ticker": ticker,
                        "tv_symbol": tv_symbol,
                        "metric": metric,
                        "period_type": period_type,
                        "period_label": str(label),
                        "actual": num(item.get("Actual")),
                        "estimate_avg": num(estimate.get("average")),
                        "estimate_low": num(estimate.get("low")),
                        "estimate_high": num(estimate.get("high")),
                        "estimate_median": num(estimate.get("median")),
                        "n_brokers": int(brokers) if isinstance(brokers, (int, float)) and not isinstance(brokers, bool) else None,
                        "is_reported": 1 if item.get("IsReported") else 0,
                        "currency": currency,
                        "updated_at": when,
                    }
                )
    return rows


def segment_rows(values: dict, ticker: str, tv_symbol: str, currency: str, when: str) -> list[dict]:
    rows = []
    for kind, key in (("business", "revenue_seg_by_business_h"), ("region", "revenue_seg_by_region_h")):
        series = values.get(key) or []
        if not isinstance(series, list):
            continue
        for block in series:
            if not isinstance(block, dict):
                continue
            label = str(block.get("date") or block.get("fiscalPeriod") or "")
            for seg in block.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                value = num(seg.get("value"))
                name = seg.get("label")
                if value is None or not name or not label:
                    continue
                rows.append(
                    {
                        "ticker": ticker,
                        "tv_symbol": tv_symbol,
                        "segment_kind": kind,
                        "label": str(name),
                        "period_label": label,
                        "value": value,
                        "currency": currency,
                        "updated_at": when,
                    }
                )
    return rows


def changed_lines(existing: dict[tuple[str, str], float], rows: list[dict]) -> list[dict]:
    changed = []
    for row in rows:
        key = (row["item_id"], row["period_label"])
        old = existing.get(key)
        if old is None or not nearly_same(old, row["value"]):
            changed.append(row)
    return changed


def token_still_good(token: str) -> bool:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return False
    return float(data.get("exp") or 0) > time.time() + 180


def read_saved_token() -> str:
    if not TOKEN_PATH.is_file():
        return ""
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if token and token_still_good(token):
        return token
    return ""


def save_token(token: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)


def save_session(token: str, state: dict | None = None) -> None:
    if token and token_still_good(token):
        save_token(token)
    current = load_session()
    cookies = []
    origins = []
    if isinstance(state, dict):
        cookies = state.get("cookies") or current.get("cookies") or []
        origins = state.get("origins") or current.get("origins") or []
    elif current:
        cookies = current.get("cookies") or []
        origins = current.get("origins") or []
    payload = {"token": token if token_still_good(token) else (current.get("token") or ""), "cookies": cookies, "origins": origins}
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(SESSION_PATH, 0o600)


def load_session() -> dict:
    if not SESSION_PATH.is_file():
        return {}
    try:
        raw = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def cookie_header(cookies) -> str:
    parts = []
    if not isinstance(cookies, list):
        return ""
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        value = item.get("value") or ""
        domain = str(item.get("domain") or "")
        if not name or not value or "tradingview.com" not in domain:
            continue
        parts.append("%s=%s" % (name, value))
    return "; ".join(parts)


def cookies_usable(cookies) -> bool:
    if not isinstance(cookies, list):
        return False
    now = time.time()
    for item in cookies:
        if not isinstance(item, dict) or item.get("name") != "sessionid":
            continue
        expires = float(item.get("expires") or 0)
        if expires <= 0:
            return True
        return expires > now + 60
    return False


def token_from_payload(raw) -> str:
    if isinstance(raw, dict):
        for key in ("auth_token", "authToken"):
            value = raw.get(key)
            if isinstance(value, str) and token_still_good(value):
                return value
        for value in raw.values():
            found = token_from_payload(value)
            if found:
                return found
    elif isinstance(raw, list):
        for item in raw:
            found = token_from_payload(item)
            if found:
                return found
    elif isinstance(raw, str) and token_still_good(raw):
        return raw
    return ""


def _note_refresh(text: str) -> None:
    AUTH_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_STATUS_PATH.write_text("%s %s\n" % (stamp(), text), encoding="utf-8")


def extract_auth_token(text: str) -> str:
    """从首页 HTML 或 JSON 文本里取出还能用的短会话。"""
    if not text:
        return ""
    for match in AUTH_TOKEN_RE.finditer(text):
        token = match.group(1)
        if token_still_good(token):
            return token
    return ""


def refresh_token() -> str:
    session = load_session()
    cookies = session.get("cookies") or []
    if not cookies_usable(cookies):
        _note_refresh("没有还能用的 sessionid")
        return ""
    header = cookie_header(cookies)
    if not header:
        _note_refresh("Cookie 拼不出请求头")
        return ""
    req = urllib.request.Request(HOME_URL)
    req.add_header("User-Agent", BROWSER_UA)
    req.add_header("Cookie", header)
    req.add_header("Referer", HOME_URL)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        _note_refresh("首页换会话返回 %s" % exc.code)
        return ""
    except urllib.error.URLError as exc:
        _note_refresh("首页换会话连不上：%s" % exc.reason)
        return ""
    if "is-not-authenticated" in body:
        _note_refresh("首页显示未登录，浏览器登录状态已经失效")
        return ""
    token = extract_auth_token(body)
    if token:
        save_session(token, session)
        _note_refresh("首页换到了新的短会话")
        return token
    _note_refresh("首页已打开，但没有还能用的短会话")
    return ""


def browser_refresh_token() -> str:
    """用已保存的浏览器登录状态在无界面 Chromium 里换短会话。验证码点不了，续不上就返回空。"""
    session = load_session()
    cookies = session.get("cookies") or []
    if not cookies_usable(cookies):
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _note_refresh("这台机器没有 Playwright，没法用浏览器续会话")
        return ""
    state = {"cookies": cookies, "origins": session.get("origins") or []}
    token = ""
    html = ""
    new_state = state
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(storage_state=state)
            page = context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
            html = page.content()
            token = extract_auth_token(html)
            if not token:
                try:
                    found = page.evaluate(
                        """() => {
                            const user = window.user || {};
                            return user.auth_token || user.authToken || '';
                        }"""
                    )
                except Exception as exc:
                    _note_refresh("浏览器里取会话失败：%s" % exc)
                    found = ""
                if isinstance(found, str) and token_still_good(found):
                    token = found
            new_state = context.storage_state()
            browser.close()
    except Exception as exc:
        _note_refresh("无界面浏览器没起来：%s" % exc)
        return ""
    if token_still_good(token):
        save_session(token, new_state)
        _note_refresh("浏览器换到了新的短会话")
        return token
    if "is-not-authenticated" in html:
        _note_refresh("浏览器首页显示未登录")
    elif html:
        _note_refresh("浏览器首页没有还能用的短会话")
    return ""


def login(env: dict[str, str], *, allow_password: bool = True, reuse_saved: bool = True) -> str:
    if reuse_saved:
        saved = read_saved_token()
        if saved:
            return saved
    refreshed = refresh_token()
    if refreshed:
        print("已用浏览器登录状态换到新的短会话", flush=True)
        return refreshed
    browsed = browser_refresh_token()
    if browsed:
        print("已用无界面浏览器换到新的短会话", flush=True)
        return browsed
    if cookies_usable((load_session().get("cookies") or [])):
        raise StopRun("浏览器登录状态还在，但换不到新会话。本机跑 scripts/tv_login.sh")
    if not allow_password:
        raise StopRun("没有还能用的短会话。本机跑 scripts/tv_login.sh")
    return password_login(env)


def password_login(env: dict[str, str]) -> str:
    data = urllib.parse.urlencode(
        {"username": env["TV_USER"], "password": env["TV_PASSWORD"], "remember": "on"}
    ).encode()
    req = urllib.request.Request(
        "https://www.tradingview.com/accounts/signin/",
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.tradingview.com/",
            "Origin": "https://www.tradingview.com",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:180]
        raise StopRun("登录失败 %s" % detail) from exc
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StopRun("登录返回不是 JSON") from exc
    if obj.get("code") == "rate_limit":
        raise StopRun("登录被限流，需要本机跑 scripts/tv_login.sh 点一次验证码")
    user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
    token = user.get("auth_token") or ""
    if not token:
        raise StopRun("登录没有拿到会话，需要本机跑 scripts/tv_login.sh")
    save_token(token)
    return token


def search_symbol(text: str) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "text": text,
            "hl": "0",
            "exchange": "",
            "lang": "en",
            "search_type": "stocks",
            "domain": "production",
        }
    )
    req = urllib.request.Request(
        SEARCH_URL + "?" + query,
        headers={"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    rows = data.get("symbols") or []
    return rows if isinstance(rows, list) else []


def resolve_symbol(
    code: str,
    exchange: str,
    pause,
    ticker: str = "",
    match=None,
) -> tuple[str | None, str]:
    matcher = match or match_symbol
    last_error = "没有对应的交易所"
    tried = 0
    for yahoo_ex in exchange_candidates(ticker, exchange):
        source_id = EXCHANGE_MAP.get(yahoo_ex or "")
        if not source_id:
            continue
        last_error = "搜索结果里没有 %s:%s" % (source_id, code)
        for candidate in search_codes(code):
            if tried:
                pause()
            tried += 1
            try:
                rows = search_symbol(candidate)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 403):
                    raise StopRun("代码搜索被限流") from exc
                last_error = "搜索失败 %s" % exc.code
                continue
            except urllib.error.URLError:
                last_error = "搜索失败"
                continue
            found = matcher(rows, candidate, source_id)
            if found:
                return found, ""
    return None, last_error


def recvn(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise EOFError("连接中途断了")
        buf += chunk
    return buf


def send_text(sock: socket.socket, text: str) -> None:
    data = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    header += mask
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    sock.sendall(bytes(header) + masked)


def recv_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = recvn(sock, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", recvn(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recvn(sock, 8))[0]
    if second & 0x80:
        mask = recvn(sock, 4)
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(recvn(sock, length)))
    else:
        payload = recvn(sock, length)
    return opcode, payload


def wrap(method: str, params: list) -> str:
    body = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    return "~m~%d~m~%s" % (len(body), body)


def parse_packets(text: str) -> list[str]:
    out = []
    index = 0
    while text.startswith("~m~", index):
        index += 3
        mark = text.find("~m~", index)
        if mark < 0:
            break
        length = int(text[index:mark])
        index = mark + 3
        out.append(text[index : index + length])
        index += length
    return out


class QuoteClient:
    def __init__(self, token: str):
        self.token = token
        self.sock: socket.socket | None = None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self) -> None:
        self.close()
        raw = socket.create_connection((HOST, 443), timeout=20)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET /socket.io/websocket HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://cn.tradingview.com\r\n"
            "User-Agent: Mozilla/5.0\r\n\r\n"
        ) % (HOST, key)
        sock.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            piece = sock.recv(4096)
            if not piece:
                break
            data += piece
        head = data.split(b"\r\n\r\n", 1)[0]
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise StopRun("行情连接没有建立")
        sock.settimeout(25)
        self.sock = sock
        send_text(sock, wrap("set_auth_token", [self.token]))

    def fetch(self, tv_symbol: str, fields: list[str], require_statement: bool = True) -> dict:
        merged: dict = {}
        for attempt in (1, 2):
            merged = self._fetch_once(tv_symbol, fields, require_statement)
            if (has_statement(merged) if require_statement else merged) or attempt == 2:
                return merged
            self.close()
        return merged

    def _fetch_once(self, tv_symbol: str, fields: list[str], require_statement: bool = True) -> dict:
        if self.sock is None:
            self.connect()
        assert self.sock is not None
        session = "qs_" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(10))
        try:
            send_text(self.sock, wrap("quote_create_session", [session]))
            send_text(self.sock, wrap("quote_set_fields", [session, *fields]))
            send_text(self.sock, wrap("quote_add_symbols", [session, tv_symbol]))
            send_text(self.sock, wrap("quote_fast_symbols", [session, tv_symbol]))
            merged: dict = {}
            deadline = time.time() + 25
            done = False
            while time.time() < deadline and not done:
                opcode, payload = recv_frame(self.sock)
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
                for packet in parse_packets(payload.decode("utf-8", "replace")):
                    if packet.startswith("~h~"):
                        send_text(self.sock, "~m~%d~m~%s" % (len(packet), packet))
                        continue
                    try:
                        message = json.loads(packet)
                    except json.JSONDecodeError:
                        continue
                    method = message.get("m")
                    if method == "qsd":
                        body = message.get("p") or []
                        if len(body) >= 2 and isinstance(body[1], dict):
                            merged.update(body[1].get("v") or {})
                    elif completion_for_us(message, session) and (has_statement(merged) or not require_statement):
                        done = True
                    elif method in ("critical_error", "protocol_error"):
                        text = json.dumps(message, ensure_ascii=False)[:240]
                        lowered = text.lower()
                        if "ban" in lowered or "captcha" in lowered or "rate" in lowered:
                            raise StopRun("行情接口限流，停下来")
                        done = True
            try:
                send_text(self.sock, wrap("quote_delete_session", [session]))
            except OSError:
                self.close()
            return merged
        except (OSError, EOFError, socket.timeout):
            self.close()
            raise


def insert_rows(env: dict[str, str], table: str, rows: list[dict]) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
    ch_exec(env, "INSERT INTO %s FORMAT JSONEachRow\n" % table, payload)


def existing_lines(env: dict[str, str], table: str, ticker: str) -> dict[tuple[str, str], float]:
    rows = ch_query(
        env,
        "SELECT item_id, period_label, value FROM %s FINAL WHERE ticker = %s"
        % (table, sql_str(ticker)),
    )
    return {(row["item_id"], row["period_label"]): float(row["value"]) for row in rows}


def write_symbol(env: dict[str, str], ticker: str, tv_symbol: str, currency: str, mode: str, values: dict, when: str) -> dict[str, int]:
    counts = {}
    grouped = line_rows(values, ticker, tv_symbol, currency, when)
    for table, rows in grouped.items():
        fresh = changed_lines(existing_lines(env, table, ticker), rows)
        insert_rows(env, table, fresh)
        counts[table] = len(fresh)
    dividends = dividend_rows(values, ticker, tv_symbol, currency, when)
    insert_rows(env, "tv_dividend", dividends)
    counts["tv_dividend"] = len(dividends)
    earnings = earnings_rows(values, ticker, tv_symbol, currency, when)
    insert_rows(env, "tv_earnings", earnings)
    counts["tv_earnings"] = len(earnings)
    segments = segment_rows(values, ticker, tv_symbol, currency, when)
    insert_rows(env, "tv_segment", segments)
    counts["tv_segment"] = len(segments)
    insert_rows(
        env,
        "tv_symbol",
        [
            {
                "ticker": ticker,
                "tv_symbol": tv_symbol,
                "currency": currency,
                "fund_view_mode": mode,
                "updated_at": when,
            }
        ],
    )
    return counts


def write_sync(env: dict[str, str], row: dict) -> None:
    insert_rows(env, "tv_sync", [row])


def period_ends(values: dict) -> tuple[str, str]:
    fy = values.get("fiscal_period_fy_h") or []
    fq = values.get("fiscal_period_fq_h") or []
    last_fy = str(fy[0]) if isinstance(fy, list) and fy else ""
    last_fq = str(fq[0]) if isinstance(fq, list) and fq else ""
    return last_fy, last_fq


def fresh_enough(checked_at: str, hours: int) -> bool:
    try:
        moment = datetime.strptime(checked_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - moment).total_seconds() < hours * 3600


def pause_between(low: float, high: float) -> None:
    time.sleep(random.uniform(low, high))


def run(mode: str, only: set[str], limit: int, force: bool = False) -> None:
    env = load_env()
    ensure_tables(env)
    token = login(env)
    items = listings(env)
    if only:
        items = [item for item in items if item["ticker"] in only]
    if limit:
        items = items[:limit]
    state = sync_state(env)
    known = known_symbols(env)
    client = QuoteClient(token)
    full_fields = history_fields()
    probe_fields = list(PROBE_KEYS)
    print("名单 %d，字段 %d" % (len(items), len(full_fields)), flush=True)
    failures = 0
    done = skipped = unresolved = empty = updated = 0
    try:
        for index, item in enumerate(items, start=1):
            ticker = item["ticker"]
            previous = state.get(ticker) or {}
            if (
                mode == "full"
                and not force
                and previous.get("status") == "ok"
                and fresh_enough(str(previous.get("checked_at") or ""), 18)
            ):
                skipped += 1
                print("%d/%d %s 这次已经写入过，跳过" % (index, len(items), ticker), flush=True)
                continue
            if (
                mode == "daily"
                and not force
                and previous.get("status") == "unresolved"
                and fresh_enough(str(previous.get("checked_at") or ""), 24 * 7)
            ):
                skipped += 1
                continue
            code, _suffix = ticker_code(ticker)
            tv_symbol = known.get(ticker) or ""
            try:
                if not tv_symbol:
                    tv_symbol, reason = resolve_symbol(
                        code,
                        item.get("exchange") or "",
                        lambda: pause_between(0.8, 1.6),
                        ticker,
                    )
                    pause_between(0.8, 1.6)
                    if not tv_symbol:
                        unresolved += 1
                        write_sync(
                            env,
                            {
                                "ticker": ticker,
                                "tv_symbol": "",
                                "currency": item.get("currency") or "",
                                "fund_view_mode": "",
                                "last_fy": "",
                                "last_fq": "",
                                "probe_hash": "",
                                "status": "unresolved",
                                "detail": reason,
                                "checked_at": stamp(),
                            },
                        )
                        print("%d/%d %s 对不上 %s" % (index, len(items), ticker, reason), flush=True)
                        failures = 0
                        pause_between(2.0, 4.0)
                        continue
                if mode == "daily" and not force and previous.get("probe_hash"):
                    probed = client.fetch(tv_symbol, probe_fields)
                    digest = probe_hash(probed)
                    if digest == previous.get("probe_hash"):
                        skipped += 1
                        last_fy, last_fq = period_ends(probed)
                        write_sync(
                            env,
                            {
                                "ticker": ticker,
                                "tv_symbol": tv_symbol,
                                "currency": previous.get("currency") or item.get("currency") or "",
                                "fund_view_mode": previous.get("fund_view_mode") or "",
                                "last_fy": last_fy,
                                "last_fq": last_fq,
                                "probe_hash": digest,
                                "status": "unchanged",
                                "detail": previous.get("detail") or "",
                                "checked_at": stamp(),
                            },
                        )
                        print("%d/%d %s 没有变化" % (index, len(items), ticker), flush=True)
                        failures = 0
                        pause_between(2.0, 4.0)
                        continue
                    values = client.fetch(tv_symbol, full_fields)
                else:
                    values = client.fetch(tv_symbol, full_fields)
            except StopRun:
                raise
            except (OSError, EOFError, socket.timeout) as exc:
                failures += 1
                if not token_still_good(client.token):
                    client.token = login(env)
                    client.close()
                write_sync(
                    env,
                    {
                        "ticker": ticker,
                        "tv_symbol": tv_symbol,
                        "currency": item.get("currency") or "",
                        "fund_view_mode": "",
                        "last_fy": "",
                        "last_fq": "",
                        "probe_hash": "",
                        "status": "error",
                        "detail": "连接中断",
                        "checked_at": stamp(),
                    },
                )
                print("%d/%d %s 连接中断" % (index, len(items), ticker), flush=True)
                if failures >= 8:
                    raise StopRun("连续 8 次连不上，停下来") from exc
                time.sleep(min(60, 5 * failures))
                continue
            currency = values.get("fundamental_currency_code") or item.get("currency") or ""
            if not isinstance(currency, str):
                currency = item.get("currency") or ""
            view = infer_mode(values)
            when = stamp()
            counts = write_symbol(env, ticker, tv_symbol, currency, view, values, when)
            last_fy, last_fq = period_ends(values)
            total = sum(counts.values())
            status = "ok" if total or has_number(values, "total_revenue_fy_h") or has_number(values, "net_income_fy_h") else "empty"
            if status == "empty":
                empty += 1
            else:
                updated += 1
            write_sync(
                env,
                {
                    "ticker": ticker,
                    "tv_symbol": tv_symbol,
                    "currency": currency,
                    "fund_view_mode": view,
                    "last_fy": last_fy,
                    "last_fq": last_fq,
                    "probe_hash": probe_hash(values),
                    "status": status,
                    "detail": json.dumps(counts, ensure_ascii=False),
                    "checked_at": when,
                },
            )
            known[ticker] = tv_symbol
            failures = 0
            print("%d/%d %s %s %s 写入 %s" % (index, len(items), ticker, tv_symbol, view, counts), flush=True)
            done += 1
            pause_between(2.0, 4.0)
    finally:
        client.close()
    print(
        "结束 写入 %d，没变化或已完成 %d，对不上 %d，空 %d" % (updated, skipped, unresolved, empty),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 munger.universe 的 TradingView 财务数据")
    parser.add_argument("--daily", action="store_true", help="只补新增或数值有变化的股票")
    parser.add_argument("--full", action="store_true", help="第一次把名单拉全。18 小时内已经成功的会跳过")
    parser.add_argument("--force", action="store_true", help="不跳过最近已检查的股票，用来补新科目或重新对码")
    parser.add_argument("--only", default="", help="只跑这些代码，逗号分隔")
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
    except StopRun as exc:
        print("停下：%s" % exc, flush=True)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
