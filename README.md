# meijer-scraper

A small Python utility that pulls produce prices from meijer.com for a given
ZIP code. Built on Playwright (real Chromium browser) because meijer.com is a
JS SPA with bot protection.

## What it does

1. Opens meijer.com and sets your store via the ZIP-code selector (one cookie,
   reused for the whole session).
2. Runs each search query from `queries.csv` (or a single `--query`).
3. For each query, captures the top product result via:
   - Network XHR JSON capture (preferred — structured data), then
   - DOM scraping fallback (for when the API shape changes).
4. Writes a CSV with `trade_name, search_query, matched_name, price, size,
   url, status, timestamp`.

## Setup

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# bash / git-bash:
source .venv/Scripts/activate

pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

Run the full produce list:

```bash
python scraper.py --zip 49503 --input queries.csv --output results.csv
```

One-off query:

```bash
python scraper.py --zip 49503 --query "strawberries"
```

Watch the browser (useful for debugging selectors):

```bash
python scraper.py --zip 49503 --headed --debug
```

### Flags

| flag             | default       | description                                          |
| ---------------- | ------------- | ---------------------------------------------------- |
| `--zip`          | (required)    | ZIP code used for store selection                    |
| `--input`        | `queries.csv` | CSV with columns `trade_name,search_query`           |
| `--output`       | `results.csv` | Output CSV path                                      |
| `--query`        | —             | Run a single ad-hoc query instead of reading `--input` |
| `--headed`       | off           | Show the browser window                              |
| `--debug`        | off           | Verbose selector / capture logging                   |
| `--strict-store` | off           | Abort if the store selector flow fails               |
| `--delay`        | `1.5`         | Seconds between searches (be polite)                 |

## queries.csv format

Two columns. `trade_name` is whatever label you want to keep in the output
(e.g. your buyer-side SKU name). `search_query` is what gets typed into
meijer.com's search box.

```csv
trade_name,search_query
ONIONS RED JUMBO CTN 40#,red onion
TOMATOES ROMA LUG,roma tomato
...
```

A starter `queries.csv` covering ~27 produce items is included.

## Output

```csv
trade_name,search_query,matched_name,price,size,url,status,timestamp
ONIONS RED JUMBO CTN 40#,red onion,Red Onion,$1.29,1 lb,https://www.meijer.com/shop/...,ok,2026-05-08T14:33:21Z
```

`status` values:

- `ok` — matched via captured API response
- `ok-dom` — matched via DOM fallback
- `no-result` — search returned nothing usable
- `timeout` — page never loaded
- `error:<ExceptionName>` — unhandled error during scrape

## Caveats / things to know

- **Terms of Service.** Meijer's ToS restricts automated access. Use
  responsibly: low volume, personal use, and don't redistribute the data.
- **Selectors drift.** Meijer ships UI changes regularly. The script tries
  multiple selectors and falls back to network capture, but if both break,
  run with `--headed --debug` and update the selector lists in `scraper.py`.
- **Top-result heuristic.** "Top match" is whatever Meijer's search ranks
  first for the consumer query. If you need stricter matching (e.g. specific
  pack sizes), add a filter step after `_product_to_fields`.
- **Rate limiting.** Default 1.5s between queries is gentle. If you start
  seeing CAPTCHAs, raise `--delay` or run smaller batches.
- **Headless detection.** Some flows behave differently headless. If you get
  empty results, try `--headed`.

## Extending

- Pull additional fields (unit price, sale flags) by extending
  `_product_to_fields`.
- Match multiple results per query: change `search_top_result` to return
  the top N from `cap.extract_products()`.
- Schedule daily runs: wrap in a cron / Task Scheduler job and append to a
  date-stamped CSV.
