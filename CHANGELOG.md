# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A **patch** release means fixes; it does not
mean every flag and every default is frozen. Where a fix changes behaviour an
existing user depends on, the release note says so first, in a blockquote, so
nobody discovers it from a bill or a broken cron job.

## [Unreleased]

> **Behaviour changes.** (1) A run through a proxy now makes one pre-check
> request before its first page request, in `http_scraper.py` and all three
> browser engines. A proxy that answers **HTTP 407** or **402** ends the run
> with **exit 5** and `<out>.latest_attempt.meta.json` (`status: failed`,
> `stop_reason: proxy_auth_failed` / `proxy_balance_exhausted`) instead of
> failing every retry. (2) Through a local proxy, the browser engines **no
> longer download** images, video/audio or fonts; `--load-media` (or
> `SLEEPNUMBER_LOAD_MEDIA=1`) restores them. Runs without a proxy, over
> `--cdp-endpoint`, and the HTML-only `http_scraper.py` fetch are otherwise
> unchanged.

### Added

- `proxy_check.py`: the proxy pre-check (one CONNECT plus `HEAD /robots.txt`
  on www.sleepnumber.com, no third-party service), shared by
  `http_scraper.py` and the browser engines, plus media blocking for the
  three browser engines. The 407 message names `SLEEPNUMBER_PROXY` and says
  the gateway refused the credential (or the traffic balance is exhausted);
  a 402 says the proxy traffic/balance is exhausted. Neither prints the
  credential. A proxy that does not answer keeps the existing retry
  behaviour.
- `--load-media` on the three browser engines, and `SLEEPNUMBER_LOAD_MEDIA`
  in `.env.example`.
- Offline checks:
  - a stub proxy that answers 407/402/200 or nothing;
  - `http_scraper.py` and each engine driven to exit 5 on a 407, with no
    page request and no browser;
  - the attempt sidecar;
  - the Playwright route handler, the pyppeteer Fetch patterns and the
    Selenium blocked-URL list;
  - when each engine installs blocking.

### Changed

- CI only: the canary bills the `SLEEPNUMBER_PROXY` exit, so it now runs weekly
  (Mondays 11:23 UTC) instead of daily; a manual dispatch still runs it.

## [0.3.0] — 2026-10-16

> **Behaviour changes:**
>
> - **A typed `--category` now wins over `SLEEPNUMBER_URL`.** With that
>   variable in the environment or `.env`, `--category furniture` used to be
>   ignored silently and the environment's URL was read instead. Typing both
>   `--url` and `--category` is now a usage error (exit 2), because the two
>   can name different things. `.env.example` ships `SLEEPNUMBER_URL`
>   commented out.
> - **Negative or zero counts are usage errors (exit 2)** in every entry
>   point: `--pages` and `--concurrency` must be at least 1; `--delay`,
>   `--retry-delay` and `--proxy-block-retries` must be 0 or more. A negative
>   delay used to reach `time.sleep` and crash the run after pages had been
>   fetched.
> - **CSV cells that begin with `=`, `+`, `-`, `@`, a tab, CR or LF get a
>   leading apostrophe** so a spreadsheet reads them as text. Strings only;
>   numbers and the JSON output are unchanged, and the sidecar records
>   `csv_cells_escaped`. Measured on the committed sample and fixtures: 0 of
>   362 string cells qualify, so on today's catalogue this changes nothing.
> - **At most one paid captcha solve per page**, across every retry and exit
>   rotation. The solver used to run once per block attempt, so a page
>   fetched through a 3-exit pool could buy 3 solves. No challenge has ever
>   been observed on this site, so this was a latent cost, not a bill.
> - **The canary fails, rather than skipping green, when no
>   `SLEEPNUMBER_PROXY` secret is set.**

### Fixed

- **Output files are written atomically.** JSON, CSV and the
  `.meta.json` sidecar go to a temporary file in the same directory, are
  flushed and fsynced, then renamed over the target. A failure halfway
  through a write used to leave a truncated file where a good one had been
  (reproduced: a 559-byte `out.json` became 13 bytes of invalid JSON). A new
  file gets the mode `open()` would have given it; an existing file keeps
  its own.
