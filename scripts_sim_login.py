#!/usr/bin/env python3
"""cloud.lihero 统计数据采集脚本：登录后抓取 4 个统计接口并按站点聚合输出。"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import request

from playwright.async_api import Browser, BrowserContext, Locator, Page, Response, async_playwright

LOGIN_URL = "http://cloud.lihero.com:3700/login"
LOGIN_API_URL = "http://cloud.lihero.com:3700/java2/auth/login"
SLIDER_CONTAINER_XPATH = "//*[@id='vue-admin-beautiful']/div/div[2]/div[2]/div/div[2]/form/span/span/div/div/div"
ARTIFACT_DIR = Path("artifacts")
TOKEN_KEYS = ("access_token", "accessToken", "token")

CAPTURE_URL = "http://cloud.lihero.com:3700/lua/luaApi/execLua/SVR00017/air/dateCaptureControl/getListPage"
AUDIT_URL = "http://cloud.lihero.com:3700/lua/luaApi/execLua/SVR00019/air/statisticControl/getAuditListOfPage"
GROUP_FORM_URL = "http://cloud.lihero.com:3700/lua/luaApi/execLua/SVR00019/air/statisticControl/getGroupFormList"

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
window.chrome = window.chrome || { runtime: {} };
"""

JSON_TOKEN_REGEX = re.compile(r'"(?:access_token|accessToken|token)"\s*:\s*"([^"\\]{8,})"')

STATIONS = [
    {"stationId": "3543870467156773051", "topic": "hunankqzl", "mn": "07310001000113", "label": "朱良桥中学"},
    {"stationId": "3543871333643356347", "topic": "hunankqzl", "mn": "07310001000116", "label": "长沙环境保护职业技术学院"},
    {"stationId": "3502720171716413627", "topic": "hunankqzl", "mn": "430100CSKQLH01", "label": "长沙市高开区环保局"},
    {"stationId": "3543871581346367675", "topic": "hunankqzl", "mn": "07310001000115", "label": "马坡岭站"},
    {"stationId": "3543990743716628667", "topic": "hunankqzl", "mn": "07310000100001", "label": "株洲市天元区银海学校组分站"},
    {"stationId": "3543992153233459387", "topic": "hunankqzl", "mn": "0734AQMS000002", "label": "衡阳市邮政大楼颗粒物组分站"},
    {"stationId": "321232098637643785", "topic": "hunankqzl", "mn": "0734AQMS000001", "label": "衡阳市珠晖区师范学院有机物气站"},
    {"stationId": "3543993965686589627", "topic": "hunankqzl", "mn": "07390000100001", "label": "邵阳市大气组分监测站（市生态局站）"},
    {"stationId": "3566868777356265696", "topic": "hunankqzl", "mn": "MN0730BJZ00001", "label": "岳阳市丁山村边界站"},
    {"stationId": "3543991204418980027", "topic": "hunankqzl", "mn": "07370000100001", "label": "益阳市农业农村局组分站"},
    {"stationId": "3543992528900491451", "topic": "hunankqzl", "mn": "07350001000101", "label": "郴州市环境监测站"},
    {"stationId": "3543993602759755963", "topic": "hunankqzl", "mn": "07450000100001", "label": "怀化三中站"},
]
STATION_IDS = [item["stationId"] for item in STATIONS]
MN_NOS = [item["mn"] for item in STATIONS]


@dataclass
class LoginConfig:
    username: str
    password: str
    headless: bool
    timeout_ms: int
    slider_retries: int
    start_date: str | None
    end_date: str | None


@dataclass
class HttpResult:
    url: str
    method: str
    status_code: int
    body: Any


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def format_capture_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_date_arg(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def resolve_date_range(start_date: str | None, end_date: str | None) -> tuple[datetime, datetime]:
    now = utc_now()
    if start_date and end_date:
        start_dt = parse_date_arg(start_date)
        end_dt = parse_date_arg(end_date)
    elif start_date and not end_date:
        start_dt = parse_date_arg(start_date)
        end_dt = now
    elif end_date and not start_date:
        end_dt = parse_date_arg(end_date)
        start_dt = end_dt - timedelta(days=7)
    else:
        end_dt = now
        start_dt = now - timedelta(days=7)

    start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=0)
    if start_dt > end_dt:
        raise ValueError("start_date 不能晚于 end_date")
    return start_dt, end_dt


def ensure_artifact_dir() -> Path:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACT_DIR


