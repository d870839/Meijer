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
from datetime import datetime, timezone
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
# Captures "$2.49/lb", "$0.16 / oz", etc. — Meijer's tile-shown unit price.
UNIT_PRICE_RE = re.compile(
    r"\$\s?(\d+(?:\.\d{1,2})?)\s*/\s*(lb|oz|ea|each|count|ct|kg|g|pint|qt|gal|fl\s?oz|ml|l)\b",
    re.IGNORECASE,
)
# Same idea but for "($0.16/oz)" parenthetical variants without a leading $/.
UNIT_PRICE_PAREN_RE = re.compile(
    r"\(\s*\$?\s?(\d+(?:\.\d{1,2})?)\s*/\s*(lb|oz|ea|each|count|ct|kg|g)\s*\)",
    re.IGNORECASE,
)


@dataclass
class Result:
    zip_code: str
    trade_name: str
    search_query: str
    rank: int
    matched_name: str
    price: str
    size: str
    unit_price: str
    unit_price_basis: str
    url: str
    status: str
    date: str
    timestamp: str


# -- Network capture ---------------------------------------------------------


SEARCH_URL_INCLUDE = ("search",)
# Recommendations / promo / sponsored / personalization endpoints also contain
# product objects, so we exclude them — they were the reason every query
# returned the same featured-produce item.
SEARCH_URL_EXCLUDE = (
    "recommend", "personaliz", "sponsored", "bazaarvoice",
    "promo", "banner", "trending", "bestseller", "featured",
    "similar", "related", "suggest", "carousel", "merchandised",
    "analytics", "telemetry",
    # Ad/marketing/tag networks that pass the page URL as a query param
    # (and thus contain "search" if the user is on a search page).
    "emarsys.net", "teads.tv", "doubleclick", "googletagmanager",
    "google-analytics", "facebook.com", "scorecardresearch",
    "adservice", "adsystem", "rubiconproject", "criteo", "pubmatic",
    "bluekai", "demdex", "krxd", "segment.io", "mparticle",
    "newrelic", "datadog", "sentry.io", "fullstory",
)


class SearchCapture:
    """Capture JSON responses from search endpoints during a page load.

    One instance per query; uses a context-manager-style attach/detach so
    listeners don't leak across queries on the same page.
    """

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self._page: Optional[Page] = None
        self._handler = None

    def attach(self, page: Page) -> None:
        self._page = page
        self._handler = self._on_response
        page.on("response", self._handler)

    def detach(self) -> None:
        if self._page and self._handler:
            try:
                self._page.remove_listener("response", self._handler)
            except Exception:
                pass
        self._page = None
        self._handler = None

    async def _on_response(self, response: Response) -> None:
        url = response.url.lower()
        if not any(k in url for k in SEARCH_URL_INCLUDE):
            return
        if any(k in url for k in SEARCH_URL_EXCLUDE):
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


def _query_words(q: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z]{3,}", q)}


def _name_score(name: str, qwords: set[str]) -> int:
    """Count how many query words appear in the product name."""
    if not name or not qwords:
        return 0
    nwords = {w.lower() for w in re.findall(r"[a-zA-Z]{3,}", name)}
    return len(qwords & nwords)


def _best_products(products: list[dict], query: str, n: int) -> list[dict]:
    """Pick the top N products whose names best match the query.

    Drops any product whose name has zero overlap with the query (those are
    typically banner/recommendation items that snuck through filters).
    """
    if not products or n <= 0:
        return []
    qwords = _query_words(query)
    scored: list[tuple[int, int, dict]] = []  # (score, -idx, product)
    for i, p in enumerate(products):
        name = p.get("name") or p.get("title") or p.get("displayName") or ""
        score = _name_score(str(name), qwords)
        if score == 0:
            continue
        scored.append((score, -i, p))
    scored.sort(reverse=True)
    return [p for _, _, p in scored[:n]]


# -- Unit price --------------------------------------------------------------


