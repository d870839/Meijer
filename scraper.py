"""
Meijer.com produce price scraper.

Sets a store by zip code, runs a list of search queries, and writes the top
match (name, price, size, url) for each query to a CSV.

Usage:
    python scraper.py --zip 49503 --input queries.csv --output results.csv
    python scraper.py --zip 49503 --input queries.csv --headed   # show browser
    python scraper.py --zip 49503 --query "strawberries"          # one-off

Notes:
  - Meijer's site is a JS SPA with bot protection, so we use Playwright (real
    browser) rather than requests/httpx.
  - The store cookie is set once per session; all subsequent searches use it.
  - Selectors are defensive (multiple fallbacks) since Meijer ships UI changes
    frequently. If a selector breaks, run with --headed --debug to inspect.
  - Respect Meijer's robots.txt and Terms of Service. This script is for
    personal/educational use; don't hammer the site.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    Response,
    TimeoutError as PWTimeout,
)

BASE_URL = "https://www.meijer.com"
SEARCH_URL_TMPL = "https://www.meijer.com/shopping/search.html?text={query}"

PRICE_RE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?\s?(?:lb|oz|ct|count|each|ea|pk|pack|pint|qt|gal|fl\s?oz|kg|g))",
    re.IGNORECASE,
)


@dataclass
class Result:
    trade_name: str
    search_query: str
    matched_name: str
    price: str
    size: str
    url: str
    status: str
    timestamp: str


# -- Network capture ---------------------------------------------------------


class SearchCapture:
    """Capture JSON responses from search/catalog endpoints during a page load."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    async def _on_response(self, response: Response) -> None:
        url = response.url.lower()
        if not any(k in url for k in ("search", "catalog", "product", "graphql")):
            return
        ctype = (response.headers or {}).get("content-type", "")
        if "json" not in ctype:
            return
        try:
            data = await response.json()
        except Exception:
            return
        self.payloads.append({"url": response.url, "data": data})

    def extract_products(self) -> list[dict]:
        """Walk captured payloads and return likely product dicts."""
        products: list[dict] = []
        for entry in self.payloads:
            products.extend(_walk_for_products(entry["data"]))
        return products


def _walk_for_products(node, depth: int = 0) -> list[dict]:
    """Recursively scan a JSON tree for objects that look like product records."""
    found: list[dict] = []
    if depth > 12:
        return found
    if isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        looks_like_product = (
            ("name" in keys or "title" in keys or "displayname" in keys)
            and ("price" in keys or "pricing" in keys or "currentprice" in keys
                 or "saleprice" in keys or "regularprice" in keys)
        )
        if looks_like_product:
            found.append(node)
        for v in node.values():
            found.extend(_walk_for_products(v, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_for_products(item, depth + 1))
    return found


def _product_to_fields(p: dict) -> dict:
    """Pull name/price/size/url from a captured product dict, best-effort."""
    name = (
        p.get("name") or p.get("title") or p.get("displayName")
        or p.get("productName") or ""
    )
    price = ""
    for key in ("currentPrice", "salePrice", "price", "regularPrice"):
        v = p.get(key)
        if isinstance(v, (int, float)):
            price = f"${v:.2f}"
            break
        if isinstance(v, str) and v.strip():
            m = PRICE_RE.search(v)
            price = f"${m.group(1)}" if m else v
            break
        if isinstance(v, dict):
            for sub in ("amount", "value", "current", "sale"):
                sv = v.get(sub)
                if isinstance(sv, (int, float)):
                    price = f"${sv:.2f}"
                    break
                if isinstance(sv, str):
                    m = PRICE_RE.search(sv)
                    if m:
                        price = f"${m.group(1)}"
                        break
            if price:
                break
    size = (
        p.get("size") or p.get("sellSize") or p.get("unitSize")
        or p.get("packageSize") or ""
    )
    if not size:
        m = SIZE_RE.search(name)
        if m:
            size = m.group(1)
    url = p.get("url") or p.get("productUrl") or p.get("canonicalUrl") or ""
    if url and not url.startswith("http"):
        url = BASE_URL + url
    return {"name": str(name).strip(), "price": price, "size": str(size).strip(), "url": url}


# -- Store selection ---------------------------------------------------------


async def set_store_by_zip(page: Page, zip_code: str, debug: bool = False) -> bool:
    """Open store selector, enter zip, pick the first store. Returns True on success."""
    print(f"[store] navigating to homepage to set zip {zip_code}")
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)

    # Open store selector
    triggers = [
        'button:has-text("Select Store")',
        'button:has-text("Change Store")',
        'a:has-text("Select Store")',
        'a:has-text("Change Store")',
        '[aria-label*="store" i][role="button"]',
        '[data-testid*="store-selector" i]',
        '[data-testid*="store" i] button',
    ]
    if not await _click_first_visible(page, triggers, "store-trigger", debug):
        print("[store] couldn't find store-selector trigger")
        return False
    await page.wait_for_timeout(1500)

    # Enter zip
    zip_inputs = [
        'input[placeholder*="zip" i]',
        'input[name*="zip" i]',
        'input[aria-label*="zip" i]',
        'input[type="search"]',
        'input[type="text"]',
    ]
    if not await _fill_first_visible(page, zip_inputs, zip_code, debug):
        print("[store] couldn't find zip input")
        return False
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(3000)

    # Pick first store
    pick_btns = [
        'button:has-text("Make My Store")',
        'button:has-text("Set as my store")',
        'button:has-text("Set as My Store")',
        'button:has-text("Select Store")',
        'button:has-text("Select")',
        '[data-testid*="select-store" i]',
    ]
    if not await _click_first_visible(page, pick_btns, "store-pick", debug):
        print("[store] couldn't find store-pick button (may have auto-selected)")
        # Some flows auto-select; fall through and trust the cookie.
    await page.wait_for_timeout(2500)
    print(f"[store] zip {zip_code} set")
    return True


