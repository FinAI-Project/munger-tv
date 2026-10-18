# TradingView 抓取

从 Munger 拆出来的独立任务。从 TradingView 拉日线快照、财务、新闻、历史文档，写入 pangu。Munger 只读这些表，不再负责抓取。

```bash
cd ~/Projects/tradingview
cp .env.example .env
# 填 pangu 写账号，以及 TradingView 登录名和密码
python3 -m unittest discover -s tests
python3 scripts/load_tv_quotes.py --full
python3 scripts/load_tv_overview.py --full
python3 scripts/load_tv_financials.py --full --force
python3 scripts/load_tv_news.py --daily
python3 scripts/load_tv_documents.py --daily
```

`--full` 是第一次把名单拉全。`--daily` 只检查有没有新交易日、新财报、新新闻、新文件。财务加了新科目时，用 `--force` 再拉一遍，否则增量看主科目没变就会跳过。

也可以一次跑完：

```bash
scripts/tv_full.sh      # 日线全量 + 概览全量 + 财务强制全量 + 新闻/文档增量
scripts/tv_daily.sh     # 日线、概览、财务、新闻、文档都走增量
```

28 上每天 5:30 跑 `scripts/tv_daily.sh`。日志在 `data/tv_daily.log`。全量日志在 `data/tv_full.log`。登录会话缓存在 `data/tv_auth_token`。

日线写入 `tv_quote_daily` 和 `tv_quote_snapshot`（收盘、股本、市值同一天）。这是 TradingView 的交叉核对，不覆盖雅虎的 `quote_daily`。

概览写入 `tv_overview`：公司介绍、行业、规模，以及同一天的估值、股息率、Beta、每股账面和下一次财报日。给研究当第一张地图，不是已经核过的事实。研究按 `as_of_date <= 截止日` 取最后一行。

财务登录要验证码时，在本机执行：

```bash
scripts/tv_login.sh
```

会弹出浏览器。你点完验证码后，脚本把短会话和浏览器登录状态写到本机 `data/`，并同步到 28。28 上半夜跑财务和日线时，短会话过期会先用这份登录状态换新的，不必再敲用户名密码。换不到时，再在本机跑一次这个脚本点验证码。
