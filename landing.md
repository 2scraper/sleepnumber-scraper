# Sleep Number Scraper by 2scraper

**Open-source scraper for sleepnumber.com — four engines, your own infrastructure by default, and 2Captcha's paid products only where they are actually needed.**

Pull Sleep Number's catalogue — smart beds, mattresses, bases, bedding and furniture — into JSON or CSV, **one row per size variant**, with its own sku, price, sale price, discount, rating, review count and availability.

[**View source on GitHub →**](https://github.com/2scraper/sleepnumber-scraper)

---

## What was actually measured

**There is no captcha on this site, and no solver can help with it.**

That is worth saying first, because it is the opposite of what a scraping
page usually claims. Measured on 2026-09-17: from a datacentre address every
URL on sleepnumber.com — `/robots.txt` included — answers an identical
919-byte CloudFront **"Request blocked" 403**. Not a challenge. It carries no
`x-amzn-waf-action` header, no challenge iframe and no widget of any kind, so
there is nothing on that page for any solver, at any price, to work on: the
request never reaches Sleep Number at all.

From a **residential** exit the same requests answer **200** with the full
catalogue. Measured on two: a GB residential address and a US residential
address, both served. So the block is datacentre *reputation*, not geography.

**What the paid products buy here is one specific thing: an exit address the
site will serve.** Nothing else. The catalogue itself needs no key, no
captcha solving and no fingerprint — Sleep Number server-renders everything
into the page, so a plain HTTP client on a home connection reads it and
parses to the same rows a browser does.

Full numbers are in the [repository README](https://github.com/2scraper/sleepnumber-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (primary), **Selenium** and **pyppeteer**, all producing the identical schema and exit codes, plus a browserless client for 2Captcha's Scraper API
- Two modes: a category or collection **listing**, or a single product's **detail** page — both read by the same parser, because the site's own data gives them the same shape
- Reads the site's **React Router hydration payload** (a turbo-stream document inlined in every page), so the primary path never touches a CSS class and never waits for a grid to paint
- **One row per size variant.** `QCM10` and `KCM10` are one model in two sizes at two different prices; a mattress price monitor that collapsed them would be useless
- JSON and CSV export, a 24-column schema, and a `.meta.json` sidecar on every run recording status, pages completed and **the site's own result arithmetic**
- 59 offline checks, each one controlled — the fault it exists to catch is planted and the suite is confirmed red before the check is trusted
- Optional 2Captcha integration, wired in but never required to get started

## Three things about Sleep Number worth knowing before you start

**1. The catalogue is not in the markup.** A category page carries zero
product links and zero `"sku"` strings as elements — the grid is painted
client-side. What every page does carry is the router's hydration payload, a
flat array in which every value is an index into that same array. Decode it
and every product, every size and every price is there before a script runs.
This repo ships the decoder.

**2. `?page=N` does nothing.** `?page=2` and `?page=99` both return page one,
HTTP 200, with the payload's own `page` field still reading `1`. A scraper
that trusted the parameter would loop forever or report the first page twice.
It is also moot: `per_page` is 100 and the largest category measured holds 20
products, so every category is a single page — and the whole sellable
catalogue is 88 products across 51 categories.

**3. Prices are integer cents.** `{"cents": 393750, "currency_iso": "USD"}` —
no thousands separator to disambiguate, no decimal comma, no currency symbol
to guess at. So this repo ships no price-parsing grammar at all, and
`currency` is a stated fact rather than an inference.

## Quick start

```bash
git clone https://github.com/2scraper/sleepnumber-scraper
cd sleepnumber-scraper
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --category mattresses --format both --out mattresses
```

On a home connection that is all you need. On a VPS, a CI runner or a
container, add a residential exit:

```bash
echo 'SLEEPNUMBER_PROXY=http://{login}-zone-resi-region-us:{password}@proxy.2captcha.com:2334' >> .env
```

## Licence

MIT. Not affiliated with, endorsed by, or connected to Sleep Number
Corporation.
