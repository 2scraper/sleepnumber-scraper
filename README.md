# sleepnumber-scraper

[![tests](https://github.com/2scraper/sleepnumber-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/sleepnumber-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/sleepnumber-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/sleepnumber-scraper/actions/workflows/canary.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer%20%7C%20Scraper%20API-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/2Captcha%20account-not%20required%20to%20run-brightgreen)](#do-you-need-any-of-the-paid-products)

Scrapes [sleepnumber.com](https://www.sleepnumber.com): smart beds, mattresses,
bases, bedding and furniture — **one row per size variant**, with its own sku,
price, sale price, rating and availability.

Four entry points share one parser: Playwright (recommended), pyppeteer,
Selenium, and the 2Captcha Scraping Browser API over CDP or the Scraper API
with no local browser at all.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --category mattresses --format both --out mattresses
```

```
[+] Saved 52 products -> mattresses.json
[+] Saved 52 products -> mattresses.csv
[+] Wrote run metadata -> mattresses.meta.json (status=complete)
```

---

## Access: read this before anything else

**Sleep Number refuses datacentre addresses, and that is the entire
difficulty of scraping it.** Everything else about the site is unusually
easy.

Measured 2026-09-17 from a datacentre address (netcup, Nuremberg, AS197540):
every URL on the site answers an identical **919-byte CloudFront
"Request blocked" 403**. Not a challenge, not a captcha — a refusal.

| request | result |
|---|---|
| plain `curl` | 403 |
| full browser header set, HTTP/2 | 403 |
| HTTP/1.1, `Sec-Fetch-*`, `Accept-Language` | 403 |
| Googlebot user agent | 403 |
| `HEAD` | 403 |
| headless Chromium (Playwright) | 403 |
| `/robots.txt` and `/sitemap.xml` | 403 |
| 2Captcha **Scraper API**, its own exits | 403 |

It is the **address**, not the request. From a residential exit the same
requests answer 200 — measured the same day on two:

| exit | result |
|---|---|
| GB residential (TalkTalk, AS13285) | **200**, full catalogue |
| US residential (Charter, Ohio, AS10796) | **200**, full catalogue |

So the block is datacentre **reputation**, not geography — a UK address is
served.

**And the exit country does not change the data.** Parsing the same category
captured through each exit: 52 rows both times, the same 52 skus, and **zero
rows differing in price, original price or currency** — everything is USD
from both. So a rotating residential exit that lands outside the US is not a
correctness problem here; it is a US-only storefront quoting one currency to
everyone it serves.

That matters in practice because a 2Captcha gateway without a `-region-`
token in the login rotates: the same credential was measured exiting in GB
(TalkTalk) and, minutes later, in US Pennsylvania (Verizon). Both worked and
both returned identical rows. Pin a region only if you want a *reproducible*
exit for other reasons, not because the data needs it.

There is **no captcha to solve here.** The refusal carries no
`x-amzn-waf-action` header, no challenge iframe and no widget of any kind, so
no solver at any price can help: the request never reaches Sleep Number. What
helps is a different exit.

```bash
# .env, next to the scripts — never on the command line
SLEEPNUMBER_PROXY=http://{login}-zone-resi-region-us:{password}@proxy.2captcha.com:2334
```

---

## Do you need any of the paid products?

**For the catalogue itself: no key, no captcha solving, no fingerprint.**
Sleep Number server-renders its entire catalogue into the page, so an
ordinary HTTP client on a residential connection reads it — a plain
`requests` fetch through a residential exit returned byte-identical data to a
browser and parsed to the same 52 rows.

What you *do* need is an exit the site will serve. If you are on a home
connection, this repo needs nothing at all. If you are on a VPS, a CI runner
or a container — which is where scrapers usually live — you need a
residential proxy, and that is the one thing worth paying for here.

| product | does it help on this site? |
|---|---|
| **Proxies** | **Yes — this is the one that matters.** Without a residential exit nothing works from a datacentre. |
| **Scraping Browser API** | Yes, as an alternative to a proxy: it brings its own residential exit, and it is the way to make the Scraper API work (`--cdp-url`). |
| Captcha solving | **No.** Nothing on this site renders a challenge. The detection is wired in and stays broad, but no solve has ever been attempted here. |
| Fingerprints | Not needed. `--fingerprint` works (verified live, US fingerprint, 52 rows) but nothing measured here required it. |

This is the honest answer and it is not a sales pitch. Saying a captcha
solver is *unnecessary* is a statement about this site; it is never a
statement about what 2Captcha can solve.

---

## What it reads

### A row is a size variant

`QCM10` is the ComfortMode™ Mattress in Queen; `KCM10` is the same model in
King. They are two rows, because they are two prices — ComfortMode spans
**$989.10 to $2,069.10** across seven sizes. Grouping by model would force a
min/max pair and throw away the size dimension, which is the thing a mattress
price monitor most wants.

`sku` is the variant id, so the family's dedupe-and-diff-on-sku contract
works unchanged, and `diff_runs.py` reports per-size moves: QCM10 going on
sale while KCM10 does not is two facts, and you get two.

### Columns

`source` `scraped_at` `url` `sku` `title` `brand` `price` `currency`
`original_price` `discount_pct` `price_source` `rating` `review_count`
`in_stock` `image_url` `category` `size` `product_id` `product_slug`
`collection_slug` `product_type` `promo_badge` `page` `position`

See `sample_output.json` / `sample_output.csv`, both cut from a real run.

### Measured coverage

Every figure below is from captures taken 2026-09-17 through a US residential
exit, and reproducible with `python3 tools/capture.py`.

| listing | products | rows (variants) | priced | discounted | rated | sizes |
|---|---:|---:|---:|---:|---:|---:|
| `/categories/mattresses` | 7 | 52 | 52 | 52 | 52 | 10 |
| `/categories/sheets-pillowcases` | 10 | 211 | 211 | 13 | 211 | 13 |
| `/categories/furniture` | 20 | 169 | 169 | 157 | 169 | 8 |
| `/collections/mattresses-climate-collection` | 5 | 27 | 27 | 27 | 21 | 7 |
| `/products/cm-mattress` (detail) | 1 | 7 | 7 | 7 | 7 | 7 |

**Price coverage is 100% on every listing measured**, which is why the
engines treat a shortfall as a parsing regression rather than as the
catalogue's own doing (`page_flow.PRICE_COVERAGE_FLOOR`, 0.98).

`discounted` genuinely varies — 52 of 52 on mattresses, 13 of 211 on sheets —
so `original_price` is a real column and not a constant. `rated` varies too
(21 of 27 on the collection page).

### How big is the catalogue?

From the site's own `sitemap.xml.gz`, 2026-09-17: **1,994 URLs, 1,796
unique** —

| path | URLs | unique |
|---|---:|---:|
| `/stores/` | 1,140 | 1,137 |
| `/post/` | 337 | 337 |
| `/pages/` | 245 | 129 |
| `/categories/` | 109 | 51 |
| `/products/` | 88 | 88 |
| `/bundles/` | 16 | 16 |

So the sellable catalogue is **88 products across 51 distinct categories** —
small, and a complete pass over it is minutes rather than hours.

---

## Where the data actually is

This is the unusual part, and it is worth knowing before you read the parser.

Sleep Number runs a **React Router (Remix)** front end on **Workarea
Commerce**, served from **Fly.io** behind **CloudFront**. A category page
carries **zero** product anchors and **zero** `"sku"` strings as markup: the
grid is painted client-side.

What every page *does* carry is the router's hydration payload:

```html
<script>window.__reactRouterContext.streamController.enqueue("[{\"_1\":2,…}]")</script>
```

That is a **turbo-stream** document — a single flat array in which every
value is an *index into that same array*, and an object is spelled
`{"_16": 17}` meaning "the key at index 16, the value at index 17". Decoded
(`product_parser.decode_turbo_stream`), it hands back the complete
server-side loader data: every product, every size variant, every price,
before a single script has run.

So the extraction order here inverts this family's usual one, and the counts
are the reason rather than taste:

1. **The hydration payload — primary.** Present and complete on both page
   kinds. A listing exposes it at `category.products[]`, a detail page at
   `.product`, and they are **the same object shape**, so one reader serves
   both and they cannot drift.
2. **JSON-LD — confirmation, not primary.** Real but partial: an `ItemList`
   appears on `/categories/mattresses/king` and on *none* of
   `/categories/mattresses`, `/categories/beds-on-sale` or the collection
   page. Where present it agrees — **7 of 7 rows** on the king capture, and
   the Climate360 detail page's `7686.75` against the payload's `768675`
   cents. That agreement is recorded per row in `price_source`.
3. **The `/collections/{slug}/{SKU}` URL pattern — fallback.** A URL pattern,
   never a CSS class. It emits identifiable rows with **null prices** rather
   than pretending to have parsed.

### Prices are integer cents

```json
{"sale": {"cents": 393750, "currency_iso": "USD"},
 "regular": {"cents": 525000, "currency_iso": "USD"}}
```

No thousands separator to disambiguate, no decimal comma, no dash standing in
for the cents, no currency symbol to match longest-first. **This repo ships no
price-parsing grammar at all**, because there is nothing here for one to do —
and porting the family's would have been dead code that looks load-bearing.
`currency` comes from `currency_iso`, which is a stated fact, never a guess.

---

## Traps that look like bugs

Each of these is measured, and each is pinned by a check in `smoke_test.py`.

**The site's own structured data points at its origin host.** Every URL inside
the JSON-LD reads `http://sndotcom.fly.dev/…` — the Fly application
hostname, leaked through the renderer — and not `https://www.sleepnumber.com/…`.
Read verbatim, every row would link to a dev host that is not the shop. The
parser rewrites it.

**`?page=N` is ignored.** `/categories/sheets-pillowcases?page=2` and
`?page=99` both answer HTTP 200 carrying **page one** — the same ten
products, and the payload's own `page` field still reads `1`. A count-based
pagination loop would never terminate. Pagination is therefore planned from
the site's own arithmetic (`next_page`, `last_page`, `total_results`,
`per_page`) and terminated on **data** — a page that adds no new sku ends the
listing.

**`--concurrency` is refused, with the reason.** It follows from the above:
workers would fetch page one in parallel. It is also moot — `per_page` is 100
and the largest category measured holds 20 products, so **every category is
one page**.

**A size-filtered listing URL is the same request.**
`/categories/mattresses/king` decodes to the same payload as
`/categories/mattresses` — the same 7 products, the same `full_url`. The size
segment selects a facet in the browser. The two are not different samples.

**`regular` equals `sale` on everything that is not discounted** — 198 of 211
variants on the sheets category. Copied across, that gives an
`original_price` identical to the price and a 0% discount on most of the
catalogue. `original_price` is null unless the regular price is genuinely
higher, and `discount_pct` is computed from the two rather than read from the
`promo_badge`.

**An unreviewed product reports `0`, not null.** `rating` and `review_count`
go null *together* rather than dragging a consumer's average toward zero.

**`/products/{slug}` is not the model's `slug`.** The ComfortMode mattress has
`slug: "comfortmode"` and a canonical URL of `/products/cm-mattress`; only
the latter answers 200. `/products/i8` looks entirely plausible, appears in
search results, and returns the site's 404.

**The site's 404 is a full, 200-KB page** with site chrome and a JSON-LD
`@graph` block. It is told apart by its own wording plus the absence of any
product in the payload — never by size.

**reCAPTCHA is configured on this site and never rendered.** The base
template ships a reCAPTCHA loader, so `recaptcha` and `captcha` each occur
**once on every page**, good and refused alike. Either one used as a block
marker would report the entire catalogue as challenged. That a captcha is
*configured* is a different statement from a challenge being *rendered*.

---

## Block detection

Built from counts on pages known to be good, not from a vendor list. Over
four served pages, two of the site's own 404s and two CloudFront refusals
(one as an HTTP client receives it, one read out of a browser DOM):

| candidate marker | served ×4 | 404 ×2 | refused ×2 | kept? |
|---|---|---|---|---|
| `Request blocked` | 0 0 0 0 | 0 0 | 1 1 | **yes** |
| `Generated by cloudfront` | 0 0 0 0 | 0 0 | 1 1 | **yes** |
| `recaptcha` | 1 1 1 1 | 1 1 | 0 0 | no — fires on every page |
| `captcha` | 1 1 1 1 | 1 1 | 0 0 | no — same |
| `akamai`, `datadome`, `perimeterx`, `incapsula`, `hcaptcha`, `awswaf`, `cf-turnstile`, `challenges.cloudflare.com`, `errors.edgesuite.net` | 0 0 0 0 | 0 0 | 0 0 | no — dead code |

The **positive** signal is what actually carries detection: a page Sleep
Number served is *built out of its own assets*, and an interstitial is not.
`cdn.sleepnumber.com` appears **214 / 549 / 53 / 221** times on the four
served pages and **0** on both refusals; `__reactRouterContext` is 5 against 0.

Response headers are read first where available, because they are the one
signal a body cannot forge: a served page answers `x-cache: Miss from
cloudfront` with `server: Fly/…`, a refusal `Error from cloudfront` with
`server: CloudFront`.

---

## Engines

| engine | on this site |
|---|---|
| **`playwright_scraper.py`** | Recommended. 52 rows, exit 0, complete. |
| `puppeteer_scraper.py` | Works. Byte-identical rows to Playwright on the same URL. |
| `selenium_scraper.py` | **Cannot authenticate a proxy** — see below. |
| `scraper_api_client.py` | No local browser. Needs `--cdp-url` here. |

### Selenium's limitation matters more here than elsewhere

`chromedriver`'s `--proxy-server` takes a bare `host:port` with nowhere to put
a password. The engine strips the credentials and warns — it will not let you
believe a `user:pass` URL is doing something. On most sites that is an
inconvenience; **on this one it means Selenium cannot work from a datacentre
at all**, because the proxy is the whole point. It reports exit 3 with the
reason rather than a misleading "0 products".

Selenium is usable here from a residential connection, or through a proxy
that authenticates by source IP instead of by password.

Selenium also cannot use an authenticated remote CDP endpoint: Playwright's
`connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
`ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
`debuggerAddress` takes a bare `host:port`.

### The Scraper API needs a Scraping Browser exit

Measured 2026-09-17: the Scraper API's own exits are datacentre addresses, and
this site answers them with the same 919-byte CloudFront 403 it gives any
other. Route the fetch through a Scraping Browser session:

```bash
python3 scraper_api_client.py \
  --url https://www.sleepnumber.com/categories/mattresses \
  --cdp-url "$SLEEPNUMBER_CDP_ENDPOINT"
```

A refusal is **not retried** — a CloudFront deny is a property of the exit
address and does not change between attempts, so retrying buys a second copy
of a certainty and is billed for it. A challenge page still retries, because
that one can clear.

### Install exactly one engine

`playwright` and `pyppeteer` pin mutually unsatisfiable `pyee` versions, and
`pyppeteer` and `selenium` collide on `urllib3`. They do run side by side in
practice, but `pip check` reports the conflict and pip may resolve it by
downgrading something you wanted. Use a virtualenv per engine.

---

## Modes

```bash
# a category or collection listing (the default)
python3 playwright_scraper.py --category mattresses
python3 playwright_scraper.py --category collections/mattresses-climate-collection

# one product's detail page — same rows, one model's worth
python3 playwright_scraper.py --mode product --category cm-mattress
```

## Output contract

- **A run that finds nothing writes nothing.** Last night's good output is
  never replaced with `[]`. `--allow-empty` is the opt-out.
- **Exit codes:** `0` ok · `1` crash · `2` bad usage · `3` blocked ·
  `4` zero products · `5` remote API error · `6` partial.
- **`<out>.meta.json`** per run, recording `status`, `stop_reason`, which
  pages failed by number, and the site's own arithmetic about the result set
  — `total_results`, `per_page`, `pages_available`, `page_param_honoured` —
  because "complete" and "exhaustive" are different words.
- **An empty CSV still carries its header.**

## Diffing two runs

```bash
python3 playwright_scraper.py --category mattresses --out "mattresses_$(date +%F)"
python3 diff_runs.py --old mattresses_2026-09-16.json \
                     --new mattresses_2026-09-17.json
```

Keyed on `sku`, so per size variant. A price difference that comes with a
`price_source` difference is reported as `source_changed`, not `changed`, and
`--fail-on-change` ignores it: it says something about our two snapshots, not
about Sleep Number.

## Configuration

Credentials live in `.env` next to the scripts, never on a command line — a
secret in `argv` is readable by anything that can run `ps`.

```bash
cp .env.example .env
$EDITOR .env
python3 env_config.py     # prints what was picked up, without secrets
```

Precedence, highest first: explicit flag → exported environment variable →
`.env` → default.

## Tests

```bash
python3 smoke_test.py -v      # 59 checks, no network, no engine needed
pytest                        # the same checks, wrapped
```

The suite passes with **no engine library installed at all**, and CI's
`engine-smoke` job installs each engine in its own virtualenv and fails if the
matching group reports a skip — "skipped, engine absent" reads identically to
a broken import.

Fixtures come from real captures. Because a capture is 300-900 KB and its
catalogue lives in a turbo-stream document where every value is an index into
the same array, products cannot simply be deleted from it: every index after
the cut shifts. `tools/make_fixtures.py` therefore decodes a real capture,
takes real product objects out verbatim, re-encodes them into the same wire
format, and **asserts that what it produced decodes back to objects equal to
what went in** — so "smaller" cannot quietly become "different".

## Re-measuring anything in this README

```bash
python3 tools/capture.py                     # fetch and report what a parser could read
python3 tools/capture.py --direct            # measure THIS machine's exit
python3 tools/make_fixtures.py captures/     # rebuild the pinned fixtures
```

Every number here is dated and reproducible with one of those. A figure that
describes a living thing rots; the command beside it does not.

## Licence

MIT. Not affiliated with, endorsed by, or connected to Sleep Number
Corporation. Respect `robots.txt`, the site's terms and a sane request rate;
this tool defaults to one page at a time with a delay and refuses
`--concurrency` outright.
