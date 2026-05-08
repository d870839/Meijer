"""Sprouts Farmers Market retailer adapter.

shop.sprouts.com runs on Instacart's storefront platform but exposes a true
"In-Store" mode (in-store retail prices, not Instacart-marked-up). The
location pill in the upper-right toggles between Delivery / Pickup /
In-Store; this adapter forces In-Store, then uses the "Near {zip}" address
dialog to set the store.

Flow (reverse-engineered from the live DOM):
  1. Click the location pill (upper-right) -> fulfillment dropdown opens.
  2. Click the In-Store option in the dropdown.
  3. Click the "Near {zip}" button -> address dialog opens.
  4. Type zip into #streetAddress.
  5. Click the first address suggestion (city, state).
  6. Click "Set as my store".
"""

from __future__ import annotations

from playwright.async_api import Page, TimeoutError as PWTimeout

from . import _common as C


NAME = "sprouts"
BASE_URL = "https://shop.sprouts.com"
SEARCH_URL_TMPL = "https://shop.sprouts.com/search?search_term={query}"
SHOP_LINK_SUBSTRING = "/products/"

CARD_SELECTORS = [
    '[data-testid*="item-card" i]',
    '[data-testid*="product-card" i]',
    '[data-testid="product-tile"]',
    '[class*="product-card" i]',
    '[class*="ProductCard"]',
    '[class*="product-tile" i]',
    'article[class*="product" i]',
    'li[class*="product" i]',
]

NAME_SELECTORS = [
    '[data-testid*="item-name" i]',
    '[data-testid*="product-name" i]',
    '[class*="product-name" i]',
    '[class*="ProductName"]',
    '[class*="title" i]',
    'a[href*="/products/"]',
    'h2',
    'h3',
]

SEARCH_URL_INCLUDE = ("search", "products", "graphql", "items")
SEARCH_URL_EXCLUDE = (
    "recommend", "personaliz", "sponsored", "bazaarvoice",
    "promo", "banner", "trending", "bestseller", "featured",
    "similar", "related", "suggest", "carousel",
    "analytics", "telemetry",
    "doubleclick", "googletagmanager", "google-analytics",
    "facebook.com", "scorecardresearch", "criteo",
    "segment.io", "mparticle", "newrelic", "datadog", "sentry.io",
)


INSTORE_URL = "https://shop.sprouts.com/store/sprouts/instore"


async def set_store_by_zip(page: Page, zip_code: str, debug: bool = False) -> bool:
    # Land directly on the In-Store URL — bypasses the fulfillment-dropdown
    # step and forces in-store retail pricing for the rest of the session.
    print(f"[store] navigating to Sprouts In-Store for zip {zip_code}")
    await page.goto(INSTORE_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(4000)
    if debug:
        print(f"  [nav] landed on {page.url}")

    # Dismiss any cookie / age / welcome banner that might cover the button.
    for sel in [
        'button:has-text("Accept All")',
        'button:has-text("Accept")',
        'button:has-text("I Agree")',
        'button:has-text("Got it")',
        'button:has-text("Continue")',
        'button:has-text("Yes, I am")',
        'button:has-text("No thanks")',
        'button:has-text("Maybe later")',
        'button:has-text("Skip")',
        'button:has-text("Close")',
        'button[aria-label*="close" i]',
        'button[aria-label*="dismiss" i]',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click(force=True, timeout=2000)
                if debug:
                    print(f"  [banner] dismissed via {sel!r}")
                await page.wait_for_timeout(400)
        except Exception:
            continue

    # 1) Click the "Change store" button (or "Near {zip}" if a store is
    #    already set). Both have aria-haspopup="dialog" and open the same
    #    address dialog.
    try:
        await page.wait_for_selector(
            'button[aria-haspopup="dialog"]',
            timeout=10000, state="visible",
        )
    except PWTimeout:
        print("[store] couldn't find Change-store / Near button")
        if debug:
            print(f"  [nav] page url: {page.url}")
            print(f"  [nav] title: {await page.title()}")
        return False
    trigger = page.locator('button[aria-haspopup="dialog"]').first
    try:
        if debug:
            txt = (await trigger.inner_text(timeout=1000)).replace("\n", " ")
            print(f"  [store-trigger] clicking aria-haspopup='dialog' "
                  f"button ({txt[:40]!r})")
        await trigger.click(force=True, timeout=4000)
    except Exception as e:
        print(f"[store] couldn't click Change-store button: "
              f"{type(e).__name__}: {e}")
        return False

    # 4) Wait for the address dialog and fill #streetAddress.
    try:
        await page.wait_for_selector('#streetAddress', timeout=8000,
                                      state="visible")
    except PWTimeout:
        print("[store] address dialog didn't appear")
        return False
    try:
        if debug:
            print("  [zip-input] filling #streetAddress")
        await page.locator('#streetAddress').fill(zip_code, timeout=3000)
    except Exception as e:
        print(f"[store] couldn't fill address input: "
              f"{type(e).__name__}: {e}")
        return False

    # 5) Wait for suggestion list, click the first suggestion.
    await page.wait_for_timeout(1200)
    try:
        await page.wait_for_selector(
            '#address-suggestion-list li',
            timeout=5000, state="visible",
        )
    except PWTimeout:
        if debug:
            print("  [suggest] no suggestion list — pressing Enter as fallback")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2500)
    else:
        suggestion = page.locator(
            '#address-suggestion-list li').first
        try:
            if debug:
                print("  [suggest] clicking first suggestion")
            await suggestion.click(force=True, timeout=4000)
        except Exception as e:
            print(f"  [suggest] click failed ({type(e).__name__}); "
                  f"trying Enter")
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(2500)

    # 6) Click "Set as my store" on the resulting store list.
    try:
        await page.wait_for_selector(
            'button:has-text("Set as my store")',
            timeout=6000, state="visible",
        )
    except PWTimeout:
        print("[store] 'Set as my store' button didn't appear")
        return False
    set_btn = page.locator('button:has-text("Set as my store")').first
    try:
        if debug:
            print("  [set-store] clicking 'Set as my store'")
        await set_btn.click(force=True, timeout=4000)
    except Exception as e:
        print(f"[store] couldn't click 'Set as my store': "
              f"{type(e).__name__}: {e}")
        return False
    await page.wait_for_timeout(3000)
    print(f"[store] zip {zip_code} set")
    return True