# Conversion factors to canonical units: weight→/lb, count→/ea.
# Volume units (pint/qt/gal/ml/l/fl oz) aren't convertible to /lb without
# density, so they pass through as their own basis.
_TO_LB: dict[str, float] = {
    "lb": 1.0,
    "oz": 1.0 / 16.0,         # 16 oz = 1 lb
    "kg": 2.2046226,
    "g": 1.0 / 453.59237,
}
_COUNT_UNITS = {"ea", "each", "ct", "count", "pk", "pack"}


def _normalize_unit(u: str) -> str:
    u = u.lower().replace(" ", "")
    if u in {"each"}:
        return "ea"
    if u in {"count"}:
        return "ct"
    return u


def unit_price_from_text(text: str) -> tuple[str, str]:
    """Pull a literal '$X.XX/unit' string out of tile text. Returns
    (formatted_unit_price, basis) e.g. ('$1.55', '/lb'). ('','') if absent."""
    if not text:
        return "", ""
    for rx in (UNIT_PRICE_RE, UNIT_PRICE_PAREN_RE):
        m = rx.search(text)
        if m:
            amt = float(m.group(1))
            unit = _normalize_unit(m.group(2))
            return f"${amt:.2f}", f"/{unit}"
    return "", ""


def derive_unit_price(price: str, size: str) -> tuple[str, str]:
    """Compute $/lb (for weight) or $/ea (for count) from price + size.
    Returns ('', '') when units don't permit a clean conversion."""
    if not price or not size:
        return "", ""
    pm = PRICE_RE.search(price)
    sm = re.search(
        r"(\d+(?:\.\d+)?)\s*(lb|oz|ct|count|each|ea|pk|pack|kg|g)\b",
        size, re.IGNORECASE,
    )
    if not pm or not sm:
        return "", ""
    p = float(pm.group(1))
    qty = float(sm.group(1))
    unit = _normalize_unit(sm.group(2))
    if qty <= 0:
        return "", ""
    if unit in _TO_LB:
        lb = qty * _TO_LB[unit]
        if lb <= 0:
            return "", ""
        return f"${p / lb:.2f}", "/lb"
    if unit in _COUNT_UNITS:
        return f"${p / qty:.2f}", "/ea"
    return "", ""


def fill_unit_price(fields: dict, source_text: str = "") -> dict:
    """Populate fields['unit_price'] and ['unit_price_basis'] in place.

    Prefers an explicit '$X.XX/lb' string in source_text (the tile text or
    captured product description). Falls back to deriving from price+size.
    """
    up, basis = unit_price_from_text(source_text)
    if not up:
        up, basis = unit_price_from_text(fields.get("name", ""))
    if not up:
        up, basis = derive_unit_price(fields.get("price", ""), fields.get("size", ""))
    fields["unit_price"] = up
    fields["unit_price_basis"] = basis
    return fields


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

    # Wait for the store-list cards to render. Each card includes a
    # "Miles away" text; that's a reliable anchor.
    try:
        await page.wait_for_selector('text=/Miles away/i', timeout=8000)
    except PWTimeout:
        if debug:
            print("  [store-pick] no 'Miles away' text — store list may not have rendered")

    if not await _pick_first_store(page, debug):
        print("[store] couldn't pick a store")
        return False
    await page.wait_for_timeout(1500)

    # Optional follow-up: confirm/save/close (some flows auto-apply)
    confirm_btns = [
        'button:has-text("Save")',
        'button:has-text("Continue")',
        'button:has-text("Confirm")',
        'button:has-text("Done")',
        'button:has-text("Apply")',
    ]
    await _click_first_visible(page, confirm_btns, "store-confirm", debug)
    await page.wait_for_timeout(2500)
    print(f"[store] zip {zip_code} set")
    return True


