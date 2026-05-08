"""Food Lion retailer adapter.

Targets shop.foodlion.com (or its successor on the unified foodlion.com
domain post-Jan-2026 'Food Lion To Go' launch). The flow follows the
same store-by-zip pattern used by Meijer/Sprouts.

Selectors are best-guess on first revision — run with --headed --debug and
report what fails so we can tighten them up.
"""

from __future__ import annotations

from playwright.async_api import Page, TimeoutError as PWTimeout

from . import _common as C


NAME = "foodlion"
BASE_URL = "https://shop.foodlion.com"
SEARCH_URL_TMPL = "https://shop.foodlion.com/search?search_term={query}"
SHOP_LINK_SUBSTRING = "/product/"

CARD_SELECTORS = [
    '[data-testid*="product-card" i]',
    '[data-testid="product-tile"]',
    '[class*="product-card" i]',
    '[class*="ProductCard"]',
    '[class*="product-tile" i]',
    '[class*="ProductTile"]',
    'article[class*="product" i]',
    'li[class*="product" i]',
]

NAME_SELECTORS = [
    '[class*="product-name" i]',
    '[class*="ProductName"]',
    '[class*="title" i]',
    'a[href*="/product/"]',
    'h2',
    'h3',
]

SEARCH_URL_INCLUDE = ("search", "products", "graphql")
SEARCH_URL_EXCLUDE = (
    "recommend", "personaliz", "sponsored", "bazaarvoice",
    "promo", "banner", "trending", "bestseller", "featured",
    "similar", "related", "suggest", "carousel",
    "analytics", "telemetry",
    "doubleclick", "googletagmanager", "google-analytics",
    "facebook.com", "scorecardresearch", "criteo",
    "segment.io", "mparticle", "newrelic", "datadog", "sentry.io",
)


async def set_store_by_zip(page: Page, zip_code: str, debug: bool = False) -> bool:
    """Open Food Lion's store-selector flow and pick a store by zip."""
    print(f"[store] navigating to Food Lion to set zip {zip_code}")
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(3000)

    # Dismiss cookie/age banner if present.
    for sel in [
        'button:has-text("Accept")',
        'button:has-text("I Agree")',
        'button:has-text("Got it")',
        'button[aria-label*="close" i]',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.click()
                if debug:
                    print(f"  [banner] dismissed via {sel!r}")
                await page.wait_for_timeout(500)
                break
        except Exception:
            continue

    triggers = [
        'button:has-text("Choose Store")',
        'button:has-text("Choose Your Store")',
        'button:has-text("Select Store")',
        'button:has-text("Find a Store")',
        'button:has-text("Set Your Store")',
        'button:has-text("Change Store")',
        '[data-testid*="store-selector" i]',
        '[data-testid*="location" i]',
        '[aria-label*="store" i][role="button"]',
        'button[aria-label*="store" i]',
    ]
    if not await C.click_first_visible(page, triggers, "store-trigger", debug):
        print("[store] couldn't find store trigger")
        return False
    await page.wait_for_timeout(1500)

    zip_inputs = [
        'input[placeholder*="zip" i]',
        'input[placeholder*="ZIP" i]',
        'input[placeholder*="address" i]',
        'input[name*="zip" i]',
        'input[aria-label*="zip" i]',
        'input[aria-label*="location" i]',
        'input[type="search"]',
        'input[type="text"]:visible',
    ]
    if not await C.fill_first_visible(page, zip_inputs, zip_code,
                                       "zip-input", debug):
        print("[store] couldn't find zip input")
        return False
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(3000)

    if not await _pick_first_store(page, debug):
        print("[store] couldn't pick a store")
        return False
    await page.wait_for_timeout(1500)

    confirm_btns = [
        'button:has-text("Make This My Store")',
        'button:has-text("Set as My Store")',
        'button:has-text("Make My Store")',
        'button:has-text("Choose Store")',
        'button:has-text("Save")',
        'button:has-text("Continue")',
        'button:has-text("Confirm")',
        'button:has-text("Done")',
    ]
    await C.click_first_visible(page, confirm_btns, "store-confirm", debug)
    await page.wait_for_timeout(2500)
    print(f"[store] zip {zip_code} set")
    return True


async def _pick_first_store(page: Page, debug: bool) -> bool:
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

    pick_btns = [
        'button:has-text("Select"):visible',
        'button:has-text("Choose"):visible',
        'button:has-text("Make This My Store")',
        'button:has-text("Set as My Store")',
        '[data-testid*="select-store" i]',
    ]
    if await C.click_first_visible(page, pick_btns, "store-btn", debug,
                                    timeout_ms=3000):
        return True

    label_selectors = [
        'label:has(input[type="radio"])',
        '[role="dialog"] li',
        '[role="dialog"] [class*="store" i]',
    ]
    return await C.click_first_visible(page, label_selectors, "store-card",
                                        debug, timeout_ms=3000)
