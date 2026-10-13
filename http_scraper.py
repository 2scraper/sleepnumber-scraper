#!/usr/bin/env python3
"""
sleepnumber-scraper — HTTP edition (no browser at all)
======================================================

Fetches with `requests` and hands the bytes to the same
`product_parser.parse_products` the three browser engines use, so a row
produced here and a row produced there cannot differ.

WHY THIS EXISTS, AND THE MEASUREMENT BEHIND IT
----------------------------------------------
Sleep Number server-renders its whole catalogue into the page, inlined as
the React Router hydration payload (see product_parser.py). Nothing on a
listing or a detail page waits for JavaScript, so a browser buys rendering
nobody needs here.

The README has said that since v0.1.0, and a third-party audit on
2026-10-13 pointed out — correctly — that the recommended path launched
Chromium anyway. Re-measured that day through one US residential exit,
three runs each, same URL:

    plain requests (fetch + parse)    1.4s / 1.3s / 2.1s   52 rows each
    playwright (whole run)            4.2s / 2.9s / 64.2s  exit 0 each

Median 1.4s against 4.2s, and the rows are byte-identical on every compared
field. The 64-second outlier is the rotating proxy gateway, not the engine;
it is left in rather than dropped, because it is the ordinary variance of
this setup and a median over three runs is already a small sample.

So on THIS site this is the engine to reach for. It is not a general claim:
a site that paints its grid client-side would get nothing here, which is
exactly why the browser engines stay.

WHAT IT DELIBERATELY DOES NOT HAVE
-----------------------------------
No `--headless/--headful`, no `--cdp-endpoint`, no `--fingerprint`, no
captcha flags. Each omission is honest on this site rather than a gap:

* there is no rendering to do, so there is no browser to configure;
* no challenge has ever been observed here — the refusal is a CloudFront
  deny carrying no widget, which no solver can help with (see
  captcha_solver.py's closing section). A captcha flag on this path would
  be a promise nothing can keep;
* a fingerprint describes a browser, and there is none.

What it DOES keep is the whole output contract: the same rows, the same
exit codes, the same `<out>.meta.json`, the same refusal to overwrite good
output with an empty run, and the same `--dump-html` on success as well as
failure.

AND IT IS NOT A FOURTH COPY OF THE PAGE LOOP
---------------------------------------------
The three browser engines each carry their own loop, which this family has
learned to regret (CLAUDE.md §26). This file does not add a fourth: it has
no browser to launch, relaunch or rotate, no readiness wait and no solve
budget, so what is left is fetch, classify, parse, merge. The shared
decisions it does need — how to classify a page, how to plan pages, when a
run is complete — come from `page_flow` and `output_writer`, the same way
they do for the browser engines.

YOU WILL STILL NEED AN EXIT THE SITE SERVES
--------------------------------------------
No browser setting ever changed that, and neither does removing the
browser. From a datacentre address every URL on this site answers an
identical 919-byte CloudFront "Request blocked" 403. Set SLEEPNUMBER_PROXY
in `.env`; see the README's "Access".

Usage
-----
    python3 http_scraper.py --category mattresses --format both --out mattresses

    python3 http_scraper.py --mode product --category cm-mattress

Requires: pip install -r requirements.txt   (no browser, no driver)
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
import page_flow
from product_parser import (parse_products, detect_bot_challenge,
                            is_supported_url, normalise_url,
                            category_url, product_url, SITE_ASSET_MARKERS)
from proxy_pool import from_args as proxy_pool_from_args, mask, ROTATE_MODES, ProxyError
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("http_scraper")


# A browser's header set. Not a disguise — this site refuses an ADDRESS, not
# a client, and that distinction is measured: a plain `curl` User-Agent and a
# Chrome one get the identical 919-byte CloudFront refusal from a datacentre
# exit, and both are served from a residential one. (That is the opposite of
# rakuten.co.jp in this family, where claiming Chrome without Chrome's TLS
# fingerprint is itself what gets refused — so do not port this header set to
# another site without measuring there.)
#
# They are sent because a server is entitled to a request that says what it
# wants: `Accept-Language` is what makes the US storefront answer in English,
# and `Accept-Encoding` is left to requests, which negotiates what it can
# actually decode.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

_CRED_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _mask_credentials(text: str) -> str:
    """Mask every credential in a string, not just the first.

    `requests` puts the FULL URL into the text of its exceptions, and a proxy
    URL carries a password — so an exception message is a log (CLAUDE.md §8).
    Masking is global because a masker that handles the first occurrence
    prints the password the other four times and looks like it is working.
    """
    return _CRED_RE.sub("://***:***@", str(text))


@dataclass
class PageOutcome:
    """What one page produced. Collected per page, merged in page order."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    rows: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    html: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# What `requests` raises when the PROXY is the problem rather than the site.
