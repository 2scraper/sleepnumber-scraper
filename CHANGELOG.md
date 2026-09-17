# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A **patch** release means fixes; it does not
mean every flag and every default is frozen. Where a fix changes behaviour an
existing user depends on, the release note says so first, in a blockquote, so
nobody discovers it from a bill or a broken cron job.

## [Unreleased]

### Fixed

- **The site's own 404 was recognised only by its status code.**
  `_NOT_FOUND_MARKERS` read `<title>not found` and `page not found`, and
  NEITHER is on the page Sleep Number actually serves: `/products/i8` comes
  back with `<title>Sleep Number</title>`. A capture handed to the parser
  without a status — a `--dump-html` file, a fixture — therefore came out as
  `empty`, a claim about the catalogue. Now recognised structurally: React
  Router puts one key per matched route into `loaderData`, and a URL that
  matched none has `root` and nothing else (measured: one route key on all 17
  routed captures, zero on both real 404s). The text marker is corrected to
  `not found`, which was counted at 0 on every routed page and 2-3 on both
  404s.
- **The check that should have caught it tested an invented page.** It built
  a fixture containing `<title>Not Found</title>` and passed. The suite now
  runs against the real 404, captured the way a real run captures, and
  asserts the no-status case the invented fixture hid.
- **A landing page was reported as an empty category.** Some `/categories/…`
  URLs are curated pages rather than listings — `/categories/beds-on-sale`
  renders 320 KB with `category.name = "Sale"` and no `products` key at all.
  New state `no_listing`, and all three engines say the same sentence about
  it instead of "zero products", which read as a claim about the shelf.
- **Only one engine explained a zero-row page.** The `is_broken_parse`
  branch existed in `playwright_scraper.py` and in neither twin, so the same
  page produced three different messages. All three now share the wording, and
  a check asserts it.

### Verified live, not from memory

All four entry points, and Selenium's real limit is narrower than the README
claimed:

- Playwright, pyppeteer and **Selenium** (through a local relay that adds the
  proxy credentials) each return 52 rows, exit 0, `complete` — and the three
  row sets are byte-identical across all 14 compared fields.
- Playwright over a **Scraping Browser CDP endpoint**: 52 rows, exit 0.
- **Scraper API with `--cdp-url`**: upstream 200, 882 KB, 52 rows, exit 0,
  $0.0005. Without it, exit 3 — its own exits are refused.
- No credential reached any process `argv` during a live run, the browser's
  own command line included, nor any log, sidecar, output file or debug dump.
- A blocked run left the previous good output and its `complete` sidecar
  untouched.
- `diff_runs.py` separates a real price move from a `price_source` change,
  and `--fail-on-change` ignores the latter.


## [0.1.0] — 2026-09-17

First release. Reads sleepnumber.com's catalogue — one row per size variant,
with its price, sale price, rating and availability — through four entry
points that share one parser.

### Added

- **Two modes.** `--mode listing` reads a `/categories/…` or `/collections/…`
  page; `--mode product` reads a `/products/…` detail page. Both produce the
  same rows from the same parser, because the site's own loader data gives
  them the same object shape.
- **Four entry points.** `playwright_scraper.py` (recommended),
  `puppeteer_scraper.py`, `selenium_scraper.py`, and `scraper_api_client.py`
  for a fetch with no local browser at all. All four share
  `output_writer.finish_run()` and `page_flow.STATE_POLICY`, so they cannot
  drift on exit codes or on whether a page is worth retrying.
- **A turbo-stream decoder.** Sleep Number's catalogue is not in the markup:
  a category page carries zero product anchors and zero `"sku"` strings. It
  is inlined as the React Router hydration payload, a flat array in which
  every value is an index into that same array. `decode_turbo_stream` reads
  it, and the complete catalogue is available before any script runs.
- **`price_source` per row**, recording which views agreed: `payload`,
  `payload+jsonld` (measured 7 of 7 on a capture that publishes both),
  `payload_jsonld_disagree` (the payload is kept and the parser warns), or
  `url-fallback` (no price at all, and said so rather than implied).
- **The site's own arithmetic in the sidecar** — `total_results`, `per_page`,
  `pages_available`, `page_param_honoured` — because "complete" and
  "exhaustive" are different words.
- `tools/capture.py`, which fetches a URL set and reports what a parser could
  read: JSON-LD block count and types, inlined-state shapes,
  endpoint-shaped strings, and repeating URL segments. Every figure in the
  README is reproducible with it.
- `tools/make_fixtures.py`, which builds the offline suite's fixtures from
  real captures by decoding, reducing and re-encoding — asserting that the
  result decodes back to objects equal to what went in.
- 59 offline checks, passing with no engine library installed, and each one
  controlled: the fault it exists to catch is planted, the suite is confirmed
  red, and the run is checked for the expected check name rather than merely
  for a non-zero exit.

### Measured

Everything below is dated 2026-09-17 and reproducible with the commands in
the README. Nothing here is inherited from a sibling repository.

- **Access is an address problem.** Every URL on the site — `/robots.txt`
  included — answers an identical 919-byte CloudFront "Request blocked" 403
  from a datacentre address, whatever the headers, HTTP version or user
  agent. The same requests answer 200 from a GB residential exit and from a
  US residential exit, so the block is datacentre reputation rather than
  geography.
- **No captcha is involved.** The refusal carries no `x-amzn-waf-action`
  header and no widget of any kind. `STATE_POLICY` therefore marks a block
  retryable — a different exit may work — but **not** solvable.
- **`?page=N` is ignored.** `?page=2` and `?page=99` both return page one,
  HTTP 200, with the payload's own `page` still reading 1. `--concurrency` is
  refused with that reason rather than silently ignored.
- **Price coverage is 100%** on every listing measured: 52/52, 211/211,
  169/169, 27/27.
- **The Scraper API's own exits are refused.** Use `--cdp-url` to route
  through a Scraping Browser session.
- **Selenium cannot work here from a datacentre**, because it cannot
  authenticate a proxy and the proxy is the whole point. It reports exit 3
  with the reason.

### Fixed before release

Four defects found by the offline suite in code that had already produced a
green live run, and five more found by running paths a credential gates.
Recorded because they are the argument for writing those checks at all:

- `detect_bot_challenge` was called by all three engines and imported by
  none — a `NameError` on the first refused page, on a branch a successful
  run never reaches.
- `page_flow.is_thin_page` was called by all three engines and did not
  exist. Caught only because the signature-binding check treats a missing
  attribute as a failure rather than skipping it.
- The Dockerfile's `COPY` list omitted four modules the entrypoint imports —
  the defect that ships an image dying with `ModuleNotFoundError` on every
  invocation, `--help` included.
- `--mode product` reported `partial` on a run that was complete, which would
  have made `diff_runs.py` refuse to compare two product runs.
- `diff_runs.py` was tracking `claps` and `responses`: the copied file was
  medium-scraper's. Replaced with the price-shaped version.
- The Scraper API client retried a deterministic CloudFront refusal and was
  billed for each attempt. A refusal now returns immediately; a challenge
  page still retries.
- Selenium reported exit 4 — "this category has no products" — for a
  navigation that never happened. A blank document is now classified
  structurally and answers `blocked`.

[Unreleased]: https://github.com/2scraper/sleepnumber-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/sleepnumber-scraper/releases/tag/v0.1.0
