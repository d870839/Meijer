"""Shared helpers used by every retailer module."""

from __future__ import annotations

import re
from typing import Optional

from playwright.async_api import Page, Response


# -- Regex used across retailers --------------------------------------------

PRICE_RE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?\s?(?:lb|oz|ct|count|each|ea|pk|pack|pint|qt|gal|fl\s?oz|kg|g))",
    re.IGNORECASE,
)
UNIT_PRICE_RE = re.compile(
    r"\$\s?(\d+(?:\.\d{1,2})?)\s*/\s*(lb|oz|ea|each|count|ct|kg|g|pint|qt|gal|fl\s?oz|ml|l)\b",
    re.IGNORECASE,
)
UNIT_PRICE_PAREN_RE = re.compile(
    r"\(\s*\$?\s?(\d+(?:\.\d{1,2})?)\s*/\s*(lb|oz|ea|each|count|ct|kg|g)\s*\)",
    re.IGNORECASE,
)

_TO_LB: dict[str, float] = {
    "lb": 1.0,
    "oz": 1.0 / 16.0,
    "kg": 2.2046226,
    "g": 1.0 / 453.59237,
}
_COUNT_UNITS = {"ea", "each", "ct", "count", "pk", "pack"}


# -- Generic Playwright interaction helpers ---------------------------------


async def click_first_visible(page: Page, selectors: list[str], label: str,
                              debug: bool, timeout_ms: int = 1500,
                              force: bool = False,
                              click_timeout_ms: int = 4000) -> bool:
    """Try selectors in order; click the first that's visible. With
    force=True, skip Playwright's actionability check (useful for elements
    inside modals/portals where the surrounding page can be still animating
    or scrolling)."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout_ms):
                if debug:
                    print(f"  [{label}] clicking {sel!r}")
                await loc.click(force=force, timeout=click_timeout_ms)
                return True
        except Exception as e:
            if debug:
                print(f"  [{label}] {sel!r} failed: {type(e).__name__}")
            continue
    return False


async def fill_first_visible(page: Page, selectors: list[str], value: str,
                             label: str, debug: bool,
                             timeout_ms: int = 1500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout_ms):
                if debug:
                    print(f"  [{label}] filling {sel!r}")
                await loc.fill(value)
                return True
        except Exception:
            continue
    return False


# -- Network capture --------------------------------------------------------


class SearchCapture:
    """Capture JSON responses from a retailer's search endpoints during a
    page load. One instance per query; attach/detach explicitly so listeners
    don't leak across queries on the same page."""

    def __init__(self, include: tuple[str, ...], exclude: tuple[str, ...]) -> None:
        self.include = include
        self.exclude = exclude
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
        if self.include and not any(k in url for k in self.include):
            return
        if any(k in url for k in self.exclude):
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
        out: list[dict] = []
        for entry in self.payloads:
            out.extend(_walk_for_products(entry["data"]))
        return out


def _walk_for_products(node, depth: int = 0) -> list[dict]:
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


def query_words(q: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z]{3,}", q)}


def name_score(name: str, qwords: set[str]) -> int:
    if not name or not qwords:
        return 0
    nwords = {w.lower() for w in re.findall(r"[a-zA-Z]{3,}", name)}
    return len(qwords & nwords)


def best_products(products: list[dict], query: str, n: int) -> list[dict]:
    if not products or n <= 0:
        return []
    qwords = query_words(query)
    scored: list[tuple[int, int, dict]] = []
    for i, p in enumerate(products):
        nm = p.get("name") or p.get("title") or p.get("displayName") or ""
        score = name_score(str(nm), qwords)
        if score == 0:
            continue
        scored.append((score, -i, p))
    scored.sort(reverse=True)
    return [p for _, _, p in scored[:n]]


def product_to_fields(p: dict, base_url: str) -> dict:
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
        url = base_url.rstrip("/") + url
    return {"name": str(name).strip(), "price": price,
            "size": str(size).strip(), "url": url}


# -- Unit price -------------------------------------------------------------


def _normalize_unit(u: str) -> str:
    u = u.lower().replace(" ", "")
    if u == "each":
        return "ea"
    if u == "count":
        return "ct"
    return u


def unit_price_from_text(text: str) -> tuple[str, str]:
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
    up, basis = unit_price_from_text(source_text)
    if not up:
        up, basis = unit_price_from_text(fields.get("name", ""))
    if not up:
        up, basis = derive_unit_price(fields.get("price", ""), fields.get("size", ""))
    fields["unit_price"] = up
    fields["unit_price_basis"] = basis
    return fields


# -- DOM scraping (generic across retailers) --------------------------------


async def dom_top_results(page: Page, top_n: int, card_selectors: list[str],
                          name_selectors: list[str], shop_link_substring: str,
                          base_url: str, debug: bool) -> list[dict]:
    """Iterate the first matching product cards on a search-results page,
    skip empty/duplicate ones, and return up to `top_n` distinct results."""
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

    max_scan = min(count, max(top_n * 6, top_n + 10))
    out: list[dict] = []
    seen: set[str] = set()

    for i in range(max_scan):
        if len(out) >= top_n:
            break
        card = cards_locator.nth(i)
        try:
            text = await card.inner_text(timeout=2000)
        except Exception:
            text = ""

        name = ""
        for sel in name_selectors:
            try:
                t = await card.locator(sel).first.inner_text(timeout=1000)
                if t and t.strip():
                    name = t.strip().splitlines()[0]
                    break
            except Exception:
                continue
        if not name and text:
            name = text.strip().splitlines()[0]

        m = PRICE_RE.search(text)
        price = f"${m.group(1)}" if m else ""

        sm = SIZE_RE.search(text)
        size = sm.group(1) if sm else ""

        url = ""
        try:
            href = await card.locator(
                f"a[href*='{shop_link_substring}']"
            ).first.get_attribute("href", timeout=1000)
            if href:
                url = href if href.startswith("http") else base_url.rstrip("/") + href
        except Exception:
            pass

        if not name and not price:
            if debug:
                print(f"  [dom] skipping empty card #{i}")
            continue
        key = url or f"{name}|{price}"
        if key in seen:
            if debug:
                print(f"  [dom] skipping duplicate card #{i} ({name!r})")
            continue
        seen.add(key)

        fields = {"name": name, "price": price, "size": size, "url": url}
        fill_unit_price(fields, text)
        out.append(fields)

    return out
