"""
Multi-retailer grocery price scraper.

Sets a store by zip code, runs a list of search queries, and writes the top
matches (name, price, size, unit price, url) for each query to a CSV.

Usage:
    python scraper.py --retailer meijer --zip 43228
    python scraper.py --retailer sprouts --zip 37013 --headed --debug
    python scraper.py --retailer foodlion --zip 23223 --top-n 3
    python scraper.py --retailer meijer --zips 45238,43228 --top-n 3 --snapshot

Each retailer has its own adapter under retailers/<name>.py. They share
helpers (top-N, dedup, unit-price extraction, snapshot/history) so adding a
new retailer is mostly a question of writing the store-selector flow and a
few selectors.
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
from types import ModuleType

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

import retailers
from retailers import _common as C


@dataclass
class Result:
    retailer: str
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


# -- Search ------------------------------------------------------------------


async def search_top_results(page: Page, retailer: ModuleType, query: str,
                              top_n: int, debug: bool = False,
                              dump_payloads: bool = False) -> list[dict]:
    """Run one query against `retailer` and return up to top_n product dicts."""
    cap = C.SearchCapture(retailer.SEARCH_URL_INCLUDE,
                          retailer.SEARCH_URL_EXCLUDE)
    cap.attach(page)
    try:
        url = retailer.SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
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
            slug = re.sub(r'[^a-z0-9]+', '-', query.lower())
            dump_path = Path(f"payload-{retailer.NAME}-{slug}.json")
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(cap.payloads, f, indent=2, default=str)
            print(f"  [capture] dumped to {dump_path}")

        # Prefer DOM (matches what the user sees, in ranking order).
        dom_results = await C.dom_top_results(
            page, top_n,
            card_selectors=retailer.CARD_SELECTORS,
            name_selectors=retailer.NAME_SELECTORS,
            shop_link_substring=retailer.SHOP_LINK_SUBSTRING,
            base_url=retailer.BASE_URL,
            debug=debug,
        )
        if dom_results:
            return [{**r, "status": "ok-dom"} for r in dom_results]

        # Fallback: relevance-scored API capture.
        products = cap.extract_products()
        best = C.best_products(products, query, top_n)
        if best:
            out: list[dict] = []
            for p in best:
                fields = C.product_to_fields(p, retailer.BASE_URL)
                if fields["name"]:
                    C.fill_unit_price(fields, json.dumps(p, default=str))
                    out.append({**fields, "status": "ok"})
            if out:
                return out

        return [{"name": "", "price": "", "size": "",
                 "unit_price": "", "unit_price_basis": "",
                 "url": url, "status": "no-result"}]
    finally:
        cap.detach()


# -- Driver ------------------------------------------------------------------


def _parse_zips(args: argparse.Namespace) -> list[str]:
    if args.zips:
        return [z.strip() for z in args.zips.split(",") if z.strip()]
    if args.zip:
        return [args.zip.strip()]
    return []


def _append_history(path: Path, results: list[Result],
                    fieldnames: list[str]) -> None:
    """Append rows to a rolling history CSV; if the existing file's header
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
        existing_header = next(csv.reader(f), [])

    if existing_header == fieldnames:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            for r in results:
                w.writerow(asdict(r))
        return

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
    retailer = retailers.get(args.retailer)
    print(f"[retailer] {retailer.NAME}")

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
            out_path = Path("snapshots") / f"{retailer.NAME}-results-{today}.csv"
        else:
            out_path = Path(args.output)
    else:
        out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    history_path = (Path(args.history_file)
                    if args.history_file
                    else Path(f"history-{retailer.NAME}.csv"))

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
            print(f"\n=== {retailer.NAME.upper()} | ZIP {zip_code}  "
                  f"({zi}/{len(zips)}) ===")
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

            ok = await retailer.set_store_by_zip(page, zip_code,
                                                  debug=args.debug)
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
                        page, retailer, q, top_n=args.top_n,
                        debug=args.debug, dump_payloads=args.dump_payloads,
                    )
                except Exception as e:
                    rs = [{
                        "name": "", "price": "", "size": "",
                        "unit_price": "", "unit_price_basis": "",
                        "url": retailer.SEARCH_URL_TMPL.format(
                            query=q.replace(" ", "+")),
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
                        retailer=retailer.NAME,
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
        _append_history(history_path, results, fieldnames)
        print(f"appended {len(results)} rows to {history_path}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Multi-retailer grocery price scraper.")
    p.add_argument("--retailer", required=True,
                   choices=retailers.SUPPORTED,
                   help="Which retailer to scrape")
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
                   help="Browser channel: 'chrome', 'msedge', or '' for "
                        "Playwright's bundled Chromium. Default: chrome")
    p.add_argument("--dump-payloads", action="store_true",
                   help="Dump captured search-API JSON per query to "
                        "payload-<retailer>-<query>.json")
    p.add_argument("--top-n", type=int, default=1,
                   help="Number of top matches to record per query (default: 1)")
    p.add_argument("--snapshot", action="store_true",
                   help="Snapshot mode: write date-stamped CSV under snapshots/ "
                        "and append all rows to a rolling history file.")
    p.add_argument("--history-file", default="",
                   help="Rolling history CSV path. Default: history-<retailer>.csv")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
