#!/usr/bin/env python3
"""
sleepnumber-scraper — 2Captcha Scraper API edition (fourth entry point)
=======================================================================

Unlike playwright_scraper.py / puppeteer_scraper.py / selenium_scraper.py,
this one manages NO browser locally. It posts the URL to 2Captcha's Scraper
API, which runs the fetch on its own infrastructure and returns the HTML,
and then hands that HTML to the same `product_parser.parse_products` the
browser engines use — so the two paths cannot disagree about what a row is.

WHEN IT HELPS HERE, AND WHEN IT DOES NOT
-----------------------------------------
Sleep Number's catalogue is entirely server-rendered into the page's
hydration payload, so there is nothing to wait for and nothing to scroll:
this is exactly the shape of page a browserless fetch handles well. The
whole difficulty on this site is the EXIT ADDRESS, not the rendering.

Which is also the catch, and it is measured rather than assumed — see the
README's "Access". Run it plain and read the exit code:

    exit 0   the API's own exits reached the site
    exit 3   they were refused, the same CloudFront 403 a datacentre address
             gets. Then pass --cdp-url to route the fetch through a Scraping
             Browser session, whose exit is residential.

A sibling repo (bbb-scraper) measured its target refusing the Scraper API's
own exits while the same task through a Scraping Browser session came back
200 — so this is a live possibility rather than a hypothetical, and the flag
exists for it.

Usage
-----
    python scraper_api_client.py --url https://www.sleepnumber.com/categories/mattresses

    python scraper_api_client.py \
        --url https://www.sleepnumber.com/products/cm-mattress \
        --cdp-url "$SLEEPNUMBER_CDP_ENDPOINT" --out cm

`--key` defaults to $TWOCAPTCHA_KEY and `--cdp-url` to
$SLEEPNUMBER_CDP_ENDPOINT, so neither needs to be typed — a secret on a
command line is readable by anything that can run `ps` (§3).

Requires: pip install -r requirements.txt   (no browser at all)
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

import requests

from product_parser import (BOT_CHALLENGE_MARKERS, detect_bot_challenge,
                            detect_page_state, parse_products,
                            category_url, product_url, normalise_url,
                            is_supported_url)
from output_writer import save
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

# Internal marker, never returned to the shell — main() maps it to 3. It
# separates "refused, and trying again will be refused identically" from
# "a challenge, which another attempt may clear". Both are exit 3 to the
# caller; only one is worth paying for twice.
EXIT_REFUSED_DETERMINISTIC = -3

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


def _build_wait_for(args) -> Optional[str]:
    """`waitFor` must be a JSON STRING (double-encoded), per the API docs.
    Passing a nested object is silently wrong.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return json.dumps({"text": args.wait_text})
    if args.wait_element:
        return json.dumps({"element": args.wait_element, "checkVisible": True})
    if args.wait_state:
        return json.dumps({"state": args.wait_state})
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # so we get {"status", "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", wait_for)

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", debug)

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    upstream_status = body.get("status")
    logger.info("Upstream page status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    return html, upstream_status


def main() -> int:
    args = parse_args()

    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2

    # A challenge page is not necessarily final (see _run_once), so a
    # single attempt is not evidence. Each retry is a fresh billable task —
    # $0.0005 at the observed rate — so the default is deliberately low.
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        rc = _run_once(args, attempt, attempts)
        if rc == EXIT_REFUSED_DETERMINISTIC:
            # A CloudFront deny is a property of the EXIT ADDRESS, and the
            # Scraper API's exits do not change between attempts on this
            # site: measured 2026-09-17, two consecutive attempts returned
            # the identical 919-byte refusal and were billed $0.0005 each.
            # Retrying it buys a second copy of a certainty, which is §8's
            # "detected is not the same as paying" with a price tag. A
            # challenge page WOULD be worth retrying — that is why the two
            # are different return values rather than one.
            return 3
        if rc != 3 or attempt == attempts:
            return rc
        logger.info("Challenge page on attempt %d/%d — retrying in %ds.",
                    attempt, attempts, args.retry_delay)
        time.sleep(args.retry_delay)
    return rc


def _run_once(args, attempt: int = 1, attempts: int = 1) -> int:
    if attempts > 1:
        logger.info("Attempt %d/%d", attempt, attempts)

    try:
        html, upstream_status = fetch_html(args)
    except requests.RequestException as e:
        logger.error("Network error talking to the Scraper API: %s", e)
        return EXIT_API_ERROR
    except RuntimeError as e:
        # HTTP 4xx/5xx from the API, including the 422 that a busy or
        # unreachable cdpurl produces.
        logger.error("%s", e)
        return EXIT_API_ERROR

    if args.dump_html:
        with open(args.dump_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Raw HTML written to %s", args.dump_html)

    # Same policy as the browser engines: the status decides the blocked
    # case, because this site's refusal has no marker to detect.
    state = detect_page_state(html, status=upstream_status, url=args.url)
    if state == "blocked":
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.error(
            "Sleep Number did not serve the Scraper API's request "
            "(upstream HTTP %s, %d bytes) — saved to %s. This site refuses "
            "every datacentre address with an identical CloudFront 403, and "
            "the Scraper API's own exits are datacentre addresses. Pass "
            "--cdp-url to route the fetch through a Scraping Browser "
            "session, whose exit is residential. This is exit 3, distinct "
            "from an empty result (exit 4).",
            upstream_status, len(html), dump)
        return EXIT_REFUSED_DETERMINISTIC

    vendor = detect_bot_challenge(html)
    if vendor:
        logger.error(
            "The Scraper API returned a %s bot-challenge page (%d bytes), not real content.",
            vendor, len(html),
        )
        logger.error("A challenge page is not a final answer — retry before "
                     "concluding anything (--retries). On this site what "
                     "clears a refusal is a different EXIT, not a retry — "
                     "is the EXIT, not a solver: pass --cdp-url to route "
                     "through a Scraping Browser session, or use "
                     "playwright_scraper.py / puppeteer_scraper.py directly.")
        return 3

    # The SAME parser the three browser engines call, with the same
    # arguments, so a row produced here and a row produced there cannot
    # differ. `page` is threaded for a listing and left None for a detail
    # page, exactly as each engine's _parse_for_mode does it.
    products = parse_products(html, args.url,
                              page=1 if args.mode == "listing" else None)
    if args.category:
        for row in products:
            row.category = args.category
    if products:
        priced = sum(1 for r in products if r.price is not None)
        logger.info("Parsed %d row(s); price coverage %d/%d.",
                    len(products), priced, len(products))
    else:
        logger.error("The Scraper API returned %d bytes that parsed to zero "
                     "rows. Writing nothing rather than replacing a previous "
                     "good result with an empty one.", len(html))

    if not products:
        dump = f"{args.out}_scraperapi_debug.html"
        with open(dump, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("0 rows parsed — saved the raw response to %s so "
                       "you can see what actually came back.", dump)
        return 4

    return save(products, args.out, args.format, allow_empty=args.allow_empty)


def parse_args():
    p = argparse.ArgumentParser(
        description="Sleep Number scraper — 2Captcha Scraper API edition (no local "
                    "browser). Both page kinds are a good fit: Sleep "
                    "Number server-renders its whole catalogue into the "
                    "page, so there is nothing to wait for and nothing to "
                    "scroll. --cdp-url is effectively REQUIRED — measured "
                    "2026-09-17, the Scraper API's own exits are datacentre "
                    "addresses and this site answers them with the same "
                    "919-byte CloudFront 403 it gives any other.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var. A key passed on the
    # command line is visible to anyone who can run `ps`, and it lands in
    # shell history and in any log that echoes the command line.
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="A www.sleepnumber.com URL — a /categories/…, "
                        "/collections/… or /products/… page. Defaults to "
                        "endpoint answers that for free. Required, unless "
                        "$SLEEPNUMBER_URL, then to the default category.")
    p.add_argument("--mode", choices=["listing", "product"],
                   default="profile",
                   help="Default profile, unlike the browser engines, "
                        "because a profile is the only page kind this path "
                        "reads that the free endpoint cannot.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="sleepnumber_products_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page, e.g. '$'. Use this "
                           "on protected sites — a DOM/load wait is satisfied instantly by "
                           "the challenge page itself.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible, e.g. 'a[href*=\"-item-\"]'")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were parsed. Off by "
                        "default so a failed fetch can't overwrite a good result.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a bot-challenge page comes back. One retry is "
                        "usually worth it. Each attempt is a separate billable task, so "
                        "this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the raw returned HTML to this path (always, even on success)")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "SLEEPNUMBER_CDP_ENDPOINT": "cdp_url",
        "SLEEPNUMBER_URL": "url",
    })
    if not args.url:
        args.url = (product_url(args.category) if args.mode == "product" and args.category
                    else category_url(args.category or ""))
    args.url = normalise_url(args.url)
    supported, why = is_supported_url(args.url)
    if not supported:
        p.error(why)
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