async def _click_first_visible(page: Page, selectors: list[str], label: str,
                                debug: bool) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                if debug:
                    print(f"  [{label}] clicking {sel!r}")
                await loc.click()
                return True
        except Exception:
            continue
    return False


async def _fill_first_visible(page: Page, selectors: list[str], value: str,
                              debug: bool) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                if debug:
                    print(f"  [zip-input] filling {sel!r}")
                await loc.fill(value)
                return True
        except Exception:
            continue
    return False


# -- Search ------------------------------------------------------------------


async def search_top_result(page: Page, query: str, debug: bool = False) -> dict:
    """Navigate to search results for `query` and return the top product info."""
    cap = SearchCapture()
    await cap.attach(page)

    url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
    if debug:
        print(f"  [search] {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PWTimeout:
        return {"name": "", "price": "", "size": "", "url": url, "status": "timeout"}

    # Wait for the network to quiet so XHR captures fire.
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(1500)

    # Prefer captured API data
    products = cap.extract_products()
    if products:
        first = _product_to_fields(products[0])
        if first["name"]:
            return {**first, "status": "ok"}

    # Fallback: DOM scrape
    dom = await _dom_top_result(page, debug)
    if dom["name"]:
        return {**dom, "status": "ok-dom"}
    return {"name": "", "price": "", "size": "", "url": url, "status": "no-result"}


async def _dom_top_result(page: Page, debug: bool) -> dict:
    """Best-effort DOM extraction of the first product card."""
    card_selectors = [
        '[data-testid="product-tile"]',
        '[class*="product-tile"]',
        '[class*="ProductTile"]',
        '[class*="productCard"]',
        'article[class*="product" i]',
    ]
    card = None
    for sel in card_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                card = loc
                if debug:
                    print(f"  [dom] using card selector {sel!r}")
                break
        except Exception:
            continue
    if card is None:
        return {"name": "", "price": "", "size": "", "url": ""}

    # Name
    name = ""
    for sel in ['[class*="name" i]', '[class*="title" i]', 'a[href*="/shop/"]', 'h2', 'h3']:
        try:
            t = await card.locator(sel).first.inner_text(timeout=1500)
            if t and t.strip():
                name = t.strip().splitlines()[0]
                break
        except Exception:
            continue

    # Price
    price = ""
    try:
        text = await card.inner_text(timeout=2000)
    except Exception:
        text = ""
    m = PRICE_RE.search(text)
    if m:
        price = f"${m.group(1)}"

    # Size
    size = ""
    sm = SIZE_RE.search(text)
    if sm:
        size = sm.group(1)

    # URL
    url = ""
    try:
        href = await card.locator("a[href*='/shop/']").first.get_attribute("href", timeout=1500)
        if href:
            url = href if href.startswith("http") else BASE_URL + href
    except Exception:
        pass

    return {"name": name, "price": price, "size": size, "url": url}


# -- Driver ------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    queries: list[tuple[str, str]] = []
    if args.query:
        queries.append((args.query.upper(), args.query))
    else:
        if not Path(args.input).exists():
            print(f"input file not found: {args.input}", file=sys.stderr)
            return 2
        with open(args.input, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trade = row.get("trade_name", "").strip()
                q = row.get("search_query", "").strip()
                if not q:
                    continue
                queries.append((trade or q, q))
    if not queries:
        print("no queries to run", file=sys.stderr)
        return 2

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not args.headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context: BrowserContext = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        ok = await set_store_by_zip(page, args.zip, debug=args.debug)
        if not ok and args.strict_store:
            print("[fatal] could not set store; aborting", file=sys.stderr)
            await browser.close()
            return 3

        results: list[Result] = []
        for trade, q in queries:
            print(f"[search] {trade!r} -> {q!r}")
            try:
                r = await search_top_result(page, q, debug=args.debug)
            except Exception as e:
                r = {"name": "", "price": "", "size": "",
                     "url": SEARCH_URL_TMPL.format(query=q.replace(" ", "+")),
                     "status": f"error:{type(e).__name__}"}
                print(f"  [error] {e}")
            results.append(Result(
                trade_name=trade,
                search_query=q,
                matched_name=r["name"],
                price=r["price"],
                size=r["size"],
                url=r["url"],
                status=r["status"],
                timestamp=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            ))
            print(f"  -> {r['status']}: {r['name']!r} {r['price']} {r['size']}")
            await page.wait_for_timeout(int(args.delay * 1000))

        await browser.close()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    ok_count = sum(1 for r in results if r.status.startswith("ok"))
    print(f"\nwrote {out_path} ({ok_count}/{len(results)} matched)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Scrape Meijer.com produce prices by zip code.")
    p.add_argument("--zip", required=True, help="ZIP code to use for store selection")
    p.add_argument("--input", default="queries.csv",
                   help="CSV with columns trade_name,search_query (default: queries.csv)")
    p.add_argument("--output", default="results.csv", help="Output CSV path")
    p.add_argument("--query", help="Run a single ad-hoc query instead of --input")
    p.add_argument("--headed", action="store_true", help="Show the browser window")
    p.add_argument("--debug", action="store_true", help="Verbose selector logging")
    p.add_argument("--strict-store", action="store_true",
                   help="Abort if store selection fails")
    p.add_argument("--delay", type=float, default=1.5,
                   help="Seconds to wait between searches (default: 1.5)")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