def deep_find_token(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TOKEN_KEYS and isinstance(v, str) and len(v.strip()) >= 8:
                return v.strip()
        for v in obj.values():
            token = deep_find_token(v)
            if token:
                return token
    elif isinstance(obj, list):
        for item in obj:
            token = deep_find_token(item)
            if token:
                return token
    return None


def pick_token_from_text(text: str) -> str | None:
    if not text:
        return None
    try:
        payload = json.loads(text)
        token = deep_find_token(payload)
        if token:
            return token
    except Exception:
        pass

    match = JSON_TOKEN_REGEX.search(text)
    if match:
        return match.group(1)
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
            handle = container.locator(sel).first
            if await handle.count() > 0 and await handle.is_visible():
                hb = await handle.bounding_box()
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


async def acquire_token(browser: Browser, cfg: LoginConfig) -> str | None:
    context = await build_context(browser, cfg.timeout_ms)
    page = await context.new_page()
    tokens: list[str] = []
    login_resp_event = asyncio.Event()

    async def on_response(resp: Response) -> None:
        if not is_target_login_api(resp):
            return
        token = await parse_response_for_token(resp)
        if token:
            tokens.append(token)
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

    await context.close()
    return next((item for item in tokens if item), None)


def http_post_json(url: str, payload: dict[str, Any], token: str, timeout_s: int = 30) -> HttpResult:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {token}",
        },
    )
    with request.urlopen(req, timeout=timeout_s) as resp:
        text = resp.read().decode("utf-8")
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = text
        return HttpResult(url=url, method="POST", status_code=resp.status, body=body)


