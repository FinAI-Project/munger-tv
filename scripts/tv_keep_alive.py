#!/usr/bin/env python3
"""在 28 上续 TradingView 短会话。不弹窗、不走密码、不复用还没过期的短会话。续不上退出 2。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = Path(__file__).with_name("load_tv_financials.py")
spec = importlib.util.spec_from_file_location("load_tv_financials", path)
FIN = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FIN)


def main() -> int:
    try:
        FIN.login(FIN.load_env(), allow_password=False, reuse_saved=False)
    except FIN.StopRun as exc:
        print("停下：%s" % exc, flush=True)
        return 2
    print("ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
