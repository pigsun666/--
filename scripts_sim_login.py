#!/usr/bin/env python3
"""Playwright 模拟登录 OAMR，并输出登录接口返回值中的 token。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Browser, BrowserContext, Locator, Page, Response, TimeoutError, async_playwright

DEFAULT_LOGIN_PAGE_URL = "https://218.77.58.37:8443/OAMR/#/login"
DEFAULT_LOGIN_API_URL = "https://218.77.58.37:8443/mocha-itom-service-login/mochaitom/opsuser/login"
TOKEN_KEYS = (
    "token",
    "access_token",
    "accessToken",
    "Authorization",
    "authorization",
    "jwt",
    "jwtToken",
    "id_token",
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
window.chrome = window.chrome || { runtime: {} };
"""


@dataclass
class LoginConfig:
    username: str
    password: str
    page_url: str
    api_url: str
    headless: bool
    timeout_ms: int


@dataclass
class CapturedLoginResult:
    request_url: str
    request_method: str
    request_headers: dict[str, str]
    request_post_data: str | None
    response_status: int
    response_headers: dict[str, str]
    response_body: str
    token: str | None


def _normalize_url(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/")


def _safe_json_loads(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except Exception:
        return None


def find_token_in_obj(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in TOKEN_KEYS and isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            token = find_token_in_obj(value)
            if token:
                return token
    elif isinstance(obj, list):
        for item in obj:
            token = find_token_in_obj(item)
            if token:
                return token
    return None


async def extract_token_from_storage(page: Page) -> str | None:
    token = await page.evaluate(
        """
() => {
  const tokenKeys = ['token', 'access_token', 'accessToken', 'Authorization', 'authorization', 'jwt', 'jwtToken', 'id_token'];
  const stores = [window.localStorage, window.sessionStorage];
  for (const store of stores) {
    for (const key of tokenKeys) {
      const value = store.getItem(key);
      if (value) return value;
    }
  }
  return null;
}
"""
    )
    if token:
        return str(token)

    for cookie in await page.context.cookies():
        if cookie.get("name") in TOKEN_KEYS and cookie.get("value"):
            return str(cookie["value"])
    return None


async def build_context(browser: Browser, timeout_ms: int) -> BrowserContext:
    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        ignore_https_errors=True,
    )
    await context.add_init_script(STEALTH_JS)
    context.set_default_timeout(timeout_ms)
    return context


async def wait_for_ui_ready(page: Page) -> None:
    await page.wait_for_load_state("domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except TimeoutError:
        pass
    await page.wait_for_timeout(800)


async def first_visible(page: Page, selectors: list[str]) -> Locator:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=1_500)
            return locator
        except TimeoutError:
            continue
    raise RuntimeError(f"未找到可见元素: {selectors}")


async def fill_input(locator: Locator, value: str) -> None:
    await locator.click()
    await locator.fill("")
    await locator.type(value, delay=40)


async def maybe_click_login(page: Page) -> None:
    button = await first_visible(
        page,
        [
            "button:has-text('登录')",
            "button:has-text('登 录')",
            "button:has-text('Login')",
            "button[type='submit']",
            ".el-button--primary",
            ".ant-btn-primary",
        ],
    )
    await button.click(delay=80)


async def capture_login_response(page: Page, cfg: LoginConfig) -> CapturedLoginResult | None:
    target_url = _normalize_url(cfg.api_url)
    capture: CapturedLoginResult | None = None
    finished = asyncio.Event()

    async def on_response(response: Response) -> None:
        nonlocal capture
        if response.request.method.upper() != "POST":
            return
        if _normalize_url(response.url) != target_url:
            return

        response_body = await response.text()
        response_json = _safe_json_loads(response_body)
        token = find_token_in_obj(response_json) if response_json is not None else None

        capture = CapturedLoginResult(
            request_url=response.request.url,
            request_method=response.request.method,
            request_headers=dict(await response.request.all_headers()),
            request_post_data=response.request.post_data,
            response_status=response.status,
            response_headers=dict(await response.all_headers()),
            response_body=response_body,
            token=token,
        )
        finished.set()

    page.on("response", on_response)

    await page.goto(cfg.page_url, wait_until="domcontentloaded")
    await wait_for_ui_ready(page)

    username_input = await first_visible(
        page,
        [
            "input[placeholder*='账号']",
            "input[placeholder*='用户']",
            "input[placeholder*='用户名']",
            "input[placeholder*='请输入账号']",
            "input[placeholder*='请输入用户名']",
            "input[type='text']",
            "input:not([type])",
        ],
    )
    password_input = await first_visible(
        page,
        [
            "input[placeholder*='密码']",
            "input[placeholder*='请输入密码']",
            "input[type='password']",
        ],
    )

    await fill_input(username_input, cfg.username)
    await fill_input(password_input, cfg.password)
    await page.wait_for_timeout(300)
    await maybe_click_login(page)

    try:
        await asyncio.wait_for(finished.wait(), timeout=cfg.timeout_ms / 1000)
    except asyncio.TimeoutError:
        return None

    if capture and not capture.token:
        capture.token = await extract_token_from_storage(page)
    return capture


def print_token(result: CapturedLoginResult) -> None:
    print(result.token or "")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Playwright 模拟登录并抓取登录接口返回值")
    parser.add_argument("--username", required=True, help="登录账号")
    parser.add_argument("--password", required=True, help="登录密码")
    parser.add_argument("--page-url", default=DEFAULT_LOGIN_PAGE_URL, help="登录页地址")
    parser.add_argument("--api-url", default=DEFAULT_LOGIN_API_URL, help="登录接口地址")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True, help="是否使用无头模式，默认开启")
    parser.add_argument("--timeout-ms", type=int, default=30_000, help="超时时间，默认 30000ms")
    args = parser.parse_args()

    cfg = LoginConfig(
        username=args.username,
        password=args.password,
        page_url=args.page_url,
        api_url=args.api_url,
        headless=args.headless,
        timeout_ms=args.timeout_ms,
    )

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=cfg.headless)
            context = await build_context(browser, cfg.timeout_ms)
            page = await context.new_page()
            try:
                result = await capture_login_response(page, cfg)
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        print(f"执行失败: {exc}", file=sys.stderr)
        return 1

    if not result:
        print(
            "未在超时时间内捕获到目标登录接口响应，请确认页面选择器、验证码、网络或接口地址是否正确。",
            file=sys.stderr,
        )
        return 2

    print_token(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
