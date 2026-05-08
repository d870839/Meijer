# grocery-price-scraper

Multi-retailer produce price scraper. Sets a store by ZIP code, runs a list
of search queries, and writes the top matches (name, price, size, unit price)
to a CSV. Built on Playwright (real Chromium browser) because the target sites
are JS SPAs with bot protection.

Currently supported retailers:

- **Meijer** (`--retailer meijer`) — meijer.com
- **Sprouts Farmers Market** (`--retailer sprouts`) — shop.sprouts.com (In-Store mode for retail prices)
- **Food Lion** (`--retailer foodlion`) — shop.foodlion.com

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Use the system-installed Chrome via --channel chrome (default) — no
# Playwright browser download needed.
```

If your network blocks Playwright's Chromium download (corp TLS proxy etc.),
the default `--channel chrome` setting drives your installed Chrome instead.

## Usage

```powershell
# Meijer — Cincinnati through Louisville, top-3 matches, daily snapshot
python scraper.py --retailer meijer --zips 45238,43228,48228,46227,40214 --top-n 3 --snapshot

# Sprouts — Antioch, TN
python scraper.py --retailer sprouts --zip 37013 --top-n 3 --snapshot

# Food Lion — Richmond, VA
python scraper.py --retailer foodlion --zip 23223 --top-n 3 --snapshot

# Single ad-hoc lookup (any retailer)
python scraper.py --retailer meijer --zip 43228 --query "strawberries"

# Watch the browser + verbose logging (useful when iterating selectors)
python scraper.py --retailer sprouts --zip 37013 --headed --debug --query "celery"
```

### Flags

| flag              | default       | description                                          |
| ----------------- | ------------- | ---------------------------------------------------- |
| `--retailer`      | (required)    | `meijer`, `sprouts`, or `foodlion`                   |
| `--zip`           | —             | Single ZIP code for store selection                  |
| `--zips`          | —             | Comma-separated list of ZIPs; fresh browser context per zip |
| `--input`         | `queries.csv` | CSV with columns `trade_name,search_query`           |
| `--output`        | `results.csv` | Output CSV path                                      |
| `--query`         | —             | Run a single ad-hoc query instead of reading `--input` |
| `--top-n`         | `1`           | Top N matches per query (one row per match)          |
| `--snapshot`      | off           | Date-stamped CSV in `snapshots/` AND append to `history-<retailer>.csv` |
| `--history-file`  | (auto)        | Rolling history CSV. Default: `history-<retailer>.csv` |
| `--headed`        | off           | Show the browser window                              |
| `--debug`         | off           | Verbose selector / capture logging                   |
| `--strict-store`  | off           | Abort if the store selector flow fails               |
| `--delay`         | `1.5`         | Seconds between searches                             |
| `--channel`       | `chrome`      | Browser channel: `chrome`, `msedge`, or `''` (bundled Chromium) |
| `--dump-payloads` | off           | Dump captured search-API JSON per query              |

## Architecture

```
scraper.py             # CLI driver: parses args, runs the loop, writes CSV
queries.csv            # default search list (shared across retailers)
retailers/
  __init__.py          # registry of supported retailer modules
  _common.py           # shared helpers (DOM scraper, network capture, unit price)
  meijer.py            # Meijer-specific URLs, selectors, store-selector flow
  sprouts.py           # Sprouts In-Store-mode adapter
  foodlion.py          # Food Lion adapter
snapshots/
  meijer-results-2026-05-08.csv
  sprouts-results-2026-05-08.csv
  …
history-meijer.csv     # rolling per-retailer history (price-over-time)
history-sprouts.csv
history-foodlion.csv
```

Adding another retailer is one new module under `retailers/` plus an entry
in `retailers/__init__.SUPPORTED`. Each module exports `BASE_URL`,
`SEARCH_URL_TMPL`, card/name selectors, search-URL include/exclude tuples,
and an `async set_store_by_zip(page, zip_code, debug)` function.

## Output

Columns: `retailer, zip_code, trade_name, search_query, rank, matched_name,
price, size, unit_price, unit_price_basis, url, status, date, timestamp`.

`unit_price_basis` is `/lb` for weight-based produce (oz/kg/g converted to
$/lb), `/ea` for count-based items, or empty when the size on the tile
doesn't permit a clean conversion. The script first looks for an explicit
"$X.XX/lb" string on the tile; if absent it derives from price ÷ size.

`status` values:

- `ok-dom` — matched via DOM scraping (primary path)
- `ok` — matched via captured API JSON (fallback)
- `no-result` — search returned nothing usable
- `timeout` — page never loaded
- `error:<ExceptionName>` — unhandled error during scrape (script keeps going)

## Caveats / things to know

- **ToS.** Each retailer's terms restrict automated access. Use responsibly:
  low volume, personal use, no redistribution of the data.
- **Sprouts In-Store vs. Instacart.** `shop.sprouts.com` defaults to an
  Instacart-powered delivery mode where prices are marked up. The Sprouts
  adapter forces the **In-Store** mode (the location pill in the upper-right)
  so you get true retail prices.
- **Selectors drift.** Sites ship UI changes regularly. Each adapter tries
  several selectors and the script falls back to network capture, but if
  both break, run with `--headed --debug` and tighten the selector list in
  the relevant `retailers/<name>.py`.
- **Rate limiting.** Default 1.5s between queries is gentle. If you start
  seeing CAPTCHAs, raise `--delay` or run smaller batches.
- **Headless detection.** Some flows behave differently headless. If you
  get empty results, try `--headed`.
- **First-revision retailers.** Sprouts and Food Lion adapters were written
  without live testing access. Expect to iterate on selectors based on
  `--headed --debug` output during your first run.

## Extending

- **More fields** (unit-price, sale flags, organic badge): extend
  `_dom_top_results` in `retailers/_common.py` to read additional tile text.
- **Stricter matching** (specific pack sizes): post-filter inside
  `search_top_results` after the DOM/API extraction.
- **Daily schedule**: Windows Task Scheduler running the snapshot command.
  `history-<retailer>.csv` accumulates one row per (date, zip, query, rank).
