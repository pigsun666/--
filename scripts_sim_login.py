#!/usr/bin/env python3
"""cloud.lihero 登录脚本：输入账号密码，返回 access_token。"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

from playwright.async_api import Browser, BrowserContext, Locator, Page, Response, async_playwright

LOGIN_URL = "http://cloud.lihero.com:3700/login"
SLIDER_CONTAINER_XPATH = "//*[@id='vue-admin-beautiful']/div/div[2]/div[2]/div/div[2]/form/span/span/div/div/div"
LOGIN_API_URL = "http://cloud.lihero.com:3700/java2/auth/login"
TOKEN_KEYS = ("access_token", "accessToken", "token")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
window.chrome = window.chrome || { runtime: {} };
"""

JSON_TOKEN_REGEX = re.compile(r'"(?:access_token|accessToken|token)"\s*:\s*"([^"\\]{8,})"')


@dataclass
class LoginConfig:
    username: str
    password: str
    headless: bool
    timeout_ms: int
    slider_retries: int


def find_token_in_obj(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TOKEN_KEYS and isinstance(v, str) and len(v.strip()) >= 8:
                return v.strip()
        for v in obj.values():
            token = find_token_in_obj(v)
            if token:
                return token
    elif isinstance(obj, list):
        for item in obj:
            token = find_token_in_obj(item)
            if token:
                return token
    return None


def pick_token_from_text(text: str) -> str | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        token = find_token_in_obj(parsed)
        if token:
            return token
    except Exception:
        pass

    match = JSON_TOKEN_REGEX.search(text)
    if match:
        return match.group(1)

    qs = parse_qs(text, keep_blank_values=False)
    for key in TOKEN_KEYS:
        val = qs.get(key)
        if val and val[0]:
            return val[0]
    return None


async def parse_response_for_token(resp: Response) -> str | None:
    try:
        text = await resp.text()
    except Exception:
        return None
    return pick_token_from_text(text)


def is_target_login_api(resp: Response) -> bool:
    return resp.request.method.upper() == "POST" and resp.url.split("?", 1)[0] == LOGIN_API_URL


async def type_like_human(locator: Locator, text: str) -> None:
    await locator.click()
    await locator.fill("")
    for ch in text:
        await locator.type(ch, delay=random.randint(25, 80))


async def resolve_first(page: Page, selectors: list[str]) -> Locator:
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            return loc.first
    raise RuntimeError(f"未找到元素: {selectors}")


async def wait_for_vue_ready(page: Page, timeout_ms: int) -> None:
    await page.wait_for_load_state("domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 9000))
    except Exception:
        pass
    await page.wait_for_timeout(600)


async def drag_like_human(page: Page, start_x: float, start_y: float, distance: float, timeout_ms: int) -> None:
    await page.mouse.move(start_x, start_y)
    await page.wait_for_timeout(random.randint(40, 100))
    await page.mouse.down()

    moved = 0.0
    steps = random.randint(24, 34)
    for i in range(steps):
        progress = (i + 1) / steps
        moved = distance * (1 - (1 - progress) ** 3)
        await page.mouse.move(start_x + moved, start_y + random.uniform(-1.3, 1.3), steps=random.randint(1, 3))
        await page.wait_for_timeout(random.randint(8, 22))

    await page.mouse.move(start_x + moved - random.uniform(1.0, 3.2), start_y + random.uniform(-0.8, 0.8))
    await page.wait_for_timeout(random.randint(20, 60))
    await page.mouse.move(start_x + moved + random.uniform(0.8, 2.0), start_y + random.uniform(-0.8, 0.8))
    await page.wait_for_timeout(random.randint(8, 30))
    await page.mouse.up()
    await page.wait_for_timeout(min(timeout_ms, 1800))


async def handle_slider(page: Page, timeout_ms: int, retries: int) -> bool:
    container = page.locator(f"xpath={SLIDER_CONTAINER_XPATH}").first
    for _ in range(retries):
        if await container.count() == 0 or not await container.is_visible():
            await page.wait_for_timeout(400)
            continue

        box = await container.bounding_box()
        if not box or box["width"] < 80:
            await page.wait_for_timeout(300)
            continue

        handle_candidates = [
            ".slider-button",
            ".slider-handle",
            ".nc_iconfont.btn_slide",
            ".captcha-slider-button",
            ".geetest_slider_button",
            "[class*='slider'][class*='handle']",
            "[role='slider']",
        ]

        for sel in handle_candidates:
            h = container.locator(sel).first
            if await h.count() > 0 and await h.is_visible():
                hb = await h.bounding_box()
                if hb:
                    await drag_like_human(
                        page,
                        hb["x"] + hb["width"] / 2,
                        hb["y"] + hb["height"] / 2,
                        max(110, box["width"] - hb["width"] - random.randint(8, 16)),
                        timeout_ms,
                    )
                    return True

        await drag_like_human(
            page,
            box["x"] + min(22, box["width"] * 0.08),
            box["y"] + box["height"] / 2,
            max(110, box["width"] - min(36, box["width"] * 0.15)),
            timeout_ms,
        )
        return True
    return False


async def extract_token_from_storage(page: Page) -> str | None:
    token = await page.evaluate(
        """
() => {
  const keys = ['access_token', 'accessToken', 'token', 'Authorization'];
  const stores = [window.localStorage, window.sessionStorage];
  for (const store of stores) {
    for (const key of keys) {
      const val = store.getItem(key);
      if (val) return val;
    }
  }
  return null;
}
"""
    )
    if token:
        return str(token)

    for c in await page.context.cookies():
        if c.get("name", "").lower() in {"access_token", "accesstoken", "token", "authorization"} and c.get("value"):
            return c["value"]
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
    )
    await context.add_init_script(STEALTH_JS)
    context.set_default_timeout(timeout_ms)
    return context


async def perform_login(browser: Browser, cfg: LoginConfig) -> str | None:
    context = await build_context(browser, cfg.timeout_ms)
    page = await context.new_page()
    captured_tokens: list[str] = []
    login_resp_event = asyncio.Event()

    async def on_response(resp: Response) -> None:
        if not is_target_login_api(resp):
            return
        token = await parse_response_for_token(resp)
        if token:
            captured_tokens.append(token)
        login_resp_event.set()

    page.on("response", on_response)

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await wait_for_vue_ready(page, cfg.timeout_ms)

    user_input = await resolve_first(page, ["input[placeholder*='账号']", "input[placeholder*='用户名']", "input[type='text']"])
    pass_input = await resolve_first(page, ["input[placeholder*='密码']", "input[type='password']"])
    login_btn = await resolve_first(page, ["button:has-text('登录')", "button[type='submit']", ".el-button--primary"])

    await type_like_human(user_input, cfg.username)
    await type_like_human(pass_input, cfg.password)
    await handle_slider(page, timeout_ms=cfg.timeout_ms, retries=cfg.slider_retries)
    await page.wait_for_timeout(500)

    if await login_btn.is_enabled():
        await login_btn.click(delay=random.randint(50, 120))
    else:
        await page.wait_for_timeout(1600)
        if await login_btn.is_enabled():
            await login_btn.click(delay=random.randint(50, 120))

    try:
        await asyncio.wait_for(login_resp_event.wait(), timeout=cfg.timeout_ms / 1000)
    except asyncio.TimeoutError:
        pass

    await page.wait_for_timeout(800)

    token = next((t for t in captured_tokens if t), None)
    if not token:
        token = await extract_token_from_storage(page)

    await context.close()
    return token


async def main() -> None:
    parser = argparse.ArgumentParser(description="登录并返回 access_token")
    parser.add_argument("--username", required=True, help="账号")
    parser.add_argument("--password", required=True, help="密码")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True, help="是否无头模式（默认开启）")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="超时时间（毫秒）")
    parser.add_argument("--slider-retries", type=int, default=3, help="滑块重试次数")

    args = parser.parse_args()
    cfg = LoginConfig(
        username=args.username,
        password=args.password,
        headless=args.headless,
        timeout_ms=args.timeout_ms,
        slider_retries=args.slider_retries,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        try:
            token = await perform_login(browser, cfg)
        finally:
            await browser.close()

    print(f"access_token={token or ''}")


if __name__ == "__main__":
    asyncio.run(main())