async def fetch_api_json(url: str, payload: dict[str, Any], token: str, timeout_ms: int) -> HttpResult:
    timeout_s = max(10, timeout_ms // 1000)
    return await asyncio.to_thread(http_post_json, url, payload, token, timeout_s)


def write_json_artifact(filename: str, payload: Any) -> str:
    artifact_dir = ensure_artifact_dir()
    path = artifact_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def avg(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def site_seed() -> dict[str, dict[str, Any]]:
    seeded: dict[str, dict[str, Any]] = {}
    for item in STATIONS:
        seeded[item["stationId"]] = {
            "station_id": item["stationId"],
            "station_name": item["label"],
            "mn": item["mn"],
            "topic": item["topic"],
            "capture": {},
            "audit": {},
            "qc": {},
            "ops": {},
        }
    return seeded


def merge_rows(sites: dict[str, dict[str, Any]], rows: list[dict[str, Any]], bucket: str, mn_key: str = "mn") -> None:
    for row in rows:
        station_id = str(row.get("stationId") or row.get("station_id") or "")
        if not station_id:
            continue
        site = sites.setdefault(
            station_id,
            {
                "station_id": station_id,
                "station_name": row.get("mnName") or row.get("name") or "",
                "mn": row.get(mn_key) or row.get("mn") or row.get("mnNo") or "",
                "topic": row.get("topic") or "",
                bucket: {},
            },
        )
        site.setdefault("capture", {})
        site.setdefault("audit", {})
        site.setdefault("qc", {})
        site.setdefault("ops", {})
        site["station_name"] = site.get("station_name") or row.get("mnName") or row.get("name") or ""
        site["mn"] = site.get("mn") or row.get(mn_key) or row.get("mn") or row.get("mnNo") or ""
        if row.get("topic") and not site.get("topic"):
            site["topic"] = row.get("topic")
        site[bucket] = row


def build_overview(sites: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "station_count": len(sites),
        "capture_ratio_avg": avg([safe_float(site.get("capture", {}).get("captureRatioValue")) for site in sites]),
        "stop_capture_ratio_avg": avg([safe_float(site.get("capture", {}).get("stopCaptureRatioValue")) for site in sites]),
        "valid_ratio_avg": avg([safe_float(site.get("capture", {}).get("validRatioValue")) for site in sites]),
        "audit_level1_complete_rate_avg": avg([safe_float(site.get("audit", {}).get("level1CompleteRate")) for site in sites]),
        "audit_level2_complete_rate_avg": avg([safe_float(site.get("audit", {}).get("level2CompleteRate")) for site in sites]),
        "audit_level3_complete_rate_avg": avg([safe_float(site.get("audit", {}).get("level3CompleteRate")) for site in sites]),
        "qc_complete_rate_avg": avg([safe_float(site.get("qc", {}).get("completeRate")) for site in sites]),
        "ops_complete_rate_avg": avg([safe_float(site.get("ops", {}).get("completeRate")) for site in sites]),
    }


async def collect_statistics(token: str, timeout_ms: int, start_date: str | None, end_date: str | None) -> dict[str, Any]:
    now = utc_now()
    start_dt, end_dt = resolve_date_range(start_date, end_date)

    capture_payload = {
        "pageSize": len(STATIONS),
        "pageNum": 1,
        "startTime": format_capture_time(start_dt),
        "endTime": format_capture_time(end_dt),
        "baseList": STATIONS,
    }
    audit_payload = {
        "pageSize": len(STATIONS),
        "pageNum": 1,
        "mnNos": MN_NOS,
        "startDate": format_date(start_dt),
        "endDate": format_date(end_dt),
        "stationIds": STATION_IDS,
    }
    qc_payload = {
        "workType": "2",
        "startDate": format_date(start_dt),
        "endDate": format_date(end_dt),
        "stationIds": STATION_IDS,
    }
    ops_payload = {
        "workType": "1",
        "startDate": format_date(start_dt),
        "endDate": format_date(end_dt),
        "stationIds": STATION_IDS,
    }

    capture_res, audit_res, qc_res, ops_res = await asyncio.gather(
        fetch_api_json(CAPTURE_URL, capture_payload, token, timeout_ms),
        fetch_api_json(AUDIT_URL, audit_payload, token, timeout_ms),
        fetch_api_json(GROUP_FORM_URL, qc_payload, token, timeout_ms),
        fetch_api_json(GROUP_FORM_URL, ops_payload, token, timeout_ms),
    )

    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    raw_files = {
        "capture": write_json_artifact(f"capture_{timestamp}.json", capture_res.body),
        "audit": write_json_artifact(f"audit_{timestamp}.json", audit_res.body),
        "qc": write_json_artifact(f"qc_{timestamp}.json", qc_res.body),
        "ops": write_json_artifact(f"ops_{timestamp}.json", ops_res.body),
    }

    sites = site_seed()
    merge_rows(sites, list((capture_res.body or {}).get("rows", [])), "capture", mn_key="mn")
    merge_rows(sites, list((audit_res.body or {}).get("rows", [])), "audit", mn_key="mnNo")
    merge_rows(sites, list((qc_res.body or {}).get("data", [])), "qc")
    merge_rows(sites, list((ops_res.body or {}).get("data", [])), "ops")

    site_list = sorted(sites.values(), key=lambda item: item.get("station_name") or item.get("station_id"))

    return {
        "ok": True,
        "source": "cloud.lihero.com",
        "fetched_at": now.isoformat() + "Z",
        "date_range": {
            "start": format_date(start_dt),
            "end": format_date(end_dt),
        },
        "overview": build_overview(site_list),
        "sites": site_list,
        "raw_files": raw_files,
        "requests": [
            {"name": "capture", "url": capture_res.url, "method": capture_res.method, "status_code": capture_res.status_code},
            {"name": "audit", "url": audit_res.url, "method": audit_res.method, "status_code": audit_res.status_code},
            {"name": "qc", "url": qc_res.url, "method": qc_res.method, "status_code": qc_res.status_code},
            {"name": "ops", "url": ops_res.url, "method": ops_res.method, "status_code": ops_res.status_code},
        ],
        "errors": [],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="登录后采集 4 个统计接口并输出站点聚合 JSON")
    parser.add_argument("--username", required=True, help="账号")
    parser.add_argument("--password", required=True, help="密码")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True, help="是否无头模式（默认开启）")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="超时时间（毫秒）")
    parser.add_argument("--slider-retries", type=int, default=3, help="滑块重试次数")
    parser.add_argument("--start-date", default=None, help="查询开始日期，格式 YYYY-MM-DD；默认近一周")
    parser.add_argument("--end-date", default=None, help="查询结束日期，格式 YYYY-MM-DD；默认今天")
    args = parser.parse_args()

    cfg = LoginConfig(
        username=args.username,
        password=args.password,
        headless=args.headless,
        timeout_ms=args.timeout_ms,
        slider_retries=args.slider_retries,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        try:
            token = await acquire_token(browser, cfg)
        finally:
            await browser.close()

    if not token:
        print(json.dumps({"ok": False, "errors": ["login_failed_or_token_missing"]}, ensure_ascii=False))
        return

    try:
        result = await collect_statistics(token, cfg.timeout_ms, cfg.start_date, cfg.end_date)
    except Exception as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False))
        return

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