- **The credential scan reads what it used to skip.** Its exclusions tested
  the absolute path, so a clone under a directory called `venv/` scanned 0
  files and printed the clean line; they now test the path relative to the
  repo, and a scan of zero files fails. `.json`, `.csv`, `.toml`, `.cfg`,
  `.ini` and renamed copies of `.env` (`.env.bak`, `.env.local`) are now
  read.
- **The pyppeteer engine hands the HTTP status and headers to the
  classifier**, as the Playwright engine does.
- **`.env.example` and the README no longer say the block tracks geography
  or that Scraping Browser credentials last "about a day".** The block is
  address reputation (GB and US residential exits were both served), and the
  credential lifetime was never measured here.
- Comments that described other sites' measurements as this site's were
  corrected, and references to private notes were replaced with the rule in
  the repo's own words. A suite check keeps them out.
- The Docker image now carries `http_scraper.py`, and CI tests Python 3.13,
  which the badge already claimed.

## [0.2.0] — 2026-10-13

> **Behaviour change:** `--retries 0` is now a usage error (exit 2) in every
> entry point. It used to be accepted and made the load loop body
> unreachable, so nothing was fetched and the engine reported the browser's
> own blank page as a block — exit 3, with a 39-byte "capture", in about 40
> milliseconds. If a script of yours passes 0, pass 1: the flag counts
> ATTEMPTS, so 1 means "try once and do not retry".
>
> Also: `--retries` on `scraper_api_client.py` used to mean EXTRA attempts
> while meaning ATTEMPTS everywhere else. It now means attempts there too,
> and its default moves 1 → 2 so the number of requests actually made is
> unchanged.

### Added

- **`http_scraper.py` — a fourth entry point that launches no browser.**
  Sleep Number server-renders its whole catalogue into the page, so there is
  nothing to wait for and nothing to scroll. Measured 2026-10-13 through one
  US residential exit, three runs each: **1.4s / 1.3s / 2.1s** against
  Playwright's 4.2s / 2.9s / 64.2s, and **52 rows whose every field is
  identical**. It is now the path the README recommends for this site. It
  deliberately takes no `--headless`, `--cdp-endpoint`, `--fingerprint` or
  captcha flags, because there is no browser to configure and no challenge
  has ever been observed here.
- The canary now runs both transports daily and **asserts they agree field
  for field** — the family's "every engine agrees" contract under test
  rather than asserted. Controlled: a one-cent difference turns it red.
- `http_scraper.py` and `scraper_api_client.py` joined the `--help` check in
  `ci_checks.py`; neither had been covered.
- **A run is now checked against the site's own count of products.** The
  payload states `total_results`; a run that parses fewer distinct products
  than that reports `partial` (exit 6) with `stop_reason:
  parser_incomplete` and `products_missing` in the sidecar, instead of exit
  0 with a quietly short file. Controlled live: a parser made to drop one
  product returned exit 6, `products_missing: 1`.

  This answers the audit's point about price coverage only warning, and does
  it with the response rather than a threshold. `total_results` was measured
  on 2026-10-13 to equal the products in the payload exactly across four
  categories (7, 10, 20 and 9), so an inequality is our problem.

### Fixed

- **`--retries 0` fetched nothing and called it a block** (see above). An
  audit ran exactly that command on 2026-10-13 and reported the site as
  unreachable; its 39-byte evidence was `about:blank`. The site does refuse
  this address — `--retries 1` gets the real 923-byte CloudFront 403 — so
  the conclusion was right and the evidence was about our bug.
- **The canary asserted a sale that has since ended.** It required the three
  canary categories to disagree about discounting by at least 20 points,
  from figures measured during a September sale (52 of 52 mattresses
  discounted). On 13 October the same categories read 0, 11 of 194 and 0,
  with the parser working perfectly — so the check would have been red every
  day for a catalogue that simply is not discounted. Removed; the discount
  column is still guarded by assertions that hold either way (no
  `original_price` at or below its price, no non-positive discount, 100%
  price coverage).

