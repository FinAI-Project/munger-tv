#!/usr/bin/env python3
"""在本机打开真实浏览器登录 TradingView。验证码由你点，脚本只收会话。

用法：
  scripts/tv_login.sh
  scripts/tv_login.sh --no-push
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIN_PATH = Path(__file__).with_name("load_tv_financials.py")
REMOTE = "honghua@fengheasia.com@10.0.2.28"
REMOTE_TOKEN = "/home/honghua/Projects/tradingview/data/tv_auth_token"
REMOTE_SESSION = "/home/honghua/Projects/tradingview/data/tv_session.json"


def load_financials():
    spec = importlib.util.spec_from_file_location("load_tv_financials", FIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIN = load_financials()


def token_stamp(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64

        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = float(data.get("exp") or 0)
        return datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "看不出过期时间"


def take_token(found: dict, token: str) -> None:
    if isinstance(token, str) and FIN.token_still_good(token):
        found["token"] = token


def walk_for_token(found: dict, raw) -> None:
    if isinstance(raw, dict):
        take_token(found, raw.get("auth_token") or "")
        take_token(found, raw.get("authToken") or "")
        for value in raw.values():
            walk_for_token(found, value)
    elif isinstance(raw, list):
        for item in raw:
            walk_for_token(found, item)
    elif isinstance(raw, str):
        take_token(found, raw)


async def click_first(page, selectors: list[str]) -> bool:
    for selector in selectors:
        button = page.locator(selector).first
        try:
            if await button.count() and await button.is_visible():
                await button.click()
                return True
        except Exception:
            continue
    return False


async def fill_login(page, username: str, password: str) -> None:
    try:
        user_box = page.locator('input[name="username"], input#id_username, input[type="email"], input[type="text"]').first
        await user_box.wait_for(timeout=20000)
        await user_box.fill(username)
        await click_first(page, ['button:has-text("Continue")', 'button:has-text("继续")', 'button[type="submit"]'])
        pass_box = page.locator('input[name="password"], input#id_password, input[type="password"]').first
        await pass_box.wait_for(timeout=15000)
        await pass_box.fill(password)
        await click_first(
            page,
            [
                'label:has-text("Remember")',
                'label:has-text("记住")',
                'label:has-text("保持登录")',
                'input[name="remember"]',
            ],
        )
        await click_first(page, ['button:has-text("Sign in")', 'button:has-text("登录")', 'button[type="submit"]'])
        print("账号已填。请在窗口里完成验证码，通过后不要关浏览器。", flush=True)
    except Exception:
        print("登录框还没出来，请在窗口里自己登录并完成验证码。", flush=True)


async def dismiss_banners(page) -> None:
    await click_first(
        page,
        [
            'button:has-text("接受全部")',
            'button:has-text("全部接受")',
            'button:has-text("允许全部")',
            'button:has-text("Accept all")',
            'button:has-text("Allow all")',
            'button:has-text("I agree")',
            '[data-name="accept-all"]',
        ],
    )


def has_session_cookie(state: dict) -> bool:
    for item in state.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        if item.get("name") == "sessionid" and "tradingview.com" in str(item.get("domain") or ""):
            return True
    return False


async def token_from_page(page) -> str:
    try:
        html = await page.content()
    except Exception:
        html = ""
    token = FIN.extract_auth_token(html)
    if token:
        return token
    try:
        stored = await page.evaluate(
            """() => {
                const bags = [window.localStorage, window.sessionStorage];
                const hits = [];
                for (const bag of bags) {
                    for (let i = 0; i < bag.length; i++) {
                        const value = bag.getItem(bag.key(i) || '') || '';
                        if (value.split('.').length === 3 && value.length > 80) hits.push(value);
                    }
                }
                return hits;
            }"""
        )
    except Exception:
        stored = []
    if isinstance(stored, list):
        for item in stored:
            if FIN.token_still_good(item):
                return item
    return ""


async def run(push: bool) -> None:
    env = FIN.load_env()
    username = env.get("TV_USER") or ""
    password = env.get("TV_PASSWORD") or ""
    if not username or not password:
        raise SystemExit("先在 .env 里写 TV_USER 和 TV_PASSWORD")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "还没有 Playwright。先执行：python3 -m venv .venv && .venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium"
        ) from exc

    found: dict[str, str] = {}
    token = ""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        async def on_response(response) -> None:
            url = response.url
            if "signin" not in url and "/accounts/" not in url:
                return
            try:
                if response.status != 200:
                    return
                walk_for_token(found, await response.json())
            except Exception:
                return

        page.on("response", on_response)
        await page.goto("https://cn.tradingview.com/", wait_until="domcontentloaded")
        await dismiss_banners(page)
        await fill_login(page, username, password)
        deadline = time.time() + 15 * 60
        token = ""
        state = {}
        last_note = ""
        while time.time() < deadline:
            await dismiss_banners(page)
            state = await context.storage_state()
            token = found.get("token") or await token_from_page(page)
            if has_session_cookie(state) and not FIN.token_still_good(token):
                FIN.save_session("", state)
                token = FIN.refresh_token() or token
            if FIN.token_still_good(token) and has_session_cookie(state):
                state = await context.storage_state()
                break
            names = [item.get("name") for item in state.get("cookies") or [] if isinstance(item, dict)]
            note = "%s cookies=%s token=%s" % (page.url, ",".join(str(name) for name in names[:8]), "有" if token else "无")
            if note != last_note:
                print("还在等登录被收下：%s" % note, flush=True)
                last_note = note
            await page.wait_for_timeout(2000)
        await browser.close()

    if not has_session_cookie(state):
        raise SystemExit("15 分钟内没有看到登录 Cookie。请确认是在 Google Chrome for Testing 里登录的。")
    if not FIN.token_still_good(token):
        FIN.save_session("", state)
        raise SystemExit("登录 Cookie 已保存，但还没换到短会话。可再跑一次脚本，或稍后让财务任务自己换。")

    FIN.save_session(token, state)
    print("会话已保存 %s，短会话有效到 %s" % (FIN.TOKEN_PATH, token_stamp(token)), flush=True)
    print("浏览器登录状态写在 %s，28 上短会话过期后会先用它换新的" % FIN.SESSION_PATH, flush=True)
    if not push:
        return
    FIN.TOKEN_PATH.chmod(0o600)
    FIN.SESSION_PATH.chmod(0o600)
    copied = []
    for local, remote in ((FIN.TOKEN_PATH, REMOTE_TOKEN), (FIN.SESSION_PATH, REMOTE_SESSION)):
        result = subprocess.run(
            ["scp", "-o", "BatchMode=yes", str(local), "%s:%s" % (REMOTE, remote)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit("本机已保存，同步到 28 失败：%s" % (result.stderr.strip() or result.stdout.strip() or result.returncode))
        copied.append(remote)
    print("已经同步到 28：%s" % "，".join(copied), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="本机浏览器登录 TradingView，保存会话")
    parser.add_argument("--no-push", action="store_true", help="只写本机，不同步到 28")
    args = parser.parse_args()
    import asyncio

    asyncio.run(run(push=not args.no_push))


if __name__ == "__main__":
    main()