# They want opposite responses — a timeout deserves another try at the same
# exit, a dead proxy a different one — and catching only the timeout type is
# how this escapes as a traceback (§8).
#
# Measured on the rotating 2Captcha gateway this repo was developed against:
# a `ConnectionResetError` during the CONNECT handshake, surfacing as
# requests' ConnectionError, on roughly one request in fifteen.
_PROXY_ERRORS = (requests.exceptions.ProxyError,
                 requests.exceptions.SSLError,
                 requests.exceptions.ConnectionError)


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Never raises for an EXPECTED failure."""
    outcome = PageOutcome(page_num=page_num, url=url)
    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0

    for block_attempt in range(block_retries + 1):
        if pool:
            proxy = pool.current
            session.proxies = {"http": proxy, "https": proxy}
            logger.info("Using proxy exit %s", mask(proxy))

        response, last_error = None, None
        for attempt in range(1, args.retries + 1):
            logger.info("Fetching page %d: %s", page_num, url)
            try:
                response = session.get(url, headers=HEADERS, timeout=args.timeout,
                                       allow_redirects=True)
                break
            except _PROXY_ERRORS as exc:
                # Named on the spot: "the exit is dead" and "the site is slow"
                # read identically in a traceback and want different answers.
                last_error = f"proxy/transport failure: {_mask_credentials(exc)}"
                logger.warning("Page %d attempt %d/%d — %s", page_num, attempt,
                               args.retries, last_error[:200])
            except requests.exceptions.Timeout as exc:
                last_error = f"timeout: {_mask_credentials(exc)}"
                logger.warning("Page %d attempt %d/%d timed out after %.0fs",
                               page_num, attempt, args.retries, args.timeout)
            except requests.RequestException as exc:
                last_error = _mask_credentials(exc)
                logger.warning("Page %d attempt %d/%d — %s", page_num, attempt,
                               args.retries, last_error[:200])
            if attempt < args.retries:
                time.sleep(args.retry_delay * (2 ** (attempt - 1)))

        if response is None:
            outcome.load_failed = True
            outcome.state = "unknown"
            logger.error("Page %d: gave up after %d attempt(s). Last error: %s",
                         page_num, args.retries, last_error or "not recorded")
            if pool and block_attempt < block_retries:
                pool.advance(f"unusable exit: {last_error or 'no response'}")
                continue
            return outcome

        html = response.text
        outcome.html = html
        outcome.final_url = response.url
        # The status AND the headers reach the classifier. A served page
        # answers `x-cache: Miss from cloudfront` with `server: Fly/…`; a
        # refusal answers `Error from cloudfront` — the one signal a body
        # cannot forge, and the reason this is not a bare body check.
        outcome.state = page_flow.classify(
            html, status=response.status_code, url=response.url,
            headers={k.lower(): v for k, v in response.headers.items()})
        logger.info("Page %d: HTTP %d, %d bytes, state=%s",
                    page_num, response.status_code, len(response.content),
                    outcome.state)

        if args.dump_html:
            # On SUCCESS as well as failure: a run can return the right count
            # with a field silently unpopulated, and then the exact bytes are
            # the only way to tell a parsing bug from a bad fetch.
            path = (f"{args.dump_html}.page{page_num}" if args.pages > 1
                    else args.dump_html)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(html)
            logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                        path, len(html))

        if page_flow.counts_as_blocked(outcome.state):
            outcome.blocked_by = detect_bot_challenge(html) or outcome.state
            # State what was OBSERVED before saying why (CLAUDE.md §28): a
            # message that leads with a recommendation sends a reader off to
            # buy something before they know what actually came back.
            logger.error(
                "Page %d was not served: %d bytes, state=%s, site assets "
                "present=%s. This site refuses datacentre addresses with an "
                "identical 919-byte CloudFront 403 - see the README's "
                "Access section. There is no widget on that refusal, so no "
                "solver can help; what is needed is a residential exit.",
                page_num, len(html), outcome.state,
                any(m in html.lower() for m in SITE_ASSET_MARKERS))
            if pool and block_attempt < block_retries:
                logger.info("Rotating to another exit and retrying page %d.", page_num)
                pool.advance(f"{outcome.state} on page {page_num}")
                continue
            return outcome

        break

    rows = parse_products(html, response.url,
                          page=page_num if args.mode == "listing" else None)
    logger.info("Parsed %d row(s) from page %d.", len(rows), page_num)

    if rows:
        priced = sum(1 for row in rows if row.price is not None)
        coverage = priced / len(rows)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%).",
                    page_num, priced, len(rows), 100.0 * coverage)
        if coverage < page_flow.PRICE_COVERAGE_FLOOR:
            logger.warning("Price coverage on page %d (%.0f%%) is below the "
                           "%.0f%% floor — every variant measured on this "
                           "site publishes a price, so check for a parsing "
                           "regression.", page_num, 100.0 * coverage,
                           100.0 * page_flow.PRICE_COVERAGE_FLOOR)
    else:
        if outcome.state == "no_listing":
            logger.error(
                "This URL is not a product listing. The site routed and "
                "rendered it, but its payload carries no product list at all "
                "— some /categories/... paths are curated landing pages "
                "rather than listings. Pass a category that actually holds "
                "products, e.g. --category mattresses.")
        elif page_flow.is_broken_parse(outcome.state or "content", html, 0):
            outcome.state = "broken_parse"
            logger.error("Page %d was SERVED and links to products, and "
                         "parsed to zero rows. That is a bug in this parser, "
                         "not an empty category.", page_num)

    outcome.rows = rows
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    stop_reason = "single_page_mode" if args.mode not in page_flow.PAGINATED_MODES else "completed"

    pool = proxy_pool_from_args(args)

    with requests.Session() as session:
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode in page_flow.PAGINATED_MODES and args.pages > 1:
            seen_keys.update(r.sku for r in first.rows if r.sku is not None)
            planned = page_flow.plan_pages(first.html or "", first.final_url or args.url,
                                           args.pages)
            if not planned:
                logger.info("The site says there is no page 2 — stopping here.")
                stop_reason = "pagination_exhausted"
            for page_num, url in enumerate(planned, start=2):
                time.sleep(args.delay)
                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break
                fresh = sum(1 for r in outcome.rows
                            if r.sku is None or r.sku not in seen_keys)
                seen_keys.update(r.sku for r in outcome.rows if r.sku is not None)
                if not fresh:
                    # The terminating condition is DATA, never a selector and
                    # never the ?page= parameter, which this site ignores.
                    logger.info("Page %d added no sku not already seen — "
                                "treating that as the end of the listing.", page_num)
                    stop_reason = "no_new_products"
                    break

    # Merge in PAGE order, not arrival order.
    all_rows: List = []
    seen: set = set()
    for outcome in sorted(outcomes, key=lambda o: o.page_num):
        all_rows.extend(dedupe_by_key(outcome.rows, seen, "sku"))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    arithmetic = {}
    first_html = next((o.html for o in outcomes if o.html), None)
    if first_html and args.mode == "listing":
        arithmetic = page_flow.listing_arithmetic(first_html)
    arithmetic["transport"] = "http"

    # Did we parse everything the SITE says is there? It counts products
    # while a row here is a size variant, so the comparison is on distinct
    # product_id. A shortfall is the response contradicting us, which is a
    # stronger signal than any coverage threshold — see
    # page_flow.products_shortfall. Only meaningful for a run that otherwise
    # finished: a blocked or truncated run is already not complete.
    if (args.mode == "listing" and first_html
            and stop_reason in ("completed", "pagination_exhausted",
                                "no_new_products", "single_page_mode")):
        missing = page_flow.products_shortfall(
            first_html, (r.product_id for r in all_rows))
        if missing:
            logger.error(
                "The site says this listing holds %s products; we parsed %d. "
                "%d missing. That is this parser falling short of what the "
                "response itself states, not an empty shelf — reporting the "
                "run as partial rather than complete.",
                arithmetic.get("total_results"),
                len({r.product_id for r in all_rows if r.product_id}), missing)
            stop_reason = "parser_incomplete"
            arithmetic["products_missing"] = missing

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      extra=arithmetic,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=SOURCE_DEFAULT,
                      start_url=args.url, final_url=final_url)


def parse_args():
    p = argparse.ArgumentParser(
        description="Sleep Number scraper (HTTP edition — no browser). "
                    "The catalogue is server-rendered into every page, so "
                    "this reads the same rows as the browser engines in "
                    "about a third of the time.")
    p.add_argument("--mode", choices=list(page_flow.MODES), default="listing",
                   help="listing (default) or product.")
    p.add_argument("--url", default=None,
                   help="The exact URL to read, replacing --category.")
    p.add_argument("--category", default=None,
                   help="A category/collection slug for --mode listing, or a "
                        "product slug for --mode product.")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to crawl. No category measured on this "
                        "site has more than one.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds.")
    p.add_argument("--retries", type=page_flow.attempts, default=3,
                   help="Attempts per page before giving up (default 3). "
                        "Counts ATTEMPTS, so 1 means 'try once, do not "
                        "retry'; 0 is a usage error.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="Per-request timeout, seconds. Every remote call here "
                        "is bounded.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="sleepnumber_products",
                   help="Output file prefix.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. You will need one from a datacentre — see "
                        "the README's Access section. Prefer SLEEPNUMBER_PROXY "
                        "in .env: a secret on a command line is readable by "
                        "anything that can run ps.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused, retry it from this "
                        "many OTHER exits before giving up. Needs a pool.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    args = p.parse_args()
    env_config.apply(args)

    if not args.url:
        if args.mode == "product":
            if not args.category:
                p.error("--mode product needs --category (a product slug, "
                        "e.g. cm-mattress) or --url")
            args.url = product_url(args.category)
        else:
            args.url = category_url(args.category or "")

    args.url = normalise_url(args.url)
    supported, why = is_supported_url(args.url)
    if not supported:
        p.error(why)

    if args.mode not in page_flow.PAGINATED_MODES and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read.", args.pages, args.mode)
        args.pages = 1
    return args


if __name__ == "__main__":
    try:
        sys.exit(scrape(parse_args()))
    except ProxyError as exc:
        logger.error("%s", exc)
        sys.exit(2)