async def _pick_first_store(page: Page, debug: bool) -> bool:
    """Select the first store in the modal. Tries several strategies so we
    don't get stuck on hidden inputs or React-managed radios."""
    # Strategy 1: Locator.check() on the first visible radio. This is the
    # right API for radios and dispatches the proper events even when the
    # actual <input> is visually hidden.
    radio_selectors = [
        'input[type="radio"][name*="store" i]:visible',
        'input[type="radio"]:visible',
        '[role="radio"]:visible',
    ]
    for sel in radio_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if debug:
                print(f"  [store-pick] check() {sel!r}")
            await loc.check(timeout=3000, force=True)
            return True
        except Exception as e:
            if debug:
                print(f"  [store-pick] check() failed: {type(e).__name__}: {e}")

    # Strategy 2: click the parent <label> (covers cases where label wraps the
    # radio and React's onChange is bound to the label).
    label_selectors = [
        'label:has(input[type="radio"])',
        '[role="dialog"] label',
    ]
    for sel in label_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if debug:
                print(f"  [store-pick] click label {sel!r}")
            await loc.click(timeout=3000, force=True)
            return True
        except Exception as e:
            if debug:
                print(f"  [store-pick] label click failed: {type(e).__name__}: {e}")

    # Strategy 3: click the first store *card* using the "Miles away" anchor.
    # We climb up to a reasonable card-sized ancestor and click that.
    try:
        if debug:
            print("  [store-pick] clicking first card via 'Miles away' anchor")
        clicked = await page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('*'));
              const target = nodes.find(n =>
                n.textContent && /Miles away/i.test(n.textContent) &&
                n.children.length < 8
              );
              if (!target) return false;
              // Walk up to a card-like ancestor (one that contains a radio).
              let card = target;
              for (let i = 0; i < 6 && card; i++) {
                if (card.querySelector && card.querySelector('input[type=radio], [role=radio]')) {
                  card.click();
                  const radio = card.querySelector('input[type=radio]');
                  if (radio && !radio.checked) {
                    radio.click();
                  }
                  return true;
                }
                card = card.parentElement;
              }
              return false;
            }
            """
        )
        if clicked:
            return True
    except Exception as e:
        if debug:
            print(f"  [store-pick] JS click failed: {type(e).__name__}: {e}")

    # Strategy 4: legacy named-button flow.
    pick_btns = [
        'button:has-text("Make My Store")',
        'button:has-text("Set as my store")',
        'button:has-text("Set as My Store")',
        '[data-testid*="select-store" i]',
    ]
    return await _click_first_visible(page, pick_btns, "store-btn", debug)


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


async def search_top_results(page: Page, query: str, top_n: int,
                              debug: bool = False,
                              dump_payloads: bool = False) -> list[dict]:
    """Navigate to search results and return up to `top_n` product dicts.

    Each dict has: name, price, size, unit_price, unit_price_basis, url, status.
    Always returns at least one dict (with status='no-result' or 'timeout' if
    nothing was matched) so the driver can emit a row for the query.
    """
    cap = SearchCapture()
    cap.attach(page)
    try:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        if debug:
            print(f"  [search] {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            return [{"name": "", "price": "", "size": "",
                     "unit_price": "", "unit_price_basis": "",
                     "url": url, "status": "timeout"}]

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)

        if debug:
            print(f"  [capture] {len(cap.payloads)} matching JSON responses")
            for ep in cap.payloads[:5]:
                print(f"    - {ep['url']}")

        if dump_payloads and cap.payloads:
            dump_path = Path(f"payload-{re.sub(r'[^a-z0-9]+', '-', query.lower())}.json")
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(cap.payloads, f, indent=2, default=str)
            print(f"  [capture] dumped to {dump_path}")

        # 1) Try DOM first — for Meijer, DOM order matches what the user sees,
        #    and the API path has only ever returned ad-network noise.
        dom_results = await _dom_top_results(page, top_n, debug)
        if dom_results:
            return [{**r, "status": "ok-dom"} for r in dom_results]

        # 2) Fall back to captured API data with relevance scoring.
        products = cap.extract_products()
        best = _best_products(products, query, top_n)
        if best:
            out: list[dict] = []
            for p in best:
                fields = _product_to_fields(p)
                if fields["name"]:
                    fill_unit_price(fields, json.dumps(p, default=str))
                    out.append({**fields, "status": "ok"})
            if out:
                return out

        return [{"name": "", "price": "", "size": "",
                 "unit_price": "", "unit_price_basis": "",
                 "url": url, "status": "no-result"}]
    finally:
        cap.detach()


async def _dom_top_results(page: Page, top_n: int, debug: bool) -> list[dict]:
    """Extract the first `top_n` visible product cards from the search page."""
    card_selectors = [
        '[data-testid="product-tile"]',
        '[class*="product-tile"]',
        '[class*="ProductTile"]',
        '[class*="productCard"]',
        'article[class*="product" i]',
    ]
    cards_locator = None
    for sel in card_selectors:
        try:
            loc = page.locator(sel)
            if await loc.first.is_visible(timeout=2000):
                cards_locator = loc
                if debug:
                    print(f"  [dom] using card selector {sel!r}")
                break
        except Exception:
            continue
    if cards_locator is None:
        return []

    try:
        count = await cards_locator.count()
    except Exception:
        count = 0
    if count == 0:
        return []

    out: list[dict] = []
    for i in range(min(count, top_n)):
        card = cards_locator.nth(i)
        try:
            text = await card.inner_text(timeout=2000)
        except Exception:
            text = ""

        # Name
        name = ""
        for sel in ['[class*="name" i]', '[class*="title" i]',
                    'a[href*="/shop/"]', 'h2', 'h3']:
            try:
                t = await card.locator(sel).first.inner_text(timeout=1000)
                if t and t.strip():
                    name = t.strip().splitlines()[0]
                    break
            except Exception:
                continue
        if not name and text:
            name = text.strip().splitlines()[0]

        # Price (first $X.XX in the tile)
        price = ""
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
            href = await card.locator("a[href*='/shop/']").first.get_attribute(
                "href", timeout=1000
            )
            if href:
                url = href if href.startswith("http") else BASE_URL + href
        except Exception:
            pass

        fields = {"name": name, "price": price, "size": size, "url": url}
        fill_unit_price(fields, text)
        out.append(fields)

    return out


# -- Driver ------------------------------------------------------------------


def _parse_zips(args: argparse.Namespace) -> list[str]:
    if args.zips:
        zs = [z.strip() for z in args.zips.split(",") if z.strip()]
        return zs
    if args.zip:
        return [args.zip.strip()]
    return []


def _append_history(path: Path, results: list[Result],
                    fieldnames: list[str]) -> None:
    """Append rows to a rolling history CSV. If the existing file's header
    doesn't match the current schema, back it up to <name>.bak and rewrite
    with the new schema (filling missing columns with empty values)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in results:
                w.writerow(asdict(r))
        return

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        existing_header = next(reader, [])

    if existing_header == fieldnames:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            for r in results:
                w.writerow(asdict(r))
        return

    # Schema migration: read all old rows, back up the file, rewrite under
    # the new schema with missing fields filled in.
    with open(path, "r", newline="", encoding="utf-8") as f:
        old_rows = list(csv.DictReader(f))
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.exists():
        backup.unlink()
    path.rename(backup)
    print(f"  [history] schema changed — old file backed up to {backup}")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in old_rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})
        for r in results:
            w.writerow(asdict(r))


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

    zips = _parse_zips(args)
    if not zips:
        print("must provide --zip or --zips", file=sys.stderr)
        return 2

    today = datetime.now().strftime("%Y-%m-%d")
    if args.snapshot:
        if args.output == "results.csv":  # default — auto-stamp it
            out_path = Path("snapshots") / f"results-{today}.csv"
        else:
            out_path = Path(args.output)
    else:
        out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        launch_kwargs = {
            "headless": not args.headed,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if args.channel:
            launch_kwargs["channel"] = args.channel
        browser = await pw.chromium.launch(**launch_kwargs)

        results: list[Result] = []
        for zi, zip_code in enumerate(zips, start=1):
            print(f"\n=== ZIP {zip_code}  ({zi}/{len(zips)}) ===")
            # Fresh context per zip so the previous store cookie/localStorage
            # doesn't leak into the next zip's session.
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

            ok = await set_store_by_zip(page, zip_code, debug=args.debug)
            if not ok:
                msg = f"[skip] zip {zip_code} — store selection failed"
                print(msg, file=sys.stderr)
                if args.strict_store:
                    await context.close()
                    await browser.close()
                    return 3
                await context.close()
                continue

            for trade, q in queries:
                print(f"[search] {trade!r} -> {q!r}")
                try:
                    rs = await search_top_results(
                        page, q, top_n=args.top_n, debug=args.debug,
                        dump_payloads=args.dump_payloads,
                    )
                except Exception as e:
                    rs = [{
                        "name": "", "price": "", "size": "",
                        "unit_price": "", "unit_price_basis": "",
                        "url": SEARCH_URL_TMPL.format(query=q.replace(" ", "+")),
                        "status": f"error:{type(e).__name__}",
                    }]
                    print(f"  [error] {type(e).__name__}: {e}")
                    try:
                        if page.is_closed():
                            page = await context.new_page()
                    except Exception:
                        pass
                ts = (datetime.now(timezone.utc).isoformat(timespec="seconds")
                      .replace("+00:00", "Z"))
                for rank, r in enumerate(rs, start=1):
                    results.append(Result(
                        zip_code=zip_code,
                        trade_name=trade,
                        search_query=q,
                        rank=rank,
                        matched_name=r.get("name", ""),
                        price=r.get("price", ""),
                        size=r.get("size", ""),
                        unit_price=r.get("unit_price", ""),
                        unit_price_basis=r.get("unit_price_basis", ""),
                        url=r.get("url", ""),
                        status=r.get("status", ""),
                        date=today,
                        timestamp=ts,
                    ))
                    up = r.get("unit_price", "")
                    up_b = r.get("unit_price_basis", "")
                    up_s = f" ({up}{up_b})" if up else ""
                    print(f"  #{rank} -> {r.get('status','')}: "
                          f"{r.get('name','')!r} {r.get('price','')} "
                          f"{r.get('size','')}{up_s}")
                try:
                    await page.wait_for_timeout(int(args.delay * 1000))
                except Exception:
                    pass

            await context.close()

        await browser.close()

    if not results:
        print("no results to write", file=sys.stderr)
        return 1

    fieldnames = list(asdict(results[0]).keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    ok_count = sum(1 for r in results if r.status.startswith("ok"))
    print(f"\nwrote {out_path} ({ok_count}/{len(results)} matched)")

    if args.snapshot:
        hist_path = Path(args.history_file)
        _append_history(hist_path, results, fieldnames)
        print(f"appended {len(results)} rows to {hist_path}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Scrape Meijer.com produce prices by zip code.")
    p.add_argument("--zip", help="Single ZIP code for store selection")
    p.add_argument("--zips",
                   help="Comma-separated list of ZIPs to scrape sequentially "
                        "(e.g. '45238,43228,48228'). Overrides --zip.")
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
    p.add_argument("--channel", default="chrome",
                   help="Browser channel to launch: 'chrome', 'msedge', or '' "
                        "(empty = use Playwright's bundled Chromium). Default: chrome")
    p.add_argument("--dump-payloads", action="store_true",
                   help="For each query, dump captured search-API JSON to "
                        "payload-<query>.json for inspection")
    p.add_argument("--top-n", type=int, default=1,
                   help="Number of top matches to record per query (default: 1)")
    p.add_argument("--snapshot", action="store_true",
                   help="Snapshot mode: write date-stamped CSV under snapshots/ "
                        "and append all rows to a rolling history file.")
    p.add_argument("--history-file", default="history.csv",
                   help="Rolling history CSV path (used with --snapshot). "
                        "Default: history.csv")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
