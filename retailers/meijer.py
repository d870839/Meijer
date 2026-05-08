"""Meijer.com retailer adapter."""

from __future__ import annotations

from playwright.async_api import Page, TimeoutError as PWTimeout

from . import _common as C


NAME = "meijer"
BASE_URL = "https://www.meijer.com"
SEARCH_URL_TMPL = "https://www.meijer.com/shopping/search.html?text={query}"
SHOP_LINK_SUBSTRING = "/shop/"

CARD_SELECTORS = [
    '[data-testid="product-tile"]',
    '[class*="product-tile"]',
    '[class*="ProductTile"]',
    '[class*="productCard"]',
    'article[class*="product" i]',
]

NAME_SELECTORS = [
    '[class*="name" i]',
    '[class*="title" i]',
    'a[href*="/shop/"]',
    'h2',
    'h3',
]

SEARCH_URL_INCLUDE = ("search",)
SEARCH_URL_EXCLUDE = (
    "recommend", "personaliz", "sponsored", "bazaarvoice",
    "promo", "banner", "trending", "bestseller", "featured",
    "similar", "related", "suggest", "carousel", "merchandised",
    "analytics", "telemetry",
    "emarsys.net", "teads.tv", "doubleclick", "googletagmanager",
    "google-analytics", "facebook.com", "scorecardresearch",
    "adservice", "adsystem", "rubiconproject", "criteo", "pubmatic",
    "bluekai", "demdex", "krxd", "segment.io", "mparticle",
    "newrelic", "datadog", "sentry.io", "fullstory",
)


async def set_store_by_zip(page: Page, zip_code: str, debug: bool = False) -> bool:
    """Open Meijer's store-selector modal, type the zip, pick the first store."""
    print(f"[store] navigating to homepage to set zip {zip_code}")
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)

    triggers = [
        'button:has-text("Select Store")',
        'button:has-text("Change Store")',
        'a:has-text("Select Store")',
        'a:has-text("Change Store")',
        '[aria-label*="store" i][role="button"]',
        '[data-testid*="store-selector" i]',
        '[data-testid*="store" i] button',
    ]
    if not await C.click_first_visible(page, triggers, "store-trigger", debug):
        print("[store] couldn't find store-selector trigger")
        return False
    await page.wait_for_timeout(1500)

    zip_inputs = [
        'input[placeholder*="zip" i]',
        'input[name*="zip" i]',
        'input[aria-label*="zip" i]',
        'input[type="search"]',
        'input[type="text"]',
    ]
    if not await C.fill_first_visible(page, zip_inputs, zip_code,
                                       "zip-input", debug):
        print("[store] couldn't find zip input")
        return False
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(3000)

    try:
        await page.wait_for_selector('text=/Miles away/i', timeout=8000)
    except PWTimeout:
        if debug:
            print("  [store-pick] no 'Miles away' text — list may not have rendered")

    if not await _pick_first_store(page, debug):
        print("[store] couldn't pick a store")
        return False
    await page.wait_for_timeout(1500)

    confirm_btns = [
        'button:has-text("Save")',
        'button:has-text("Continue")',
        'button:has-text("Confirm")',
        'button:has-text("Done")',
        'button:has-text("Apply")',
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

    pick_btns = [
        'button:has-text("Make My Store")',
        'button:has-text("Set as my store")',
        'button:has-text("Set as My Store")',
        '[data-testid*="select-store" i]',
    ]
    return await C.click_first_visible(page, pick_btns, "store-btn", debug)