- **A test fixture claimed what no response says.** The listing fixture
  inherited `total_results` from the full capture while its product list was
  trimmed, so it asserted 7 products while holding 2 — which made a correct
  parse look like a five-product shortfall the moment anything compared the
  two. The generator now re-states the total for what it kept, and proves it
  before writing.

### Documentation

- **The README promised a sidecar "per run" and means "per run that writes
  rows".** A blocked run deliberately writes neither file: the row file is
  left holding the last good run, and a `"status": "failed"` sidecar beside
  it would describe a run that did not produce the data next to it. Now said
  explicitly, with what a blocked run does leave behind.
- Re-measured figures rather than inheriting them: the catalogue is **91
  products across 52 categories** today against 88 and 51 in September, and
  the sitemap holds 1,962 URLs against 1,994. Both tables now carry two
  dates and the command that reproduces them.

### Fixed

- **Scraper API: `waitFor` is now sent as a JSON object.** It went out as a
  JSON-encoded string, following a docstring that said the API required one.
  Measured 2026-09-23 against `/tasks/sync`: the string form is refused with
  HTTP 422 ("params.waitFor must be an object") and still billed ($0.0005);
  the object form answers HTTP 200. Every `--wait-*` run was therefore
  a paid exit 5.
- **Scraper API: the target's HTTP status is read from `http_code`.** The
  client read `status`, which is the API's own verdict string (`"success"`),
  so the page classifier never saw a target 403/503 — a live rakuten run
  logged "Upstream page status success". `status` is now a fallback only
  when it is an integer.
- **Leftovers from the repos this one was copied from** (woolworths-scraper,
  transfermarkt-scraper, vrbo-scraper):
  - `TROUBLESHOOTING.md` described Transfermarkt's modes (`market-values`,
    `club-squad`, `player`) and said datacentre access was unmeasured. It now
    carries this site's CloudFront 403, `no_listing` and column traps from
    the README.
  - `CONTRIBUTING.md` and both issue templates described Woolworths' API,
    shadow-DOM tiles and Akamai. Rewritten for the React Router payload and
    CloudFront.
  - `tools/browser_profile_client.py` told you to run
    `--mode market-values`, which this repo rejects; it now prints a
    `--category mattresses` run.
  - `tools/verify_browser_api.sh` was Transfermarkt's AWS WAF probe
    (`--mode market-values`, a `waf=captcha` control), and failed on its
    first line here. Removed rather than ported: this site serves no
    challenge for the Browser API to clear, and `tools/waf_probe.sh`
    already answers "is this exit served".
  - `.dockerignore` ignored `vrbo_products.*`. It now ignores this repo's
    default `--out` prefix, `sleepnumber_products.*`.
  - `diff_runs.py`: its `--price-tolerance-pct` help said "each country site
    quotes its own currency" (mediamarkt's), a comment described a
    `lowest_price_30d` column this repo does not have, and the
    `price_source` comment named `jsonld+dom` / `dom` values this parser
    never writes. Rewritten for one USD storefront and the real
    `payload` / `payload+jsonld` / `url-fallback` values.
  - Stray comments and one log line: "an empty squad", `player`/`club-squad`
    modes, a "measured headful on 2026-09-16" pyppeteer claim, and a
    tests.yml note saying this site refuses headless (it does not).
- **README badges.** Added the release badge. The access badge said a
  2Captcha account is not required, which is true only on a home connection:
  every datacentre address gets a CloudFront 403. It now reads "needs a
  residential IP".
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

- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.


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

[Unreleased]: https://github.com/2scraper/sleepnumber-scraper/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/2scraper/sleepnumber-scraper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/2scraper/sleepnumber-scraper/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/2scraper/sleepnumber-scraper/releases/tag/v0.1.0
