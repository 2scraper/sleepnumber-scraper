#!/usr/bin/env python3
"""smoke_test.py — the offline suite.

One file of plain functions with inline fixtures, plus the real captures in
`fixtures_generated.json`. No pytest, no conftest, no
fixtures directory. `tests/test_smoke.py` wraps this as a single pytest test
so `pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It MUST pass with no engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is recorded. CI's `engine-smoke` job installs each
engine in its own virtualenv and FAILS if the matching group reports a skip,
because "skipped, engine absent" reads identically to a broken import.

What this suite is FOR, beyond the obvious
------------------------------------------
Most of these checks exist because something was, or could easily have been,
actually wrong on THIS site. The ones worth knowing before you edit:

* `recaptcha` and `captcha` each appear once on EVERY page Sleep Number
  serves — the base template ships a reCAPTCHA loader. Either one used as a
  block marker reports the whole catalogue as challenged.
  `test_markers_absent_from_a_good_page` counts them so neither can come
  back.
* The site's own JSON-LD points at `sndotcom.fly.dev`, its Fly origin.
  Read verbatim, every row links to a dev host (the "product URL under
  offers.url" with a different face).
* `?page=2` and `?page=99` both return page ONE, HTTP 200. A count-based
  pagination loop would never terminate.
* `regular` equals `sale` on everything not discounted, so copying it into
  `original_price` asserts a was-price on every undiscounted row.
* `review_stats` reports 0 for an unreviewed product, and a 0 written
  through drags every average a consumer computes.
* A CloudFront refusal reaches a parser in two encodings — HTTP-client bytes
  and browser DOM — and the suite pins both.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import product_parser as P          # noqa: E402
import page_flow                    # noqa: E402
import output_writer                # noqa: E402
import env_config                   # noqa: E402
import proxy_pool                   # noqa: E402
import captcha_solver               # noqa: E402
from output_writer import Product   # noqa: E402

VERBOSE = "-v" in sys.argv
FAILURES: list = []
PASSES: list = []
SKIPS: list = []

FIXTURES = json.loads((ROOT / "fixtures_generated.json").read_text(encoding="utf-8"))
LISTING_HTML = FIXTURES["listing_html"]
DETAIL_HTML = FIXTURES["detail_html"]
LISTING_EXPECTED = FIXTURES["listing_expected"]
DETAIL_EXPECTED = FIXTURES["detail_expected"]

NOTFOUND_HTML = FIXTURES["notfound_html"]
LANDING_HTML = FIXTURES["landing_html"]
UNDISCOUNTED_HTML = FIXTURES["undiscounted_html"]
UNDISCOUNTED_EXPECTED = FIXTURES["undiscounted_expected"]

LISTING_URL = "https://www.sleepnumber.com/categories/mattresses"
SHEETS_URL = "https://www.sleepnumber.com/categories/sheets-pillowcases"
DETAIL_URL = "https://www.sleepnumber.com/products/cm-mattress"


# ===========================================================================
# harness
# ===========================================================================
def check(fn):
    """Register and run one check. Plain functions, no framework."""
    name = fn.__name__
    try:
        fn()
    except AssertionError as e:
        FAILURES.append((name, str(e)))
        print(f"FAIL  {name}\n        {e}")
    except Exception as e:  # noqa: BLE001
        FAILURES.append((name, f"{type(e).__name__}: {e}"))
        print(f"ERROR {name}\n        {type(e).__name__}: {e}")
    else:
        PASSES.append(name)
        if VERBOSE:
            print(f"ok    {name}")
    return fn


def skip(group, reason):
    SKIPS.append((group, reason))
    print(f"SKIP  {group}: {reason}")


def _engine(name):
    """Import an engine, or record a skip. Never raises."""
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, f"driver library absent ({e})")
        return None


PW = _engine("playwright_scraper")
SE = _engine("selenium_scraper")
PP = _engine("puppeteer_scraper")
ENGINES = [e for e in (PW, SE, PP) if e is not None]
ENGINE_NAMES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
ENGINE_MODS = {"playwright_scraper": PW, "selenium_scraper": SE,
               "puppeteer_scraper": PP}


def _shipped_files(suffixes):
    """Every file this repo SHIPS with the given suffixes.

    Excludes directories RELATIVE TO THE REPO ROOT rather than anywhere in
    the absolute path: a repo developed inside `.claude/worktrees/` would
    otherwise match on the absolute path and quietly turn several checks into
    no-ops that pass for the wrong reason.
    """
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] in {".git", ".claude", "captures", "live",
                                          "__pycache__", ".pytest_cache", ".venv"}:
            continue
        yield path


# ===========================================================================
# 1. the parser, against real captured values
# ===========================================================================
@check
def test_listing_parses_to_pinned_values():
    """VALUES, not coverage.

    A column can be 100% populated and entirely wrong — amazon-scraper
    shipped a review count of 445279961 on every row of every mode while its
    coverage check happily read 100%. So every field of every row is pinned
    against what the site actually published.
    """
    rows = P.parse_products(LISTING_HTML, LISTING_URL, page=1)
    assert len(rows) == len(LISTING_EXPECTED), (
        f"{len(rows)} rows, expected {len(LISTING_EXPECTED)}")
    for got, want in zip(rows, LISTING_EXPECTED):
        for field, value in want.items():
            actual = getattr(got, field)
            assert actual == value, (
                f"{got.sku}.{field}: got {actual!r}, expected {value!r}")


@check
def test_detail_parses_to_pinned_values():
    rows = P.parse_products(DETAIL_HTML, DETAIL_URL)
    assert len(rows) == len(DETAIL_EXPECTED)
    for got, want in zip(rows, DETAIL_EXPECTED):
        for field, value in want.items():
            assert getattr(got, field) == value, (
                f"{got.sku}.{field}: got {getattr(got, field)!r}, expected {value!r}")


@check
def test_a_row_is_a_variant_not_a_product():
    """Two sizes of one model are two rows with two different prices.

    This is the repo's central modelling decision and it is cheap to reverse
    by accident — a refactor that grouped by product would still produce
    plausible output.
    """
    rows = P.parse_products(LISTING_HTML, LISTING_URL, page=1)
    by_title = {}
    for row in rows:
        by_title.setdefault(row.title, []).append(row)
    multi = [rs for rs in by_title.values() if len(rs) > 1]
    assert multi, "no model has more than one size row — is this grouping by product?"
    group = multi[0]
    assert len({r.sku for r in group}) == len(group), "sizes share a sku"
    assert len({r.size for r in group}) == len(group), "sizes share a size label"
    assert len({r.price for r in group}) > 1, (
        "every size of one model has the same price — suspicious enough to pin")


@check
def test_detail_page_confirms_a_price_against_jsonld():
    """`price_source` records WHICH views agreed, and the detail fixture
    carries the site's own real JSON-LD Product so this is a real agreement
    and not a synthetic one."""
    rows = P.parse_products(DETAIL_HTML, DETAIL_URL)
    sources = {r.price_source for r in rows}
    assert "payload+jsonld" in sources, (
        f"no row was confirmed against JSON-LD; saw {sources}")
    assert "payload_jsonld_disagree" not in sources


@check
def test_no_original_price_at_or_below_its_price():
    """The one-line canary assertion, which costs nothing and catches
    the regression forever. `regular` equals `sale` on every undiscounted
    variant, so the guard that nulls it is the difference between a real
    discount column and one that reads 0% on most of the catalogue."""
    for html, url in ((LISTING_HTML, LISTING_URL), (DETAIL_HTML, DETAIL_URL),
                      (UNDISCOUNTED_HTML, SHEETS_URL)):
        for row in P.parse_products(html, url, page=1):
            if row.original_price is not None and row.price is not None:
                assert row.original_price > row.price, (
                    f"{row.sku}: original_price {row.original_price} is not "
                    f"above price {row.price}")
            if row.discount_pct is not None:
                assert row.discount_pct > 0, (
                    f"{row.sku}: discount_pct {row.discount_pct} is not positive")


@check
def test_an_undiscounted_variant_reports_no_was_price():
    """`regular` EQUALS `sale` on everything not actually on sale — 198 of
    211 variants on the sheets category. Copied across, that puts an
    `original_price` identical to the price, and a 0% discount, on most of
    the catalogue. woolworths-scraper shipped within one commit of exactly
    this on 81% of its rows.

    This fixture exists because a CONTROL found the gap: with the mattresses
    fixture alone — where 52 of 52 rows are discounted — removing the guard
    left the suite green, which is a control that proves nothing.
    """
    rows = P.parse_products(UNDISCOUNTED_HTML, SHEETS_URL, page=1)
    assert len(rows) == len(UNDISCOUNTED_EXPECTED)
    plain = [r for r in rows if r.original_price is None]
    reduced = [r for r in rows if r.original_price is not None]
    assert plain, "fixture has no undiscounted row — the guard is untested"
    assert reduced, "fixture has no discounted row — the inverse is untested"
    for row in plain:
        assert row.discount_pct is None, (
            f"{row.sku}: not on sale but reports {row.discount_pct}% off")
    for row in reduced:
        assert row.original_price > row.price
        assert row.discount_pct and row.discount_pct > 0
    for got, want in zip(rows, UNDISCOUNTED_EXPECTED):
        for field, value in want.items():
            assert getattr(got, field) == value, (
                f"{got.sku}.{field}: got {getattr(got, field)!r}, expected {value!r}")


@check
def test_discount_is_computed_not_read_from_the_badge():
    """`promo_badge` is marketing copy; `discount_pct` is arithmetic.

    Pinned because the badge is right there and reading it would look like a
    shortcut rather than a bug.
    """
    rows = [r for r in P.parse_products(LISTING_HTML, LISTING_URL, page=1)
            if r.discount_pct is not None]
    assert rows, "fixture has no discounted row to check"
    for row in rows:
        expected = round((row.original_price - row.price) / row.original_price * 100, 2)
        assert row.discount_pct == expected, (
            f"{row.sku}: discount_pct {row.discount_pct} != computed {expected}")


@check
def test_prices_are_cents_and_currency_is_stated():
    """Currency is read from `currency_iso`, which is a fact — never guessed
    from a symbol and never defaulted."""
    for row in P.parse_products(LISTING_HTML, LISTING_URL, page=1):
        assert row.currency == "USD", f"{row.sku}: currency {row.currency!r}"
        assert row.price is not None and row.price > 0
        # cents/100 can only ever have two decimals
        assert round(row.price, 2) == row.price, f"{row.sku}: {row.price}"


@check
def test_rating_and_review_count_go_null_together():
    """A 0 from an unreviewed product must not reach a consumer's average.

    Built as a payload rather than asserted against the fixture, because the
    fixture's products all have reviews — and the case that matters is the
    one the fixture does NOT contain.
    """
    product = {"name": "Unreviewed", "id": "X", "variants": [
        {"sku": "QX1", "details": {"Size": ["Queen"]}, "active": True,
         "sale": {"cents": 10000, "currency_iso": "USD"}}],
        "review_stats": {"average_overall_rating": 0, "total_review_count": 0}}
    rating, count = P._rating_of(product)
    assert rating is None, f"an unreviewed product reported rating {rating}"
    assert not count, f"an unreviewed product reported {count} reviews"
    # And the inverse: a real rating survives.
    product["review_stats"] = {"average_overall_rating": 4.5, "total_review_count": 12}
    assert P._rating_of(product) == (4.5, 12)


@check
def test_origin_host_leak_is_rewritten():
    """The site publishes `http://sndotcom.fly.dev/...` in its own JSON-LD.

    Verbatim, every row would link to a Fly application host that is not the
    shop. Pinned in BOTH directions: the leak is rewritten, and a genuine
    www URL is left alone.
    """
    leaked = "http://sndotcom.fly.dev/collections/mattresses-climate-collection/KCM10"
    fixed = P._public_url(leaked)
    assert fixed == ("https://www.sleepnumber.com/collections/"
                     "mattresses-climate-collection/KCM10"), fixed
    good = "https://www.sleepnumber.com/products/cm-mattress"
    assert P._public_url(good) == good
    # A description that merely mentions the string is not a URL and is not
    # rewritten — the replacement is on the host, not on the text.
    assert P._public_url("https://www.sleepnumber.com/p/sndotcom.fly.dev-guide") \
        == "https://www.sleepnumber.com/p/sndotcom.fly.dev-guide"
    for row in P.parse_products(LISTING_HTML, LISTING_URL, page=1):
        assert P.ORIGIN_HOST_LEAK not in (row.url or ""), row.url


@check
def test_no_row_points_at_the_origin_or_lacks_a_url():
    for html, url in ((LISTING_HTML, LISTING_URL), (DETAIL_HTML, DETAIL_URL)):
        for row in P.parse_products(html, url, page=1):
            assert row.url.startswith("https://www.sleepnumber.com/"), row.url
            assert row.sku and row.sku in row.url, (row.sku, row.url)


@check
def test_page_and_position_are_unique_across_a_multi_page_run():
    """One line, and the column is worthless without it.

    tokopedia-scraper shipped `page` as 1 on every row of a two-page run;
    `position` restarts at 1 per page, so 60 of 119 rows silently claimed a
    position another row already held.
    """
    pairs = set()
    for page_num in (1, 2, 3):
        for row in P.parse_products(LISTING_HTML, LISTING_URL, page=page_num):
            key = (row.page, row.position)
            assert key not in pairs, f"duplicate (page, position) {key}"
            pairs.add(key)
    assert len({p for p, _ in pairs}) == 3, "page was not threaded into the parser"


# ===========================================================================
# 2. turbo-stream
# ===========================================================================
@check
def test_turbo_stream_decodes_indices_and_cycles():
    """The wire format: a flat array where every value is an index into it.

    The cycle case is not hypothetical — the site's own route objects
    reference shared sub-objects, and an unbounded resolver recurses until
    the interpreter stops it.
    """
    flat = ["a", "b", {"_0": 1}]          # {"a": "b"}
    assert P.decode_turbo_stream(json.dumps(flat)) == "a"
    # root is index 0; build one whose root is the object
    flat = [{"_1": 2}, "key", "value"]
    assert P.decode_turbo_stream(json.dumps(flat)) == {"key": "value"}
    # a self-referencing object must terminate rather than recurse forever
    flat = [{"_1": 0}, "self"]
    P.decode_turbo_stream(json.dumps(flat))      # must not raise


@check
def test_turbo_stream_absent_is_not_the_same_as_empty():
    """None means "this page has no payload"; [] means "it had one and it
    held no products". Conflating them turns a CloudFront refusal into an
    empty category."""
    assert P.extract_turbo_stream("<html><body>nothing</body></html>") is None
    assert P.loader_data("<html></html>") is None
    assert P.loader_data(LISTING_HTML) is not None


@check
def test_a_malformed_chunk_does_not_lose_the_others():
    """The stream is concatenative; a later chunk can still carry the
    catalogue, so one undecodable chunk must not discard the page."""
    good = P.extract_turbo_stream(LISTING_HTML)
    doctored = LISTING_HTML.replace(
        "<body>", '<body><script>window.x.streamController.enqueue("\\q");</script>')
    got = P.extract_turbo_stream(doctored)
    assert got is not None and good in got


# ===========================================================================
# 3. JSON-LD shapes that are legal and crash a naive parser
# ===========================================================================
@check
def test_jsonld_legal_but_hostile_shapes():
    """Every one of these is valid schema.org. Three of them crashed or
    silently lost data in a sibling repo."""
    cases = {
        "offers explicitly null":
            '{"@type":"Product","sku":"A1","offers":null}',
        "offers as a list with a non-dict in it":
            '{"@type":"Product","sku":"A2","offers":["x",{"price":"5.00",'
            '"priceCurrency":"USD"}]}',
        "aggregateRating null":
            '{"@type":"Product","sku":"A3","aggregateRating":null,'
            '"offers":{"price":"1.00","priceCurrency":"USD"}}',
        "products under @graph rather than itemListElement":
            '{"@graph":[{"@type":"Product","sku":"A4",'
            '"offers":{"price":"2.00","priceCurrency":"USD"}}]}',
        "@type as a list":
            '{"@type":["Product","Thing"],"sku":"A5",'
            '"offers":{"price":"3.00","priceCurrency":"USD"}}',
        "sku only under offers":
            '{"@type":"Product","offers":{"sku":"A6","price":"4.00",'
            '"priceCurrency":"USD"}}',
        "top level is a list":
            '[{"@type":"Product","sku":"A7","offers":{"price":"6.00",'
            '"priceCurrency":"USD"}}]',
        "unparseable json":
            '{"@type":"Product", this is not json}',
    }
    for label, body in cases.items():
        html = f'<html><head><script type="application/ld+json">{body}</script></head></html>'
        try:
            prices = P.jsonld_prices(html)
        except Exception as e:            # noqa: BLE001
            raise AssertionError(f"{label}: raised {type(e).__name__}: {e}")
        if label == "unparseable json":
            assert prices == {}, prices
        elif label == "offers explicitly null":
            assert prices.get("A1") == (None, None), prices
        else:
            assert prices, f"{label}: produced nothing"
    # and the values are actually read, not merely survived
    got = P.jsonld_prices(
        '<script type="application/ld+json">'
        '{"@type":"Product","sku":"B1","offers":{"price":"1709.10",'
        '"priceCurrency":"USD"}}</script>')
    assert got == {"B1": (1709.10, "USD")}, got


@check
def test_itemlist_entries_are_read():
    html = ('<script type="application/ld+json">'
            '{"@type":"ItemList","itemListElement":['
            '{"@type":"ListItem","position":1,"item":{"@type":"Product",'
            '"sku":"KCM10","offers":{"price":"1709.10","priceCurrency":"USD"}}}]}'
            '</script>')
    assert P.jsonld_prices(html) == {"KCM10": (1709.10, "USD")}


# ===========================================================================
# 4. block detection — the counts, pinned
# ===========================================================================
# The real CloudFront refusal, verbatim as an HTTP client receives it. 919
# bytes on the wire; the boilerplate is trimmed here but every marker-bearing
# line is byte-for-byte what the edge sent.
CLOUDFRONT_403 = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" '
    '"http://www.w3.org/TR/html4/loose.dtd">\n'
    '<HTML><HEAD><META HTTP-EQUIV="Content-Type" CONTENT="text/html; '
    'charset=iso-8859-1">\n<TITLE>ERROR: The request could not be satisfied</TITLE>\n'
    '</HEAD><BODY>\n<H1>403 ERROR</H1>\n<H2>The request could not be satisfied.</H2>\n'
    '<HR noshade size="1px">\nRequest blocked.\nWe can\'t connect to the server for '
    'this app or website at this time.\n<BR clear="all">\n<HR noshade size="1px">\n'
    '<PRE>\nGenerated by cloudfront (CloudFront)\n'
    'Request ID: luzGWV_pKUblXiucGzGBVujjh79Sox9aX1vtiPSVJAVqV2KoSalsGg==\n'
    '</PRE>\n<ADDRESS>\n</ADDRESS>\n</BODY></HTML>')

# The SAME refusal after a browser has parsed and re-serialised it. A
# marker must survive both encodings, and a literal that matches one can
# silently miss the other.
CLOUDFRONT_403_DOM = CLOUDFRONT_403.replace("<HTML>", "<html>").replace(
    "</HTML>", "</html>").replace("<TITLE>", "<title>").replace(
    "</TITLE>", "</title>").replace("&#58;", ":").replace("&#47;", "/")


@check
def test_a_refusal_is_blocked_in_both_encodings():
    for label, html in (("http-client bytes", CLOUDFRONT_403),
                        ("browser DOM", CLOUDFRONT_403_DOM)):
        state = P.detect_page_state(html, 403, "https://www.sleepnumber.com/")
        assert state == "blocked", f"{label}: classified as {state}"


@check
def test_headers_alone_can_settle_it():
    """The one signal a body cannot forge.

    A served page answers `x-cache: Miss from cloudfront` with `server:
    Fly/…`; a refusal answers `Error from cloudfront`. And if
    `x-amzn-waf-action` ever appears, the state is a solvable CHALLENGE
    rather than a dead end — a different answer, which is why this looks for
    it rather than assuming it never comes.
    """
    assert P.detect_page_state("", 403, "", {"x-cache": "Error from cloudfront"}) \
        == "blocked"
    assert P.detect_page_state("", 405, "", {"x-amzn-waf-action": "captcha"}) \
        == "captcha"
    assert P.detect_page_state(LISTING_HTML, 200, LISTING_URL,
                               {"x-cache": "Miss from cloudfront"}) == "content"


@check
def test_markers_absent_from_a_good_page():
    """Count a marker on a known-good page first — the reason this repo's
    marker list is two entries long.

    A marker that matches a page the site actually served is worse than no
    marker at all. `recaptcha` and `captcha` are the live trap here: the base
    template ships a reCAPTCHA loader, so each occurs on every page, good and
    bad alike.
    """
    for html, label in ((LISTING_HTML, "listing"), (DETAIL_HTML, "detail")):
        lowered = html.lower()
        for marker in P.BOT_CHALLENGE_MARKERS:
            assert marker not in lowered, (
                f"block marker {marker!r} occurs on a page the site SERVED "
                f"({label}) — it is a fact about the site, not a marker")
    for banned in ("recaptcha", "captcha", "akamai", "datadome", "perimeterx",
                   "cf-turnstile", "challenges.cloudflare.com", "incapsula",
                   "awswaf", "errors.edgesuite.net", "hcaptcha"):
        assert banned not in [m.lower() for m in P.BOT_CHALLENGE_MARKERS], (
            f"{banned!r} is in BOT_CHALLENGE_MARKERS. Count it on a served "
            f"page before adding it back: recaptcha/captcha fire on every "
            f"page here, and the rest were measured at zero everywhere, "
            f"which makes them dead code.")


@check
def test_the_positive_asset_signal_discriminates():
    """A served page is BUILT OUT OF the site's own assets; an interstitial
    is not. This is what catches a refusal whose wording changes, and
    Chromium's own network-error page, which carries the site's hostname in
    its <title> and would pass any title check."""
    for html in (LISTING_HTML, DETAIL_HTML):
        assert any(m in html.lower() for m in P.SITE_ASSET_MARKERS)
    for html in (CLOUDFRONT_403, CLOUDFRONT_403_DOM):
        assert not any(m in html.lower() for m in P.SITE_ASSET_MARKERS)


@check
def test_chromium_network_error_page_is_not_content():
    """Not an interstitial — the BROWSER's own error page. It carries the
    site's hostname in its title, so a title check calls it a real page
    (seen on a sibling site)."""
    html = ("<html><head><title>www.sleepnumber.com</title></head><body>"
            "<div id='main-message'>This site can&rsquo;t be reached</div>"
            "<div class='error-code'>ERR_PROXY_CONNECTION_FAILED</div>"
            "</body></html>")
    assert P.detect_page_state(html, None, "https://www.sleepnumber.com/") != "content"


@check
def test_the_sites_own_404_is_not_a_block():
    """Against the page the site REALLY serves, not one written here.

    The first version of this check invented a page with `<title>Not
    Found</title>` and `page not found` in the body, and passed. Neither
    string is on the real thing: `/products/i8` comes back with
    `<title>Sleep Number</title>`, so the marker list matched nothing and a
    404 was recognised only through its status code — a capture handed to the
    parser without one came out as `empty`, a claim about the catalogue.

    A guard is only as good as the fixture it runs against, and the
    fixture that matters is the one fetched the way a real run fetches.
    """
    # WITHOUT a status code, which is the case the invented fixture hid.
    assert P.detect_page_state(NOTFOUND_HTML, None,
                               "https://www.sleepnumber.com/products/i8") == "notfound"
    # And with one.
    assert P.detect_page_state(NOTFOUND_HTML, 404,
                               "https://www.sleepnumber.com/products/i8") == "notfound"
    assert page_flow.should_retry("notfound") is False
    assert page_flow.counts_as_blocked("notfound") is False
    # The structural signal that carries it: React Router puts one key per
    # matched route into loaderData, and a URL that matched none has only
    # `root`.
    data = P.loader_data(NOTFOUND_HTML)
    assert data is not None and not [k for k in data if k != "root"]


@check
def test_a_landing_page_is_not_an_empty_category():
    """Some /categories/... URLs are curated landing pages, not listings.

    `/categories/beds-on-sale` renders 320 KB with `category.name = "Sale"`
    and NO `products` key at all — no `total_results`, no `slug`. Reported as
    "zero products" that reads as a claim about the catalogue and sends the
    reader to check the shelf instead of the URL.

    The distinction is exact in the payload: `products` ABSENT is a landing
    page, present-and-empty is an empty category.
    """
    assert P.detect_page_state(LANDING_HTML, 200,
                               "https://www.sleepnumber.com/categories/beds-on-sale") \
        == "no_listing"
    assert P.parse_products(LANDING_HTML,
                            "https://www.sleepnumber.com/categories/beds-on-sale") == []
    assert page_flow.should_retry("no_listing") is False
    assert page_flow.counts_as_blocked("no_listing") is False
    # present-and-empty must NOT be confused with it
    import copy
    data = P.loader_data(LANDING_HTML)
    route = next(k for k in data if k.startswith("routes/categories"))
    empty = copy.deepcopy(data)
    empty[route]["category"]["products"] = []
    assert P._is_landing_page(data) is True
    assert P._is_landing_page(empty) is False


# ===========================================================================
# 5. pagination — the ?page=N trap
# ===========================================================================
@check
def test_page_param_is_not_trusted():
    """`?page=2` and `?page=99` both return page ONE on this site, HTTP 200,
    with the payload's own `page` still reading 1.

    So `pagination_is_addressable` must answer False, `plan_pages` must plan
    nothing beyond what the site itself offers, and `--concurrency` must be
    refused rather than silently ignored.
    """
    assert page_flow.pagination_is_addressable("listing", LISTING_HTML, LISTING_URL) is False
    assert page_flow.plan_pages(LISTING_HTML, LISTING_URL, 5) == []
    assert tuple(page_flow.CONCURRENCY_CAPABLE_MODES) == ()


@check
def test_page_url_still_builds_the_convention():
    """Kept, and known not to work on its own — it is what
    `pagination_is_addressable` compares the site's own next-link against.
    Deleting it would remove the comparison, not the problem."""
    assert P.page_url(LISTING_URL, 2) == LISTING_URL + "?page=2"
    assert P.page_url(LISTING_URL, 1) == LISTING_URL
    # existing query parameters are preserved, and `page` is REPLACED rather
    # than duplicated
    assert P.page_url(LISTING_URL + "?sort=price&page=4", 2) \
        == LISTING_URL + "?sort=price&page=2"


@check
def test_a_parser_shortfall_is_measured_against_the_site_own_count():
    """The site counts PRODUCTS; a row here is a size VARIANT.

    So the comparison is on distinct `product_id`, and getting that wrong
    would make a healthy 52-row run look like a 45-product shortfall against
    a stated total of 7. Pinned in both directions.

    This replaces a coverage threshold an audit asked for on 2026-10-13. A
    threshold is a guess about how much loss is acceptable; `total_results`
    is the response itself proving the loss.
    """
    rows = P.parse_products(LISTING_HTML, LISTING_URL, page=1)
    ids = [r.product_id for r in rows]
    # the fixture holds fewer products than the real category, so its own
    # stated total is what must be matched — never the live one
    bits = page_flow.pagination(LISTING_HTML)
    total = bits.get("total_results")
    assert isinstance(total, int), "the listing fixture states no total_results"

    # a complete read of the fixture's own products reports no shortfall
    complete = page_flow.products_shortfall(LISTING_HTML, ids)
    assert complete == 0 or complete is None, (
        f"a full parse of the fixture reports {complete} missing against "
        f"total_results={total}; the comparison is probably counting rows "
        f"instead of distinct products")

    # losing one product is reported as exactly one missing
    kept = sorted({i for i in ids if i})
    assert len(kept) >= 2, "fixture has too few products to drop one"
    short = page_flow.products_shortfall(LISTING_HTML, kept[:-1])
    assert short == complete + 1, (
        f"dropping one product changed the shortfall from {complete} to "
        f"{short}; it should have gone up by exactly one")

    # a page that states no total cannot be contradicted
    assert page_flow.products_shortfall(LANDING_HTML, []) is None, (
        "a landing page states no total_results, so there is no claim to "
        "fall short of — a shortfall there would be invented")

    # THE DISCRIMINATING CASE, and the fixture cannot provide it.
    #
    # A row is a size VARIANT, so a listing normally has far more rows than
    # products — 52 rows for 7 products on the real mattresses category. If
    # this counted rows instead of distinct products the subtraction would
    # clamp at zero on every healthy page and the check would NEVER FIRE,
    # which is the failure that matters and the one a planted fault against
    # the fixture alone stayed green on (treat a green control as a hole
    # in the suite, not as a pass).
    #
    # So state it directly: three products claimed, three ROWS parsed, but
    # all three rows belong to ONE product. Two are missing.
    stub_html = LISTING_HTML
    stated = page_flow.pagination(stub_html).get("total_results")
    assert isinstance(stated, int) and stated >= 1
    # one product id repeated `stated + 2` times: more rows than the total,
    # and fewer distinct products than it.
    repeated = ["ONLY-ONE-PRODUCT"] * (stated + 2)
    got = page_flow.products_shortfall(stub_html, repeated)
    assert got == stated - 1, (
        f"with {stated} products claimed and {len(repeated)} rows all from "
        f"ONE product, the shortfall should be {stated - 1}; got {got}. "
        f"That is the signature of counting rows instead of distinct "
        f"products, which makes this check silently never fire.")


@check
def test_parser_incomplete_is_not_a_complete_stop_reason():
    """It must reach the caller as `partial`, not `complete`.

    Verified live too: a parser made to drop one product returned exit 6
    with `stop_reason: parser_incomplete` and `products_missing: 1`.
    """
    assert "parser_incomplete" not in output_writer.COMPLETE_STOP_REASONS
    for name in ENGINE_NAMES + ("http_scraper",):
        text = (ROOT / f"{name}.py").read_text()
        assert "products_shortfall" in text, (
            f"{name} never asks whether it parsed what the site counted")
        assert 'stop_reason = "parser_incomplete"' in text, name


@check
def test_the_site_states_its_own_arithmetic():
    bits = page_flow.listing_arithmetic(LISTING_HTML)
    assert bits["per_page"] == 100, bits
    assert isinstance(bits["total_results"], int)
    assert bits["pages_available"] == 1, bits
    assert bits["page_param_honoured"] is False


@check
def test_a_size_filtered_listing_url_is_the_same_request():
    """`/categories/mattresses/king` returns the SAME payload as
    `/categories/mattresses`. The size segment is a client-side facet, so the
    two are not different samples and must not be reported as such."""
    ok, why = P.is_supported_url("https://www.sleepnumber.com/categories/mattresses/king")
    assert ok, why
    assert P.category_from_url(
        "https://www.sleepnumber.com/categories/mattresses/king") == "mattresses"


# ===========================================================================
# 6. state policy
# ===========================================================================
@check
def test_state_policy_is_total_and_consistent():
    """Every state `detect_page_state` can return must have a policy, or an
    engine falls through to a default nobody chose."""
    produced = {"content", "blocked", "notfound", "empty", "unknown", "captcha",
                "no_listing"}
    for state in produced:
        assert state in page_flow.STATE_POLICY, f"no policy for {state!r}"
    for state, policy in page_flow.STATE_POLICY.items():
        assert set(policy) == {"retry", "solve", "blocked"}, state
    # A block on this site carries no widget, so it is retryable (a different
    # exit may work) but NOT solvable (there is nothing to solve).
    assert page_flow.should_retry("blocked") is True
    assert page_flow.should_solve("blocked") is False
    assert page_flow.counts_as_blocked("blocked") is True
    assert page_flow.should_solve("captcha") is True
    assert page_flow.should_retry("content") is False


@check
def test_a_served_page_that_parses_to_nothing_is_our_bug():
    """"0 products" sends the reader to check the URL; "the parser failed"
    sends them to the parser. They are different facts."""
    html = ('<html><body><a href="/collections/mattresses-climate-collection/QT8">x</a>'
            '</body></html>')
    assert page_flow.is_broken_parse("content", html, 0) is True
    assert page_flow.is_broken_parse("content", html, 5) is False
    assert page_flow.is_broken_parse("empty", html, 0) is False
    # A genuinely empty category links to no product and is NOT this.
    assert page_flow.is_broken_parse("content", "<html><body>none</body></html>", 0) is False


@check
def test_classify_takes_status_as_a_keyword():
    """A sibling shipped `classify(html, status, url)` positionally
    while two engines called it `classify(html, url=…)`, and both crashed on
    their FIRST fetch. Keyword-only makes that impossible to do silently."""
    sig = inspect.signature(page_flow.classify)
    assert sig.parameters["status"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["url"].kind is inspect.Parameter.KEYWORD_ONLY


# ===========================================================================
# 7. the URL-pattern fallback
# ===========================================================================
@check
def test_fallback_emits_identifiable_rows_without_inventing_prices():
    """A page whose payload will not decode still yields rows that can be
    followed — but they must NOT carry a price, because this site publishes
    none in markup. A fabricated price is worse than a null one."""
    html = ('<html><head><link rel="preconnect" href="https://cdn.sleepnumber.com">'
            '</head><body>'
            '<a href="/collections/mattresses-climate-collection/QT8">Queen</a>'
            '<a href="/collections/mattresses-climate-collection/KT8">King</a>'
            '<a href="/collections/mattresses-climate-collection/QT8">dup</a>'
            '</body></html>')
    rows = P.parse_products(html, LISTING_URL, page=1)
    assert [r.sku for r in rows] == ["QT8", "KT8"], [r.sku for r in rows]
    for row in rows:
        assert row.price is None and row.currency is None
        assert row.price_source == "url-fallback"
        assert row.url.startswith("https://www.sleepnumber.com/collections/")


# ===========================================================================
# 8. output contract
# ===========================================================================
@check
def test_exit_codes_are_the_family_contract():
    assert output_writer.EXIT_BLOCKED == 3
    assert output_writer.EXIT_NO_PRODUCTS == 4
    assert output_writer.EXIT_API_ERROR == 5
    assert output_writer.EXIT_REMOTE_API_ERROR == 5
    assert output_writer.EXIT_PARTIAL == 6


@check
def test_the_family_prefix_is_in_order():
    """A consumer reading several repos in this family reads the same opening
    columns. Site-specific fields go at the END."""
    names = [f.name for f in __import__("dataclasses").fields(Product)]
    assert names[:5] == ["source", "scraped_at", "url", "sku", "title"], names[:5]
    for field in ("price", "currency", "price_source", "category"):
        assert field in names, field
    assert names.index("size") > names.index("category"), (
        "site-specific columns must follow the family prefix")


@check
def test_a_run_that_finds_nothing_writes_nothing():
    """Never replace last night's good output with []. A consumer cannot tell
    an empty category from a failed run, and the failure destroys the last
    known good data."""
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w") as f:
            f.write('[{"sku": "PREVIOUS"}]')
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            code = output_writer.finish_run([], prefix, "json", False,
                                            blocked=False, stop_reason="completed",
                                            pages_requested=1, pages_completed=1,
                                            pages_failed=[], mode="listing",
                                            start_url="https://www.sleepnumber.com/",
                                            final_url="https://www.sleepnumber.com/")
        assert code == output_writer.EXIT_NO_PRODUCTS, code
        assert json.load(open(prefix + ".json")) == [{"sku": "PREVIOUS"}], (
            "an empty run overwrote the previous good output")
        assert not os.path.exists(prefix + ".meta.json"), (
            "a failed run wrote a sidecar beside good data it did not produce")


@check
def test_an_empty_csv_still_carries_its_header():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "empty.csv")
        output_writer.write_csv([], path, Product)
        head = open(path).readline().strip()
        assert head.startswith("source,scraped_at,url,sku,title"), head


@check
def test_dedupe_is_by_sku_and_keeps_page_order():
    rows = [Product(sku="A", position=1), Product(sku="B", position=2),
            Product(sku="A", position=3), Product(sku="C", position=4)]
    seen = set()
    kept = output_writer.dedupe_by_key(rows, seen, "sku")
    assert [r.sku for r in kept] == ["A", "B", "C"], [r.sku for r in kept]


# ===========================================================================
# 9. structural checks — the ones that found real bugs across this family
# ===========================================================================
_PY_FILES = sorted(p for p in _shipped_files({".py"}))


@check
def test_no_unreachable_statement_after_a_terminator():
    """A statement after return/raise/break/continue in the SAME block.

    Found the identical fifteen lines in SIX repos of this family, byte for
    byte, present since each one's first commit: a function whose `def` line
    had been lost, leaving its body absorbed into the end of the function
    above it. It parses, it imports, `--help` works, `compileall` passes, and
    the undefined-name walk below cannot see it — that walk pools bindings
    per file rather than tracking scopes, which is right for what IT is for.
    This is a second check, not a tightening of the first.
    """
    terminators = (ast.Return, ast.Raise, ast.Break, ast.Continue)
    hits, scanned = [], 0
    for path in _PY_FILES:
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, terminators):
                        hits.append(f"{path.name}:{block[i + 1].lineno} "
                                    f"(after {type(stmt).__name__.lower()} "
                                    f"on line {stmt.lineno})")
                        break
    assert scanned, "scanned nothing — this check would pass for the wrong reason"
    assert not hits, "unreachable statement(s): " + "; ".join(hits)


@check
def test_no_undefined_names():
    """`compileall` proves a file PARSES, not that its names RESOLVE.

    A live run of a sibling's pyppeteer engine died with NameError on a line
    reached only while fetching, after an import was removed. The module
    imported cleanly, --help worked, compileall passed and CI was green.

    Kept deliberately COARSE — bindings are pooled per file rather than
    tracked per scope — so it under-reports rather than inventing problems.
    """
    import builtins
    hits, scanned = [], 0
    for path in _PY_FILES:
        scanned += 1
        tree = ast.parse(path.read_text(), filename=str(path))
        bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
                args = getattr(node, "args", None)
                if args:
                    for a in (list(args.args) + list(args.posonlyargs)
                              + list(args.kwonlyargs)
                              + [args.vararg, args.kwarg]):
                        if a is not None:
                            bound.add(a.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.ExceptHandler,)) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Lambda):
                for a in (list(node.args.args) + list(node.args.kwonlyargs)
                          + [node.args.vararg, node.args.kwarg]):
                    if a is not None:
                        bound.add(a.arg)
            elif isinstance(node, (ast.comprehension,)):
                for n in ast.walk(node.target):
                    if isinstance(n, ast.Name):
                        bound.add(n.id)
            elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
                bound.update(node.names)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                for n in ast.walk(node.optional_vars):
                    if isinstance(n, ast.Name):
                        bound.add(n.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in bound:
                    hits.append(f"{path.name}:{node.lineno} {node.id}")
    assert scanned, "scanned nothing"
    assert not hits, "name(s) never imported, defined or assigned: " + "; ".join(sorted(set(hits))[:20])


@check
def test_every_shared_call_binds_against_the_real_signature():
    """Bind every shared call against the real signature, with a correction.

    It earns its keep: a sibling shipped `classify(html, status, url)` while
    two of three engines called it `classify(html, url=…)`, and BOTH crashed
    on their first fetch — invisible to import, --help, compileall, the
    undefined-name walk above and four hundred green assertions, because none
    of those calls a function the way a live run does.

    The correction is the important part: resolving the callee with
    `getattr(owner, attr, None)` and skipping anything not callable means a
    name the shared module DOES NOT DEFINE comes back None, is not callable,
    and is silently skipped — the single loudest thing this check could say
    is the one case it stays quiet about. A missing attribute is a FAILURE
    here, not a skip.

    And the one rule it needs: a name bound anywhere in the calling file
    SHADOWS a same-named module. An engine takes `pool` and `page_flow`-like
    objects as parameters, so without this the check reports false positives
    on every method call.
    """
    owners = {"page_flow": page_flow, "product_parser": P,
              "output_writer": output_writer, "proxy_pool": proxy_pool,
              "env_config": env_config}
    checked, problems = 0, []
    for path in _PY_FILES:
        if path.name in ("smoke_test.py",) or path.parent.name == "tools":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        # names bound in this file shadow a module of the same name
        shadowed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                shadowed.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                for arg in (list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs)):
                    shadowed.add(arg.arg)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            base = node.func.value
            if not isinstance(base, ast.Name) or base.id not in owners:
                continue
            if base.id in shadowed:
                continue
            owner, attr = owners[base.id], node.func.attr
            if not hasattr(owner, attr):
                problems.append(f"{path.name}:{node.lineno} {base.id}.{attr} "
                                f"DOES NOT EXIST in {base.id}")
                continue
            fn = getattr(owner, attr)
            if not callable(fn):
                continue
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            pos = [object()] * len(node.args)
            kw = {k.arg: object() for k in node.keywords if k.arg}
            if any(k.arg is None for k in node.keywords):
                continue                      # **kwargs splat — cannot bind
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*pos, **kw)
            except TypeError as e:
                problems.append(f"{path.name}:{node.lineno} {base.id}.{attr}(): {e}")
            checked += 1
    assert checked, "bound nothing — this check would pass for the wrong reason"
    assert not problems, "shared-module call(s) that cannot bind:\n  " + "\n  ".join(problems)


@check
def test_every_policy_constant_has_a_consumer():
    """A policy constant nothing reads is the same defect as dead code,
    and harder to see, because the prose beside it reads like enforcement.

    A sibling's `RETRY_ON_BLOCKED` carried a paragraph of measured
    justification and no engine consulted it — setting it False changed
    nothing at all.
    """
    sources = {p.name: p.read_text() for p in _PY_FILES}
    constants = [n for n in dir(page_flow)
                 if n.isupper() and not n.startswith("_")]
    assert constants, "found no policy constants to check"
    own = sources["page_flow.py"]
    orphans = []
    for name in constants:
        used_elsewhere = any(name in text for f, text in sources.items()
                             if f != "page_flow.py")
        # Or consumed by this module's own accessor — READY_SELECTORS is read
        # by ready_selector(), which the engines DO call. That is a real
        # consumer; it just is not a consumer by NAME. Counting occurrences
        # inside the module distinguishes "read by our own API" from "written
        # once and never mentioned again", which is the defect being hunted.
        used_internally = own.count(name) > 1
        if not (used_elsewhere or used_internally):
            orphans.append(name)
    assert not orphans, (
        f"page_flow constant(s) with no consumer outside their own module: "
        f"{orphans}. Either something should read them, or they are prose.")


# ===========================================================================
# 10. wording, config and packaging
# ===========================================================================
@check
def test_banned_wording_absent():
    """The phrases are ASSEMBLED from pieces rather than written out, so
    this check can scan smoke_test.py ITSELF.

    Three repos in this family exempted their own suite file wholesale — the
    file most likely to acquire a stray phrase, or a pasted credential, was
    the one file nobody scanned.
    """
    ad = "anti" + "detect"
    banned = (" ".join(["cloud", "browser"]),
              f"{ad} browser",
              "gate.2prx" + ".com",
              f"--{ad}",
              f"{ad.upper()}_LOCAL_API")
    shipped = list(_shipped_files({".py", ".md", ".yml", ".yaml", ".txt", ".toml"}))
    assert shipped, "scanned nothing"
    for path in shipped:
        text = path.read_text(errors="replace").lower()
        for phrase in banned:
            assert phrase not in text, (
                f"{path.relative_to(ROOT)} contains a banned phrase. Write "
                f"'Scraping Browser API' instead.")


@check
def test_the_product_is_named_correctly():
    readme = (ROOT / "README.md")
    if not readme.exists():
        return
    text = readme.read_text(errors="replace")
    assert "Scraping Browser API" in text, (
        "the README never names the Scraping Browser API")


@check
def test_env_example_documents_exactly_what_the_code_reads():
    """Equal in BOTH directions. A documented-but-unread variable is worse
    than an undocumented one: it looks configurable and is inert."""
    example = (ROOT / ".env.example").read_text()
    # A variable shipped COMMENTED OUT (`# SLEEPNUMBER_URL=…`) is still
    # documented: it is commented so that a copied example sets nothing.
    documented = set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]*)=", example, re.M))
    read = set(env_config.ENV_KEYS)
    assert documented == read, (
        f"documented but never read: {sorted(documented - read)}; "
        f"read but never documented: {sorted(read - documented)}")


@check
def test_a_copied_env_example_reads_as_unset():
    """`cp .env.example .env` then a run must NOT send `{login}-zone-…`
    to the API as if it were a credential.

    The two credentialled URLs are documented the way the vendor documents
    them, so a literal placeholder list never matched them — which produced a
    401 a long way from its cause. Any `{…}` left in a value is unset.
    """
    example = (ROOT / ".env.example").read_text()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w") as f:
            f.write(example)
        saved = {k: os.environ.pop(k, None) for k in env_config.ENV_KEYS}
        try:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                env_config.load_env(path)
                values = {k: env_config.env_value(k) for k in env_config.ENV_KEYS}
            for name, value in values.items():
                assert value is None, (
                    f"{name} read as {value!r} from an untouched .env.example "
                    f"— a placeholder would be sent to the API as a credential")
            assert "{" not in buf.getvalue() or "password" not in buf.getvalue().lower(), (
                "the placeholder warning quoted the value; name the "
                "placeholder instead — the value can be a credentialled URL")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


@check
def test_env_keys_are_not_mapped_onto_flags_with_defaults():
    """The loader only fills UNSET values, so a variable mapped onto a
    flag with a non-empty default is silently inert — a setting that looks
    configurable and is not."""
    assert "SLEEPNUMBER_OUT" not in env_config.ENV_KEYS, (
        "--out has a non-empty default; a variable mapped onto it can never "
        "take effect")


@check
def test_no_credentials_committed():
    """The repo's own scan, invoked from here as well as from CI — one
    implementation, two callers.

    This family shipped TWO sources of truth for this, one dead (a
    `ci_checks.py` nothing ran) and one holed (an inline grep matching only
    ws:// and wss://, so an `http://user:pass@` credential sailed past).
    """
    script = ROOT / ".github" / "ci_checks.py"
    if not script.exists():
        # The Docker image deliberately COPYs no .github/ — see the
        # Dockerfile. Trigger on the WHOLE directory being absent, never on a
        # file inside it going missing, or this check quietly starts passing
        # the moment its input disappears.
        assert not (ROOT / ".github").exists(), (
            ".github/ exists but ci_checks.py is missing")
        return
    r = subprocess.run([sys.executable, str(script), "--secret-check"],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"credential scan failed:\n{r.stdout}\n{r.stderr}"


@check
def test_ci_calls_the_script_rather_than_reimplementing_it():
    """Two sources of truth, one dead and one holed. The workflow must
    INVOKE ci_checks.py, not grep for credentials itself."""
    wf = ROOT / ".github" / "workflows" / "tests.yml"
    if not wf.exists():
        assert not (ROOT / ".github").exists(), "workflows/ is missing"
        return
    text = wf.read_text()
    assert "ci_checks.py" in text, (
        "tests.yml does not invoke .github/ci_checks.py — if it greps for "
        "credentials inline instead, the two will disagree")


@check
def test_dockerfile_copies_every_module_the_entrypoint_imports():
    """Three repos in this family shipped an image that died with
    ModuleNotFoundError on EVERY invocation, `--help` included, because one
    module the engine imports at module level was missing from the COPY list.
    Nothing built the image, so nothing noticed. This needs no Docker."""
    dockerfile = ROOT / "Dockerfile"
    if not dockerfile.exists():
        return
    # Join backslash continuations FIRST. Read line by line, a COPY whose
    # file list wraps — which every readable one does — loses everything
    # after the first line, and the check then reports the modules on the
    # continuation as missing. That is the "what happens when it resolves nothing" question asked of this check
    # itself: when it parses something before testing it, what does it do
    # when the parse comes up short? Here it cried wolf; the same blind spot
    # would have let a genuinely missing module through had the COPY been
    # split the other way.
    text = dockerfile.read_text().replace("\\\n", " ")
    copied = set()
    for line in text.splitlines():
        if line.strip().upper().startswith("COPY"):
            for token in line.split()[1:]:
                if token.endswith(".py"):
                    copied.add(token)
    assert copied, "parsed no .py out of the Dockerfile's COPY lines at all"
    # the entrypoint's transitive first-party imports
    entry = "playwright_scraper"
    local = {p.stem for p in ROOT.glob("*.py")}
    need, stack = set(), [entry]
    while stack:
        mod = stack.pop()
        if mod in need:
            continue
        need.add(mod)
        src = (ROOT / f"{mod}.py")
        if not src.exists():
            continue
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in local:
                        stack.append(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module in local and node.level == 0:
                stack.append(node.module)
    missing = {f"{m}.py" for m in need} - copied
    assert not missing, (
        f"Dockerfile COPY list is missing {sorted(missing)} — the image would "
        f"die with ModuleNotFoundError on every invocation, --help included")


@check
def test_sample_output_matches_the_row_schema():
    from dataclasses import fields
    names = [f.name for f in fields(Product)]
    for name, loader in (("sample_output.json", "json"), ("sample_output.csv", "csv")):
        path = ROOT / name
        if not path.exists():
            continue
        if loader == "json":
            rows = json.loads(path.read_text())
            assert rows, f"{name} is empty"
            assert list(rows[0].keys()) == names, (
                f"{name} columns drifted from Product")
        else:
            header = path.read_text().splitlines()[0].split(",")
            assert header == names, f"{name} header drifted from Product"


# ===========================================================================
# 11. the engines — flags, imports, agreement
# ===========================================================================
# Flags every engine must take: the contract plus the five this family's own
# list omitted for months while nearly every repo shipped them.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless",
    "--headful", "--fingerprint", "--fp-country", "--fp-tags", "--mode",
}

# Flags the README documents as belonging to ONE engine. The exception list
# IS the documentation: the lesson is that this check must fail in BOTH
# directions, so closing a documented difference is a failure too.
ENGINE_ONLY_FLAGS = {
    # Nothing. Every flag the primary engine takes, its twins take too.
    "playwright_scraper": set(),
    # Chromium sandbox switches, which only chromedriver needs spelled out:
    # Playwright and pyppeteer pass their own equivalents internally.
    "selenium_scraper": {"--no-sandbox", "--disable-dev-shm-usage"},
    # pyppeteer downloads its own Chromium unless pointed at one.
    "puppeteer_scraper": {"--chromium-path"},
}


def _flags_of(name):
    text = (ROOT / f"{name}.py").read_text()
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', text))


@check
def test_every_engine_takes_the_contract_flags():
    for name in ENGINE_NAMES:
        flags = _flags_of(name)
        missing = CONTRACT_FLAGS - flags
        assert not missing, f"{name} is missing {sorted(missing)}"


@check
def test_engine_flag_sets_agree_in_both_directions():
    """Written only after it was skipped once and then found TWELVE
    flags the primary engine had and its twins did not — nine of them
    predating the work, while the README promised "same CLI".

    Both directions: a new unshared flag fails, and so does CLOSING a
    documented difference, because the exception list is the documentation.
    """
    sets = {name: _flags_of(name) for name in ENGINE_NAMES}
    for name, flags in sets.items():
        others = set().union(*(f for n, f in sets.items() if n != name))
        unshared = flags - others
        documented = ENGINE_ONLY_FLAGS[name]
        assert unshared == documented, (
            f"{name}: flags only it has are {sorted(unshared)}, but the "
            f"documented exceptions are {sorted(documented)}. Either add the "
            f"flag to the other engines, or document it here.")


@check
def test_removed_flags_stay_removed():
    """Scoped to the ENGINES: `--country` is banned on a scraper (it could
    disagree with the URL) and legitimate on fingerprint_client.py, where it
    picks a fingerprint locale."""
    for name in ENGINE_NAMES:
        flags = _flags_of(name)
        for gone in ("--country", "--" + "anti" + "detect", "--page-size",
                     "--search-term", "--club-id", "--player-id"):
            assert gone not in flags, f"{name} reintroduced {gone}"


@check
def test_engines_import_their_driver_at_module_level():
    """For "the suite passes with no engine installed" to MEAN anything,
    each engine must import its driver at MODULE level.

    A sibling imported `launch`/`connect` inside the launch path, so the
    module imported cleanly with no pyppeteer present: the group never
    skipped, and the CI job that exists to fail on unexpected skips could not
    have caught a broken import. It also let CI run against a stub version
    for a while with nothing noticing.
    """
    expect = {"playwright_scraper": "playwright",
              "selenium_scraper": "selenium",
              "puppeteer_scraper": "pyppeteer"}
    for name, driver in expect.items():
        tree = ast.parse((ROOT / f"{name}.py").read_text())
        found = False
        for node in tree.body:                   # MODULE level only
            if isinstance(node, ast.Import):
                found |= any(a.name.split(".")[0] == driver for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                found |= (node.module or "").split(".")[0] == driver
        assert found, (
            f"{name} does not import {driver} at module level — the suite "
            f"would not skip when the driver is absent, so a broken import "
            f"would read as a pass")


@check
def test_all_three_engines_share_the_mode_vocabulary():
    for name in ENGINE_NAMES:
        text = (ROOT / f"{name}.py").read_text()
        assert "page_flow.MODES" in text, (
            f"{name} spells its own mode list instead of reading "
            f"page_flow.MODES — that is how two engines come to disagree")


@check
def test_engines_agree_on_what_a_row_is():
    """The three `_parse_for_mode` bodies must call the same parser the same
    way. This is the function that decides what a row IS."""
    if not ENGINES:
        return
    bodies = {}
    for mod in ENGINES:
        src = inspect.getsource(mod._parse_for_mode)
        bodies[mod.__name__] = re.sub(r"\s+", " ", src.split('"""')[-1]).strip()
    assert len(set(bodies.values())) == 1, (
        "engines parse differently:\n" + "\n".join(f"  {k}: {v}" for k, v in bodies.items()))


@check
def test_engines_produce_the_same_rows_from_the_same_html():
    """Not that they call the same function — that they RETURN the same rows.
    The strongest form of the family's "three engines must agree"."""
    if len(ENGINES) < 2:
        return

    class _Args:
        mode = "listing"

    outputs = {}
    for mod in ENGINES:
        rows = mod._parse_for_mode(LISTING_HTML, LISTING_URL, _Args(), 1)
        outputs[mod.__name__] = [(r.sku, r.price, r.currency, r.size) for r in rows]
    first = next(iter(outputs.values()))
    for name, rows in outputs.items():
        assert rows == first, f"{name} disagrees with the other engine(s)"


@check
def test_all_engines_describe_a_landing_page_identically():
    """The message a user acts on. Three spellings of it is how two engines
    come to describe the same page differently."""
    needle = "not a product listing"
    for name in ENGINE_NAMES:
        text = (ROOT / f"{name}.py").read_text()
        assert needle in text, (
            f"{name} does not tell the user that a landing page is not a "
            f"listing — it would report 'zero products' instead")


# The browserless entry points. They are NOT in ENGINE_NAMES on purpose:
# the flag-parity checks compare the three browser engines against each
# other, and these two deliberately take no --headless, --cdp-endpoint,
# --fingerprint or captcha flags. See http_scraper.py's docstring for why
# each omission is honest on this site rather than a gap.
BROWSERLESS = ("http_scraper", "scraper_api_client")


@check
def test_http_engine_needs_no_browser_driver():
    """It must import with no playwright, selenium or pyppeteer installed.

    That is the whole point of it, and it is the kind of claim that rots
    quietly: one `import playwright` added for a type hint and the engine
    silently requires the thing it exists to avoid.
    """
    import ast
    tree = ast.parse((ROOT / "http_scraper.py").read_text())
    drivers = {"playwright", "selenium", "pyppeteer"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in drivers, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in drivers, node.module
    # and it really does import here, where no driver is guaranteed present
    import http_scraper                      # noqa: F401


@check
def test_http_engine_parses_identically_to_the_browser_engines():
    """Same HTML in, same rows out.

    Verified live as well (identical on every field of all 52 rows), but
    pinned offline so a divergence is caught without a network or a key.
    """
    import http_scraper

    class _Args:
        mode = "listing"

    want = P.parse_products(LISTING_HTML, LISTING_URL, page=1)
    # the HTTP engine calls the shared parser with the same arguments; assert
    # it does rather than re-implementing the call here
    src = inspect.getsource(http_scraper._fetch_one_page)
    assert "parse_products(html, response.url," in src, (
        "http_scraper does not call the shared parser the way the engines do")
    assert "page=page_num if args.mode == \"listing\" else None" in src, (
        "http_scraper threads `page` differently from the browser engines")
    assert want, "fixture produced no rows"


@check
def test_browserless_entry_points_take_the_data_flags():
    """They may skip the browser flags; they may not skip the output contract."""
    # What BOTH must take: the flags that decide what is read and what is
    # written. Not --proxy: the Scraper API runs the fetch on 2Captcha's own
    # infrastructure, so a local proxy has nothing to apply to there, and it
    # takes --cdp-url instead. http_scraper does the fetching itself and
    # therefore does need one.
    must_have = {"--url", "--category", "--mode", "--format", "--out",
                 "--retries", "--allow-empty"}
    for name in BROWSERLESS:
        flags = _flags_of(name)
        missing = must_have - flags
        assert not missing, f"{name} is missing {sorted(missing)}"
    http_flags = _flags_of("http_scraper")
    assert "--proxy" in http_flags, (
        "http_scraper does its own fetching and must accept an exit")
    assert "--proxy" not in _flags_of("scraper_api_client"), (
        "scraper_api_client gained --proxy, which it cannot honour: the "
        "fetch happens on 2Captcha's infrastructure. It takes --cdp-url.")
    for browser_only in ("--headless", "--headful", "--cdp-endpoint",
                         "--fingerprint", "--solve-captcha"):
        assert browser_only not in http_flags, (
            f"http_scraper takes {browser_only}, which it cannot honour: it "
            f"manages no browser. A flag that looks available and does "
            f"nothing is the shape of bug this family keeps finding.")


@check
def test_retries_is_validated_in_every_entry_point():
    """`--retries 0` makes the load loop body unreachable.

    `for attempt in range(1, args.retries + 1)` with 0 is an empty range, so
    nothing is fetched and the engine then classifies the browser's own
    blank page as a block — a fast, confident answer to a question nobody
    asked. A third-party audit ran exactly that on 2026-10-13 and reported
    the site unreachable; the 39-byte "evidence" was about:blank.

    Pinned for ALL FOUR entry points, including the two browserless ones,
    because the flag used to mean something different in one of them.
    """
    for name in ENGINE_NAMES + BROWSERLESS:
        text = (ROOT / f"{name}.py").read_text()
        assert 'p.add_argument("--retries", type=page_flow.attempts' in text, (
            f"{name} does not validate --retries through page_flow.attempts, "
            f"so --retries 0 silently fetches nothing")
    import argparse as _ap
    for bad in (0, -1, "x"):
        try:
            page_flow.attempts(bad)
        except _ap.ArgumentTypeError:
            continue
        raise AssertionError(f"page_flow.attempts accepted {bad!r}")
    assert page_flow.attempts(1) == 1 and page_flow.attempts("3") == 3


@check
def test_every_engine_help_runs():
    """The exact invocation a missing module breaks."""
    for name in ENGINE_NAMES:
        if ENGINE_MODS[name] is None:
            continue
        r = subprocess.run([sys.executable, str(ROOT / f"{name}.py"), "--help"],
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, f"{name} --help exited {r.returncode}: {r.stderr[:400]}"
        assert "sleep number" in r.stdout.lower(), f"{name} --help does not name the site"


@check
def test_engines_refuse_concurrency_with_a_reason():
    """A flag that looks available and silently does nothing is the shape of
    bug this family keeps finding. It must be refused WITH the reason."""
    for name in ENGINE_NAMES:
        text = (ROOT / f"{name}.py").read_text()
        assert "CONCURRENCY_CAPABLE_MODES" in text, name


@check
def test_proxy_credentials_never_reach_argv_or_a_log():
    """An EXCEPTION MESSAGE is a log. Masking must be GLOBAL: a
    masker that handles the first occurrence prints the password the other
    four times and looks like it is working."""
    # Built by CONCATENATION on purpose: no line of this file may hold a
    # complete scheme://user:pass@host literal, which is what keeps
    # ci_checks.py's credential scan fully live on smoke_test.py — the one
    # file where a real credential is most likely to be pasted while
    # debugging. An allowlist entry would switch the check off exactly there.
    secret = "s3" + "cr3t"
    url = "http://user:" + secret + "@na.proxy.2captcha.com:2334"
    masked = proxy_pool.mask(url)
    assert secret not in masked, masked
    assert "na.proxy.2captcha.com" in masked and "2334" in masked, (
        "masking removed the host and port — which exit a run used is the "
        "point of the log and is not the secret")
    many = " ".join([url] * 5)
    if hasattr(proxy_pool, "mask_all"):
        assert secret not in proxy_pool.mask_all(many)
    for mod in ENGINES:
        masker = getattr(mod, "_mask_credentials", None)
        if masker is None:
            continue
        assert secret not in masker(many), (
            f"{mod.__name__}._mask_credentials masks only the first occurrence")


# ===========================================================================
@check
def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim: the API
    echoes back the task it ran, so a credentialed CDP endpoint's username
    and password went into the log.

    The fixtures are assembled from pieces, never written out whole, because
    this file is scanned by the credential check like every other one.
    """
    import scraper_api_client as sac
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    gone = pw not in out and key not in out
    kept = ("cost=0.00145" in out and "cb.2captcha.com:9222" in out
            and "status=200" in out)
    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    both = s1 not in two and s2 not in two
    wired = ('logger.info("x-debug: %s", _redact_debug_header(debug))'
             in inspect.getsource(sac))
    assert gone, "a credential or key survived redaction"
    assert kept, "redaction ate the cost/host/status"
    assert both, "only the first credential was masked"
    assert wired, "the log line does not call the redactor"


# ===========================================================================
@check
def test_scraper_api_waitfor_is_an_object():
    """Both Scraper API defects measured 2026-09-23, through the real
    parse_args() and fetch_html(), with requests.post captured (no network).

    waitFor went out as a JSON-encoded STRING, which the live API refuses
    with HTTP 422 and still bills; and the target's status was read from
    `status`, which is the API's own verdict string ("success"), so a
    target 403/503 never reached detect_page_state. The real field is
    `http_code`."""
    import scraper_api_client as sac
    captured = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "headers": {},
                    "body": "<html></html>"}

    def _post(url, **kw):
        captured.update(kw)
        return _Resp()

    real_post, real_argv = sac.requests.post, sys.argv
    sac.requests.post = _post
    sys.argv = ["scraper_api_client.py", "--key", "k" * 8,
                "--url", LISTING_URL, "--wait-text", "Mattress"]
    try:
        _html, status = sac.fetch_html(sac.parse_args())
    finally:
        sac.requests.post, sys.argv = real_post, real_argv
    wf = (captured.get("json") or {}).get("waitFor")
    assert isinstance(wf, dict) and wf.get("text") == "Mattress", (
        "Scraper API: --wait-text must send waitFor as an OBJECT, not a "
        "JSON-encoded string (HTTP 422 and still billed, 2026-09-23); got %r" % (wf,))
    assert isinstance(status, int) and status == 403, (
        "Scraper API: the status handed onward must be the target's "
        "http_code (403, an int), not the API's verdict 'success'; got %r" % (status,))


# ===========================================================================
# 12. output contract, paid-solve budget, credential scan, CLI precedence
# ===========================================================================
import types as _types  # noqa: E402


def _sample_rows():
    return [Product(sku="QCM10", title="=HYPERLINK(\"http://x\",\"y\")",
                    url="@x", brand="-5", price=-5.0, currency="USD",
                    promo_badge="+1 free", size="Queen")]


@check
def test_output_writes_are_atomic_and_keep_modes():
    """A write that fails halfway must leave the previous file intact.

    `open(path, "w")` truncates before the first byte, so a failure inside
    `json.dump` used to turn a good `out.json` into a few bytes of invalid
    JSON, and the sidecar likewise. Reproduced 2026-10-14 (559 -> 13 bytes).
    New files take `0o666 & ~umask`, exactly as `open()` would; an existing
    file keeps its own mode.
    """
    import output_writer as ow
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out")
        good = [Product(sku="A1", title="t", price=1.0)]
        rc = ow.finish_run(good, out, "both", False, blocked=False,
                           stop_reason="completed", pages_requested=1,
                           pages_completed=1, start_url="u", final_url="u")
        assert rc == 0, rc
        before = {n: pathlib.Path(d, n).read_bytes() for n in os.listdir(d)}
        real_dump = ow.json.dump

        def failing_dump(obj, f, **kw):
            f.write("[{\"partial\":")
            raise OSError("disk full (planted)")
        ow.json.dump = failing_dump
        try:
            for fn, args in ((ow.write_json, (good, out + ".json")),
                             (ow.write_run_meta, (out, {"status": "x"}))):
                try:
                    fn(*args)
                except OSError:
                    pass
                else:
                    raise AssertionError(f"{fn.__name__} swallowed the failure")
        finally:
            ow.json.dump = real_dump
        after = {n: pathlib.Path(d, n).read_bytes() for n in os.listdir(d)}
        assert after == before, (
            "a failed write changed the files on disk (truncated, or left a "
            f"temp file behind): {sorted(set(after) ^ set(before)) or 'contents differ'}")

        # Modes: a NON-default umask is the case that tells candidate rules
        # apart, so 002 and 077 are tested beside 022.
        for umask, want in ((0o022, 0o644), (0o002, 0o664), (0o077, 0o600)):
            path = os.path.join(d, f"m{umask:o}.json")
            old = os.umask(umask)
            try:
                ow.write_json(good, path)
            finally:
                os.umask(old)
            got = os.stat(path).st_mode & 0o777
            assert got == want, f"umask {umask:o}: new file is {got:o}, want {want:o}"
        kept = os.path.join(d, "kept.json")
        ow.write_json(good, kept)
        os.chmod(kept, 0o640)
        ow.write_json(good, kept)
        assert os.stat(kept).st_mode & 0o777 == 0o640, "an existing file's mode was not kept"


@check
def test_csv_formula_cells_are_escaped_in_csv_only():
    """Strings starting with = + - @ TAB CR LF get a leading apostrophe in the
    CSV only. Numbers stay numbers (a -5.0 price must remain summable), the
    JSON keeps the site's bytes, and the sidecar declares the count —
    merged UNDER the caller's extra so a collision never drops site data."""
    import csv as _csv
    import output_writer as ow
    rows = _sample_rows()
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out")
        ow.finish_run(rows, out, "both", False, blocked=False,
                      stop_reason="completed", pages_requested=1,
                      pages_completed=1, start_url="u", final_url="u")
        with open(out + ".csv", newline="", encoding="utf-8") as f:
            row = next(_csv.DictReader(f))
        assert row["title"].startswith("'="), row["title"]
        assert row["url"] == "'@x", row["url"]
        assert row["brand"] == "'-5", row["brand"]
        assert row["promo_badge"] == "'+1 free", row["promo_badge"]
        assert row["price"] == "-5.0", f"a number was escaped: {row['price']!r}"
        data = json.loads(pathlib.Path(out + ".json").read_text())
        assert data[0]["title"].startswith("=") and data[0]["url"] == "@x", (
            "the JSON output was escaped too; it must keep the site's bytes")
        meta = json.loads(pathlib.Path(out + ".meta.json").read_text())
        assert meta.get("csv_cells_escaped") == 4, meta.get("csv_cells_escaped")

        out2 = os.path.join(d, "out2")
        ow.finish_run(rows, out2, "csv", False, blocked=False,
                      stop_reason="completed", pages_requested=1,
                      pages_completed=1, start_url="u", final_url="u",
                      extra={"csv_cells_escaped": "caller"})
        meta2 = json.loads(pathlib.Path(out2 + ".meta.json").read_text())
        assert meta2["csv_cells_escaped"] == "caller", (
            "the write statistic overrode the caller's own extra field")
    # A list cell is escaped AFTER it is joined into text.
    val, esc = ow._csv_escape(ow._csv_value(["=a", "b"]))
    assert esc and val.startswith("'="), val


def _function_source(text, name):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node)
    return None


@check
def test_solve_budget_is_one_purchase_per_page_in_every_engine():
    """The solver used to run once per block attempt, so a page fetched
    through a 3-exit pool could buy 3 solves (reproduced 2026-10-14 with the
    solver stubbed). Read off DISK, not through an import, so the check runs
    for every engine whatever drivers this venv holds."""
    b = page_flow.SolveBudget(page_flow.SOLVES_PER_PAGE)
    assert page_flow.SOLVES_PER_PAGE == 1
    assert [b.claim() for _ in range(3)] == [True, False, False]
    for name in ENGINE_NAMES:
        text = (ROOT / f"{name}.py").read_text()
        handler = _function_source(text, "handle_captcha_if_present")
        fetch = _function_source(text, "_fetch_one_page")
        assert handler and fetch, f"{name}: a function this check reads is gone"
        assert "budget=None" in handler, f"{name}: the handler takes no budget"
        claim, buy = handler.find(".claim()"), handler.find("solve_recaptcha(")
        assert 0 <= claim < buy, (
            f"{name}: the allowance is not checked before the paid request")
        made, loop = fetch.find("SolveBudget("), fetch.find("for block_attempt")
        assert 0 <= made < loop, (
            f"{name}: the page's allowance is not created once, outside the "
            f"retry/rotation loop")
        calls = re.findall(r"handle_captcha_if_present\((.*?)\)\s*:", fetch, re.S)
        assert calls and all("budget=budget" in c for c in calls), (
            f"{name}: a solve call site does not pass the page's budget")


class _Challenge:
    is_aws_waf = True
    has_captcha_widget = True
    kind = "AWS WAF"
    source = "stub"
    sitekey = "stub"
    aws_waf_action = "captcha"


def _drive_handler(mod, name):
    """Call the engine's real handler 3 times against one budget, with the
    detector and the solver stubbed. Returns the number of solver calls."""
    bought = []

    def stub_solver(*a, **k):
        bought.append(1)
        raise RuntimeError("stub solver: never reaches the network")
    saved = {}
    url = "https://www.sleepnumber.com/categories/mattresses"
    patches = {"solve_recaptcha": stub_solver,
               "detect_aws_waf": lambda html, u: _Challenge()}
    if name == "playwright_scraper":
        patches["_content_when_settled"] = lambda page, *a, **k: "<html></html>"
        patches["wait_for_autosolve"] = lambda page, sel: False
        target = _types.SimpleNamespace(url=url, query_selector_all=lambda s: [])
    elif name == "puppeteer_scraper":
        patches["_driver"] = lambda s: {"content": lambda: "<html></html>"}
        page = _types.SimpleNamespace(url=url, querySelectorAll=lambda s: [])
        target = _types.SimpleNamespace(page=page, bridge=_types.SimpleNamespace(run=lambda x: x))
    else:
        patches["_driver"] = lambda s: {"content": lambda: "<html></html>",
                                        "count": lambda s: 0,
                                        "current_url": lambda: url}
        target = _types.SimpleNamespace(driver=None)
    for k, v in patches.items():
        saved[k] = getattr(mod, k)
        setattr(mod, k, v)
    try:
        args = _types.SimpleNamespace(solve_captcha="when-blocked",
                                      twocaptcha_key="stub-not-a-key",
                                      captcha_api="v2", min_score=0.7)
        budget = page_flow.SolveBudget(page_flow.SOLVES_PER_PAGE)
        for _ in range(3):
            mod.handle_captcha_if_present(target, args, "a", proxy=None,
                                          budget=budget)
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)
    return len(bought)


@check
def test_solve_budget_holds_when_the_handler_really_runs():
    """Behavioural half of the check above, per engine that imports here."""
    ran = 0
    for name, mod in ENGINE_MODS.items():
        if mod is None:
            continue
        ran += 1
        n = _drive_handler(mod, name)
        assert n == page_flow.SOLVES_PER_PAGE, (
            f"{name}: {n} solver call(s) against one page's allowance of "
            f"{page_flow.SOLVES_PER_PAGE}")
    if not ran:
        skip("solve-budget behaviour", "no engine driver installed in this venv")


@check
def test_pyppeteer_hands_the_status_to_the_classifier():
    """The Response from `goto` used to be discarded, so the classifier saw
    no status or headers in this engine while its Playwright twin did."""
    text = (ROOT / "puppeteer_scraper.py").read_text()
    fetch = _function_source(text, "_fetch_one_page")
    assert "response = bridge.run(page.goto(" in fetch
    assert re.search(r"page_flow\.classify\(html, status=resp_status", fetch), (
        "puppeteer_scraper: classify is called without the HTTP status")


@check
def test_a_typed_category_beats_an_environment_url():
    """`SLEEPNUMBER_URL` in the environment used to replace a typed
    `--category` silently: the loader filled `args.url`, and the builder ran
    only `if not args.url`. Reproduced 2026-10-14 in two engines."""
    import argparse as _ap

    class _P(_ap.ArgumentParser):
        def error(self, message):
            raise ValueError(message)
    env_url = "https://www.sleepnumber.com/categories/mattresses"
    ns = _types.SimpleNamespace
    a = ns(url=env_url, category="furniture", mode="listing")
    page_flow.resolve_start_url(_P(), a, typed_url=None)
    assert a.url.endswith("/categories/furniture"), a.url
    a = ns(url=env_url, category=None, mode="listing")
    page_flow.resolve_start_url(_P(), a, typed_url=None)
    assert a.url == env_url, a.url
    a = ns(url=env_url, category="furniture", mode="listing")
    try:
        page_flow.resolve_start_url(_P(), a, typed_url=env_url)
    except ValueError as e:
        assert "--url" in str(e) and "--category" in str(e), e
    else:
        raise AssertionError("--url and --category both typed was accepted")
    a = ns(url=None, category=None, mode="product")
    try:
        page_flow.resolve_start_url(_P(), a, typed_url=None)
    except ValueError:
        pass
    else:
        raise AssertionError("--mode product with nothing to read was accepted")
    for name in ENGINE_NAMES + ("http_scraper",):
        text = (ROOT / f"{name}.py").read_text()
        cap, apply_ = text.find("typed_url = args.url"), text.find("env_config.apply(args)")
        assert 0 <= cap < apply_, f"{name}: the typed URL is not captured before env_config.apply"
        assert "page_flow.resolve_start_url(p, args, typed_url)" in text, (
            f"{name}: does not resolve the start URL through page_flow")


@check
def test_numeric_flags_reject_negative_values():
    """A negative --delay reaches time.sleep and raises mid-run; --pages 0
    or --concurrency 0 asks for nothing. Rejected at parse time, by name."""
    import argparse as _ap
    for fn, bad in ((page_flow.positive_int, ("0", "-1", "x")),
                    (page_flow.non_negative_int, ("-1", "x")),
                    (page_flow.non_negative_float, ("-0.5", "x", "nan"))):
        for v in bad:
            try:
                fn(v)
            except _ap.ArgumentTypeError:
                continue
            raise AssertionError(f"{fn.__name__} accepted {v!r}")
    assert page_flow.non_negative_int("0") == 0 and page_flow.non_negative_float("0") == 0
    want = {'"--pages"': "page_flow.positive_int",
            '"--concurrency"': "page_flow.positive_int",
            '"--delay"': "page_flow.non_negative_float",
            '"--retry-delay"': "page_flow.non_negative_float",
            '"--proxy-block-retries"': "page_flow.non_negative_int"}
    for name in ENGINE_NAMES + BROWSERLESS:
        text = (ROOT / f"{name}.py").read_text()
        for flag, validator in want.items():
            m = re.search(r"p\.add_argument\(" + re.escape(flag) + r",\s*type=([\w.]+)", text)
            if m:
                assert m.group(1) == validator, (
                    f"{name} {flag} uses type={m.group(1)}, not {validator}")


def _scan_copy(dest, plant=None):
    """Copy the repo into `dest` and run its own credential scan there."""
    import shutil
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache"))
    if plant:
        rel, content = plant
        pathlib.Path(dest, rel).write_text(content)
    return subprocess.run(
        [sys.executable, str(pathlib.Path(dest, ".github", "ci_checks.py")),
         "--secret-check"], capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


@check
def test_credential_scan_reads_backups_and_json_and_any_parent_dir():
    """Three holes, each reproduced 2026-10-14: a clone under a directory
    called `venv/` scanned 0 files and printed the clean line (exclusions
    tested the ABSOLUTE path), and a key in an untracked `.env.bak` or a
    JSON capture was never read."""
    if not (ROOT / ".github" / "ci_checks.py").exists():
        assert not (ROOT / ".github").exists(), ".github/ exists but ci_checks.py is missing"
        return
    key = "ab" * 16          # assembled, so this file carries no 32-hex literal
    with tempfile.TemporaryDirectory() as d:
        r = _scan_copy(os.path.join(d, "venv", "clone"))
        assert r.returncode == 0, r.stdout + r.stderr
        m = re.search(r"ok\s+(\d+) files scanned", r.stdout)
        assert m and int(m.group(1)) > 10, (
            f"a clone under venv/ scanned too little: {r.stdout.strip()!r}")
    for rel, content in ((".env.bak", f"TWOCAPTCHA_KEY={key}\n"),
                         ("capture_dump.json", json.dumps({"apiKey": key}))):
        with tempfile.TemporaryDirectory() as d:
            r = _scan_copy(os.path.join(d, "clone"), (rel, content))
            assert r.returncode != 0 and rel in r.stdout, (
                f"a key planted in {rel} was not caught:\n{r.stdout}")


@check
def test_no_private_citations_in_shipped_files():
    """This repo's public files must state their rules in their own words.
    Citations of a private instructions file, or bare section numbers into
    it, point readers at a document they cannot open. The tokens are
    assembled from pieces so this check can scan its own file."""
    banned = ("CLAUDE" + ".md", "claude_" + "2scraper", "2scraper_" + "repo_rules",
              "play" + "book " + chr(0xA7), "template " + chr(0xA7), chr(0xA7))
    files = list(_shipped_files({".py", ".md", ".yml", ".yaml", ".txt",
                                 ".toml", ".sh", ".example", ".cfg"}))
    files += [ROOT / n for n in (".gitignore", ".dockerignore", "Dockerfile")
              if (ROOT / n).exists()]
    assert len(files) > 20, f"scanned only {len(files)} files"
    hits = []
    for path in files:
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if any(t in line for t in banned):
                hits.append(f"{path.relative_to(ROOT)}:{i}")
    assert not hits, f"private citations in shipped files: {hits[:10]}"


# ===========================================================================
# Proxy traffic savers: the pre-check and media blocking
# ===========================================================================
# Each sub-check is recorded under its own label, so a planted fault shows
# up by name rather than as one opaque failure.
import contextlib  # noqa: E402


def _savers_check(label, condition):
    if condition:
        PASSES.append(label)
        if VERBOSE:
            print(f"ok    {label}")
    else:
        FAILURES.append((label, "failed"))
        print(f"FAIL  {label}")
    return bool(condition)


def _savers_group(title):
    if VERBOSE:
        print(f"\n== {title}")


class _SaversSkips(list):
    def append(self, msg):
        skip("proxy savers", msg)
_SAVERS_URL = LISTING_URL
_SAVERS_SECRET = "s3cret-Pw-not-real"


class _StubProxy:
    """A one-port HTTP proxy on 127.0.0.1 that answers every request with a
    fixed status and records the request heads it received."""

    def __init__(self, status):
        import socket
        import threading
        self.status, self.heads = status, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.heads.append(data.decode("latin-1"))
                reason = {407: "Proxy Authentication Required",
                          402: "Payment Required", 200: "OK"}.get(
                              self.status, "X")
                conn.sendall(f"HTTP/1.1 {self.status} {reason}\r\n"
                             "Content-Length: 0\r\nConnection: close\r\n\r\n"
                             .encode())

    def url(self, secret=_SAVERS_SECRET):
        # Concatenated, so no line here looks like a credentialled URL.
        return "http://user-zone-x:" + secret + "@127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _captured_output():
    """stdout and every log record, as one string."""
    import logging
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        root.removeHandler(handler)


class _FakeRoute:
    def __init__(self, resource_type):
        self.request = type("R", (), {"resource_type": resource_type})()
        self.verdict = None

    def abort(self):
        self.verdict = "abort"

    def continue_(self):
        self.verdict = "continue"


def _media_verdicts(handler):
    out = {}
    for rtype in ("image", "media", "font", "document", "script", "xhr",
                  "fetch", "stylesheet"):
        route = _FakeRoute(rtype)
        handler(route)
        out[rtype] = route.verdict
    return out


def _engine_args(engine, tmp, proxy_url, *extra):
    """The engine's own parser, on a stubbed argv, with no .env in play."""
    from unittest import mock
    argv = [engine.__name__, "--url", _SAVERS_URL, "--out",
            os.path.join(tmp, "out")]
    if proxy_url:
        argv += ["--proxy", proxy_url]
    argv += list(extra)
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(env_config, "load_env", lambda *a, **k: None):
        return engine.parse_args()


def test_proxy_traffic_savers(skips, check, group):
    group("Proxy traffic savers: pre-check before the browser, media "
          "blocking through a proxy")
    import argparse
    import asyncio
    from unittest import mock
    import output_writer
    import proxy_check
    from proxy_pool import ProxyPool
    ok = True

    # -- the pre-check, against a stub proxy --------------------------------
    stub = _StubProxy(407)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        ok &= check("a 407 from the proxy is auth_failed and fatal "
                    f"({res.outcome})", res.outcome == "auth_failed"
                    and res.fatal and res.stop_reason == "proxy_auth_failed")
        head = stub.heads[-1] if stub.heads else ""
        ok &= check("the pre-check is ONE CONNECT to the target host, with "
                    "the credential", len(stub.heads) == 1
                    and head.startswith("CONNECT www.sleepnumber.com:443 ")
                    and "Proxy-Authorization: Basic " in head)
        with _captured_output() as buf:
            gated = proxy_check.gate(ProxyPool([stub.url()]), _SAVERS_URL,
                                     timeout=5)
        text = buf.getvalue()
        ok &= check("gate() returns the fatal check on a 407",
                    gated is not None and gated.fatal)
        ok &= check("the 407 message names the variable and the cause",
                    proxy_check.PROXY_SOURCES.split()[0] in text
                    and "refused the credential" in text
                    and "traffic balance is exhausted" in text)
        ok &= check("the 407 message never prints the credential",
                    _SAVERS_SECRET not in text and "user-zone-x" not in text)
    finally:
        stub.close()

    stub = _StubProxy(402)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        msg = proxy_check.failure_message(res, stub.url())
        ok &= check("a 402 is balance_exhausted and fatal",
                    res.outcome == "balance_exhausted" and res.fatal
                    and res.stop_reason == "proxy_balance_exhausted")
        ok &= check("the 402 message says traffic/balance exhausted, no "
                    "credential", "traffic/balance exhausted" in msg
                    and _SAVERS_SECRET not in msg)
    finally:
        stub.close()

    stub = _StubProxy(200)
    try:
        res = proxy_check.precheck(stub.url(), "http://127.0.0.1:9/x",
                                   timeout=5)
        head = stub.heads[-1] if stub.heads else ""
        ok &= check("a plain-http target is one HEAD for its robots.txt",
                    res.outcome == "ok" and not res.fatal
                    and head.startswith("HEAD http://127.0.0.1:9/robots.txt "))
    finally:
        stub.close()

    dead = "http://u:" + _SAVERS_SECRET + "@127.0.0.1:" + str(_closed_port())
    res = proxy_check.precheck(dead, _SAVERS_URL, timeout=5)
    ok &= check("a proxy that does not answer is a transport error, NOT "
                "fatal (existing retry behaviour applies)",
                res.outcome == "transport_error" and not res.fatal)
    with _captured_output() as buf:
        gated = proxy_check.gate(ProxyPool([dead]), _SAVERS_URL, timeout=5)
    ok &= check("gate() lets a transport error through, credential unprinted",
                gated is None and _SAVERS_SECRET not in buf.getvalue())
    ok &= check("no pool means no pre-check",
                proxy_check.gate(None, _SAVERS_URL) is None)
    ok &= check("a socks5 exit is skipped, not refused",
                proxy_check.precheck("socks5://127.0.0.1:1", _SAVERS_URL)
                .outcome == "skipped")

    # -- the attempt sidecar ------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = output_writer.finish_without_fetch(
                out, stop_reason="proxy_auth_failed", pages_requested=3,
                start_url=_SAVERS_URL)
        with open(out + ".latest_attempt.meta.json") as f:
            meta = json.load(f)
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
    ok &= check(f"a refused proxy exits 5 ({rc})", rc == 5)
    ok &= check("...and records status failed / proxy_auth_failed in the "
                "attempt sidecar", meta.get("status") == "failed"
                and meta.get("stop_reason") == "proxy_auth_failed"
                and meta.get("pages_completed") == 0)
    ok &= check("...leaving the previous output and its .meta.json alone",
                untouched)

    # -- when media is blocked ----------------------------------------------
    pool = ProxyPool(["http://127.0.0.1:1"])
    ns = argparse.Namespace
    ok &= check("blocked through a local proxy by default",
                proxy_check.block_media(ns(load_media=False, block_media=False),
                                        pool, remote=False)
                is proxy_check.BLOCK_MEDIA_BY_DEFAULT)
    ok &= check("not blocked without a proxy",
                not proxy_check.block_media(
                    ns(load_media=False, block_media=False), None, False))
    ok &= check("not blocked over --cdp-endpoint, even when forced",
                not proxy_check.block_media(
                    ns(load_media=False, block_media=True), pool, True))
    ok &= check("--load-media turns it off",
                not proxy_check.block_media(
                    ns(load_media=True, block_media=False), pool, False))
    ok &= check("an env value of 0 does not count as --load-media",
                proxy_check.block_media(ns(load_media="0", block_media=False),
                                        pool, False)
                is proxy_check.BLOCK_MEDIA_BY_DEFAULT)
    ok &= check("the hidden --block-media forces it without a proxy",
                proxy_check.block_media(ns(load_media=False, block_media=True),
                                        None, False))
    parser = argparse.ArgumentParser()
    proxy_check.add_media_flags(parser)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--load-media", "--block-media"])
        exclusive = False
    except SystemExit:
        exclusive = True
    ok &= check("--load-media and --block-media are mutually exclusive",
                exclusive)
    ok &= check("--block-media is hidden from --help",
                "--block-media" not in parser.format_help()
                and "--load-media" in parser.format_help())
    ok &= check(f"{proxy_check.LOAD_MEDIA_ENV} maps onto --load-media",
                env_config.ENV_KEYS.get(proxy_check.LOAD_MEDIA_ENV)
                == "load_media")

    # -- the route handler, on a stub page ----------------------------------
    ctx = mock.MagicMock()
    with _captured_output():
        proxy_check.install_playwright(ctx)
    pattern, handler = ctx.route.call_args[0]
    verdicts = _media_verdicts(handler)
    ok &= check(f"Playwright: route on every URL ({pattern})", pattern == "**/*")
    ok &= check("Playwright: image/media/font aborted, everything else "
                f"continues {verdicts}",
                all(verdicts[t] == "abort" for t in ("image", "media", "font"))
                and all(v == "continue" for t, v in verdicts.items()
                        if t not in ("image", "media", "font")))

    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive_pyppeteer():
        await proxy_check.install_pyppeteer(page)
        on_paused = cdp.on.call_args[0][1]
        on_paused({"requestId": "r1", "resourceType": "Image"})
        await asyncio.sleep(0)

    with _captured_output():
        asyncio.run(_drive_pyppeteer())
    calls = [c[0] for c in cdp.send.await_args_list]
    enable = next((c[1] for c in calls if c[0] == "Fetch.enable"), {})
    kinds = sorted(pt.get("resourceType") for pt in enable.get("patterns", []))
    ok &= check("pyppeteer: Fetch pauses ONLY Image/Media/Font, on its own "
                f"CDP session {kinds}", cdp.on.call_args[0][0] ==
                "Fetch.requestPaused" and kinds == ["Font", "Image", "Media"])
    ok &= check("pyppeteer: a paused request is failed, not continued",
                ("Fetch.failRequest", {"requestId": "r1",
                                       "errorReason": "BlockedByClient"})
                in calls)
    ok &= check("pyppeteer: never the removed Network.setRequestInterception",
                "setRequestInterception" not in inspect.getsource(
                    proxy_check.install_pyppeteer).split('"""')[-1])

    driver = mock.MagicMock()
    with _captured_output():
        installed = proxy_check.install_selenium(driver)
    sent = {c[0][0]: c[0][1] for c in driver.execute_cdp_cmd.call_args_list}
    urls = sent.get("Network.setBlockedURLs", {}).get("urls", [])
    ok &= check("Selenium: Network.setBlockedURLs with image/video/font "
                "patterns", installed and "*.png" in urls and "*.woff2" in urls
                and "*.mp4?*" in urls)
    ok &= check("Selenium: no pattern touches a document, script, JSON or "
                "stylesheet", not [u for u in urls if re.search(
                    r"\.(html?|js|json|css)\b", u)])
    broken = mock.MagicMock()
    broken.execute_cdp_cmd.side_effect = RuntimeError("no CDP")
    with _captured_output():
        ok &= check("Selenium: a driver that refuses CDP is not fatal",
                    proxy_check.install_selenium(broken) is False)

    # -- the requests transport, driven: no page request on a 407 ----------
    import http_scraper
    stub = _StubProxy(407)
    fetched = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            args = _engine_args(http_scraper, tmp, stub.url())
            with mock.patch.object(
                    http_scraper, "_fetch_one_page",
                    side_effect=lambda *a, **k: fetched.append(1)), \
                    _captured_output() as buf:
                try:
                    rc = http_scraper.scrape(args)
                except Exception as e:  # noqa: BLE001 — fail BY NAME
                    rc = f"raised {type(e).__name__}"
            side = os.path.join(tmp, "out.latest_attempt.meta.json")
            meta = json.load(open(side)) if os.path.exists(side) else {}
    finally:
        stub.close()
    ok &= check("http_scraper: a 407 exits 5 before any page request "
                f"(rc={rc}, fetched={len(fetched)})", rc == 5 and not fetched)
    ok &= check("http_scraper: ...stop_reason proxy_auth_failed in the "
                "attempt sidecar, credential unprinted",
                meta.get("stop_reason") == "proxy_auth_failed"
                and _SAVERS_SECRET not in buf.getvalue())

    # -- each engine, driven: no browser is started on a 407 ----------------
    for name in ENGINE_NAMES:
        try:
            engine = __import__(name)
        except ImportError as e:
            skips.append(f"{name} proxy savers driving — {e}")
            print("  SKIP  %s proxy savers — %s" % (name, e))
            continue
        stub = _StubProxy(407)
        launched = []
        if name == "playwright_scraper":
            target = "sync_playwright"
        else:
            target = "_Session"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, stub.url())
                with mock.patch.object(
                        engine, target,
                        side_effect=lambda *a, **k: launched.append(1)), \
                        _captured_output() as buf:
                    try:
                        rc = engine.scrape(args)
                    except Exception as e:  # noqa: BLE001 — fail BY NAME
                        rc = f"raised {type(e).__name__}"
                side = os.path.join(tmp, "out.latest_attempt.meta.json")
                meta = json.load(open(side)) if os.path.exists(side) else {}
                wrote_rows = any(os.path.exists(os.path.join(tmp, "out" + s))
                                 for s in (".json", ".csv", ".meta.json"))
        finally:
            stub.close()
        ok &= check(f"{name}: a 407 exits 5 without starting a browser "
                    f"(rc={rc}, launched={len(launched)})",
                    rc == 5 and not launched)
        ok &= check(f"{name}: ...with stop_reason proxy_auth_failed in the "
                    "attempt sidecar and no output written",
                    meta.get("stop_reason") == "proxy_auth_failed"
                    and meta.get("status") == "failed" and not wrote_rows)
        ok &= check(f"{name}: ...and the credential never reaches the log",
                    _SAVERS_SECRET not in buf.getvalue())

    # -- each engine installs blocking only through a local proxy -----------
    for name in ENGINE_NAMES:
        try:
            engine = __import__(name)
        except ImportError:
            continue  # already recorded as a skip above
        for label, proxy, extra, want in (
                ("through a proxy", "http://127.0.0.1:1", (), None),
                ("with --load-media", "http://127.0.0.1:1",
                 ("--load-media",), False),
                ("without a proxy", None, (), False),
                ("forced, no proxy", None, ("--block-media",), True)):
            if want is None:
                want = proxy_check.BLOCK_MEDIA_BY_DEFAULT
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy, *extra)
            pool = ProxyPool([proxy]) if proxy else None
            got = _engine_installs_blocking(engine, args, pool)
            ok &= check(f"{name}: media blocking {label} = {want} (got {got})",
                        got is want)
    return ok


def _engine_installs_blocking(engine, args, pool):
    """Open the engine's local session on fakes; True if blocking went in."""
    from unittest import mock
    name = engine.__name__
    with _captured_output():
        if name == "playwright_scraper":
            pw = mock.MagicMock()
            browser = pw.chromium.launch.return_value
            browser.version = "120.0.0.0"
            ctx = browser.new_context.return_value
            engine._launch_local(pw, args, pool)
            return ctx.route.called
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                driver = chrome.return_value
                driver.capabilities = {"browserVersion": "120.0.0.0"}
                engine._Session(args, pool).open()
            return any(c[0][0] == "Network.setBlockedURLs"
                       for c in driver.execute_cdp_cmd.call_args_list)
        page = mock.MagicMock()
        for attr in ("setUserAgent", "setViewport", "authenticate",
                     "setRequestInterception", "evaluateOnNewDocument",
                     "setExtraHTTPHeaders"):
            setattr(page, attr, mock.AsyncMock())
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        browser = mock.MagicMock()
        browser.newPage = mock.AsyncMock(return_value=page)
        browser.version = mock.AsyncMock(return_value="HeadlessChrome/120.0")

        async def fake_launch(*a, **k):
            return browser
        bridge = engine._AsyncBridge()
        try:
            with mock.patch.object(engine, "launch", fake_launch):
                engine._Session(bridge, args, pool).open()
        finally:
            closer = getattr(bridge, "close", None)
            if closer:
                closer()
        return any(c[0][0] == "Fetch.enable" for c in cdp.send.await_args_list)


from proxy_pool import ProxyPool  # noqa: E402


# ---------------------------------------------------------------------------
# Proxy traffic meter and budget
# ---------------------------------------------------------------------------

class _ByteCountingProxy:
    """A forward HTTP proxy on 127.0.0.1 that serves every plain-http GET
    itself with a body of a known size, and counts every byte it received
    and sent. The meter is judged against these two numbers."""

    def __init__(self, sizes):
        import socket
        import threading
        self.sizes = dict(sizes)            # path -> body length
        self.received = self.sent = 0
        # CONNECTs are the browser's own background connections (a system
        # Chromium phones home through its proxy too); they are not page
        # requests, this stub refuses them, and they are kept apart.
        self.connects = 0
        self.paths = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import threading
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        from urllib.parse import urlparse
        with conn:
            buf = b""
            while True:
                while b"\r\n\r\n" not in buf:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                if lines[0].startswith("CONNECT "):
                    self.connects += 1
                    try:
                        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                                     b"Content-Length: 0\r\n\r\n")
                    except OSError:
                        pass
                    return
                self.received += len(head) + 4
                length = 0
                for line in lines[1:]:
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                while len(buf) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                self.received += length
                buf = buf[length:]
                path = urlparse(lines[0].split(" ")[1]).path or "/"
                self.paths.append(path)
                size = self.sizes.get(path, 0)
                ctype = ("text/html" if path == "/" else "text/css"
                         if path.endswith(".css") else
                         "application/javascript" if path.endswith(".js")
                         else "application/octet-stream")
                body = (b"<html><head><link rel=stylesheet href=/s.css>"
                        b"<script src=/a.js></script></head><body><p id=t>"
                        if path == "/" else b"")
                body = body + b"x" * max(0, size - len(body))
                if path == "/":
                    body = body[:-11] + b"</p></body>"
                status = b"200 OK" if path in self.sizes else b"404 Not Found"
                out = (b"HTTP/1.1 " + status + b"\r\nContent-Type: "
                       + ctype.encode() + b"\r\nContent-Length: "
                       + str(len(body)).encode()
                       + b"\r\nCache-Control: no-store\r\n\r\n" + body)
                try:
                    conn.sendall(out)
                except OSError:
                    return
                self.sent += len(out)

    @property
    def total(self):
        return self.received + self.sent

    def url(self):
        return "http://127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _base_exception(fn):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 — the point is to see it
        return e
    return None


def test_proxy_meter_units(skips):
    _savers_group("Proxy traffic meter: counting, the budget, the sidecar")
    import argparse
    from unittest import mock
    import output_writer
    import proxy_meter
    ok = True
    ns = argparse.Namespace

    # -- the meter itself ----------------------------------------------------
    m = proxy_meter.ProxyMeter(1)
    fired = []
    m.on_exceed(lambda: fired.append(1))
    m.add(600_000)
    m.add(400_000)
    ok &= _savers_check("1 MB budget: exactly 1,000,000 bytes is not over it",
                m.bytes == 1_000_000 and not m.exceeded and not fired)
    m.add(1)
    ok &= _savers_check("...one byte more is, and the refusal hook fires",
                m.exceeded and fired == [1])
    m.add(50)
    ok &= _savers_check("...the hook fires once, not per request", fired == [1])
    err = _base_exception(m.check)
    ok &= _savers_check("check() raises ProxyBudgetExceeded once over budget",
                isinstance(err, proxy_meter.ProxyBudgetExceeded))
    ok &= _savers_check("ProxyBudgetExceeded passes an `except Exception` (a retry "
                "loop cannot swallow it)",
                not issubclass(proxy_meter.ProxyBudgetExceeded, Exception))
    ok &= _savers_check("sidecar fields: bytes, budget in bytes, exceeded",
                m.sidecar() == {"proxy_bytes": 1_000_051,
                                "proxy_budget_bytes": 1_000_000,
                                "proxy_budget_exceeded": True})
    ok &= _savers_check(f"summary line ({m.summary()})",
                m.summary() == "Proxy traffic: 1.0 MB of 1.0 MB budget")
    late = []
    m.on_exceed(lambda: late.append(1))
    ok &= _savers_check("a hook added after the cap was crossed runs at once",
                late == [1])
    free = proxy_meter.ProxyMeter(0)
    free.add(10 ** 12)
    ok &= _savers_check("--proxy-budget-mb 0 means no cap (null budget in the "
                "sidecar)", not free.exceeded and _base_exception(free.check)
                is None and free.sidecar()["proxy_budget_bytes"] is None)

    # -- the flag and the environment ----------------------------------------
    parser = argparse.ArgumentParser()
    proxy_meter.add_budget_flag(parser)
    for bad in ("-1", "abc", "nan", "inf"):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["--proxy-budget-mb", bad])
            refused = False
        except SystemExit as e:
            refused = e.code == 2
        ok &= _savers_check(f"--proxy-budget-mb {bad} is a usage error (exit 2)",
                    refused)
    ok &= _savers_check("--proxy-budget-mb 0 and 2.5 parse",
                parser.parse_args(["--proxy-budget-mb", "0"]).proxy_budget_mb
                == 0.0 and parser.parse_args(
                    ["--proxy-budget-mb", "2.5"]).proxy_budget_mb == 2.5)
    env = proxy_meter.BUDGET_ENV
    with mock.patch.dict(os.environ, {env: ""}):
        ok &= _savers_check("default budget is 10 MB",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 10.0
                    and proxy_meter.DEFAULT_BUDGET_MB == 10.0)
    with mock.patch.dict(os.environ, {env: "2.5"}):
        ok &= _savers_check(f"{env} sets the budget",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 2.5)
        ok &= _savers_check("...and the flag wins over it",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=1.0)) == 1.0)
    with mock.patch.dict(os.environ, {env: "lots"}):
        ok &= _savers_check(f"an unparseable {env} stops the run instead of "
                    "silently using a default",
                    isinstance(_base_exception(lambda: proxy_meter.budget_mb(
                        ns(proxy_budget_mb=None))), SystemExit))
    if hasattr(env_config, "ENV_KEYS"):
        ok &= _savers_check(f"{env} maps onto --proxy-budget-mb",
                    env_config.ENV_KEYS.get(env) == "proxy_budget_mb")
    pool = ProxyPool(["http://127.0.0.1:1"])
    ok &= _savers_check("metered only through a local proxy: not without one, not "
                "over --cdp-endpoint",
                proxy_meter.start(ns(proxy_budget_mb=None), None) is None
                and proxy_meter.start(ns(proxy_budget_mb=None), pool,
                                      remote=True) is None
                and isinstance(proxy_meter.start(ns(proxy_budget_mb=None),
                                                 pool), proxy_meter.ProxyMeter))

    a = ns(proxy_budget_mb=0.001)
    proxy_meter.start(a, pool).add(5_000)
    ok &= _savers_check("proxy_meter.check(args) stops a run that is over budget",
                isinstance(_base_exception(lambda: proxy_meter.check(a)),
                           proxy_meter.ProxyBudgetExceeded)
                and _base_exception(lambda: proxy_meter.check(
                    ns(proxy_budget_mb=None))) is None)

    # -- CDP events with known sizes (pyppeteer, Selenium) ------------------
    m = proxy_meter.ProxyMeter(0)
    c = proxy_meter.CdpCounter(m)
    req = {"method": "GET", "url": "http://x.test/a", "headers": {"A": "b"}}
    est = len("GET http://x.test/a HTTP/1.1\r\n") + 2 + len("A: b\r\n")
    c.feed("Network.requestWillBeSent", {"requestId": "1", "request": req})
    c.feed("Network.dataReceived", {"requestId": "1", "encodedDataLength": 99})
    c.feed("Network.loadingFinished", {"requestId": "1",
                                       "encodedDataLength": 1234})
    ok &= _savers_check(f"CDP: a finished request = its request + encodedDataLength "
                f"({m.bytes} vs {est + 1234})", m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "2", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2",
                                     "errorText": "net::ERR_BLOCKED_BY_CLIENT"})
    c.feed("Network.requestWillBeSent", {"requestId": "2b", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2b", "errorText": "x",
                                     "blockedReason": "inspector"})
    ok &= _savers_check("CDP: a request the browser blocked costs nothing",
                m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "3", "request": req})
    c.feed("Network.dataReceived", {"requestId": "3", "encodedDataLength": 50})
    c.feed("Network.loadingFailed", {"requestId": "3",
                                     "errorText": "net::ERR_CONNECTION_RESET"})
    ok &= _savers_check("CDP: a request that failed mid-body costs what arrived",
                m.bytes == 2 * est + 1234 + 50)
    c.feed("Network.requestWillBeSent", {"requestId": "4", "request": req})
    c.feed("Network.requestWillBeSent", {
        "requestId": "4", "request": req,
        "redirectResponse": {"encodedDataLength": 300}})
    c.feed("Network.loadingFinished", {"requestId": "4",
                                       "encodedDataLength": 500})
    ok &= _savers_check("CDP: a redirect hop is counted as its own exchange",
                m.bytes == 4 * est + 1234 + 50 + 300 + 500)

    # -- Playwright: sizes() of every finished/failed request ---------------
    m = proxy_meter.ProxyMeter(0.001)
    ctx = mock.MagicMock()
    proxy_meter.install_playwright(ctx, m)
    on = {call[0][0]: call[0][1] for call in ctx.on.call_args_list}
    request = mock.MagicMock()
    request.sizes.return_value = {"requestHeadersSize": 100,
                                  "requestBodySize": 20,
                                  "responseHeadersSize": 200,
                                  "responseBodySize": 500}
    on.get("requestfinished", lambda r: None)(request)
    dead = mock.MagicMock()
    dead.sizes.side_effect = RuntimeError("no response")
    on.get("requestfailed", lambda r: None)(dead)
    ok &= _savers_check(f"Playwright: request.sizes() summed on requestfinished "
                f"({m.bytes} of 820), a failed one without sizes is 0",
                m.bytes == 820 and "requestfailed" in on)
    pattern, gate = ctx.route.call_args[0]
    route = mock.MagicMock()
    gate(route)
    ok &= _savers_check(f"Playwright: under budget the route falls through to the "
                f"next handler ({pattern})", pattern == "**/*"
                and route.fallback.called and not route.abort.called)
    on["requestfinished"](request)
    route = mock.MagicMock()
    gate(route)
    ok &= _savers_check("Playwright: over budget every new request is aborted",
                route.abort.called and not route.fallback.called)
    ok &= _savers_check("no meter, nothing installed",
                proxy_meter.install_playwright(ctx, None) is None
                and ctx.route.call_count == 1)

    # -- Selenium: the performance log --------------------------------------
    m = proxy_meter.ProxyMeter(0.001)
    driver = mock.MagicMock()
    log = [{"message": json.dumps({"message": {
        "method": "Network.requestWillBeSent",
        "params": {"requestId": "9", "request": req}}})},
        {"message": json.dumps({"message": {
            "method": "Network.loadingFinished",
            "params": {"requestId": "9", "encodedDataLength": 2000}}})},
        {"message": "not json"}]
    driver.get_log.return_value = log
    proxy_meter.install_selenium(driver, m)
    entries = proxy_meter.selenium_log(driver)
    ok &= _savers_check(f"Selenium: the performance log is counted ({m.bytes} = "
                f"{est + 2000}) and handed back to the engine's own reader",
                m.bytes == est + 2000 and entries == log)
    blocked = [call[0] for call in driver.execute_cdp_cmd.call_args_list]
    ok &= _savers_check("Selenium: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in blocked)
    options = mock.MagicMock()
    proxy_meter.selenium_options(options, m)
    ok &= _savers_check("Selenium: the performance log is requested",
                options.set_capability.call_args[0] ==
                ("goog:loggingPrefs", {"performance": "ALL"}))

    # -- pyppeteer: its own CDP session -------------------------------------
    import asyncio
    m = proxy_meter.ProxyMeter(0.001)
    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive():
        await proxy_meter.install_pyppeteer(page, m)
        on = {call[0][0]: call[0][1] for call in cdp.on.call_args_list}
        on["Network.requestWillBeSent"]({"requestId": "p", "request": req})
        on["Network.loadingFinished"]({"requestId": "p",
                                       "encodedDataLength": 1500})
        await asyncio.sleep(0)

    asyncio.run(_drive())
    sent = [call[0] for call in cdp.send.await_args_list]
    ok &= _savers_check(f"pyppeteer: Network events counted ({m.bytes} = "
                f"{est + 1500})", m.bytes == est + 1500
                and ("Network.enable", {}) in sent)
    ok &= _savers_check("pyppeteer: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in sent)

    # -- an HTTP client, through a real stub proxy with known sizes ---------
    import requests
    stub = _ByteCountingProxy({"/": 20_000})
    try:
        m = proxy_meter.ProxyMeter(0)
        r = requests.get("http://meter.test/", proxies={"http": stub.url()},
                         timeout=10)
        proxy_meter.count_response(m, r)
        import time as _time
        for _ in range(50):
            if stub.sent:
                break
            _time.sleep(0.02)
        drift = abs(m.bytes - stub.total) / stub.total
        ok &= _savers_check(f"HTTP client through a stub proxy: metered {m.bytes} B, "
                    f"the proxy carried {stub.total} B ({drift:.1%}, "
                    f"tolerance 5%)", drift <= 0.05)
    finally:
        stub.close()

    # -- the sidecars --------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        metered = proxy_meter.ProxyMeter(10)
        metered.add(123_456)
        rows = _meter_rows()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(rows, out, "completed", metered.sidecar())
        meta = json.load(open(out + ".meta.json"))
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        ok &= _savers_check(f"a metered run records proxy_bytes / proxy_budget_bytes "
                    f"/ proxy_budget_exceeded in .meta.json (rc={rc})",
                    rc == 0 and meta.get("proxy_bytes") == 123_456
                    and meta.get("proxy_budget_bytes") == 10_000_000
                    and meta.get("proxy_budget_exceeded") is False)
        ok &= _savers_check("...and the same in .latest_attempt.meta.json",
                    attempt.get("proxy_bytes") == 123_456
                    and attempt.get("status") == meta.get("status"))

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        over = proxy_meter.ProxyMeter(0.0001)
        over.add(5_000)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(_meter_rows(), out,
                               proxy_meter.STOP_BUDGET_EXCEEDED, over.sidecar())
        meta = json.load(open(out + ".meta.json"))
        ok &= _savers_check(f"over budget WITH rows: exit 6, partial, "
                    f"stop_reason proxy_budget_exceeded (rc={rc})",
                    rc == 6 and meta.get("status") == "partial"
                    and meta.get("stop_reason") == "proxy_budget_exceeded"
                    and meta.get("proxy_budget_exceeded") is True)

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish([], out, proxy_meter.STOP_BUDGET_EXCEEDED,
                               over.sidecar())
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
        ok &= _savers_check(f"over budget with NO rows: exit 5 (rc={rc}), the "
                    f"attempt sidecar says so, the previous output is kept",
                    rc == 5 and untouched
                    and attempt.get("stop_reason") == "proxy_budget_exceeded"
                    and attempt.get("proxy_budget_exceeded") is True
                    and attempt.get("status") == "failed")

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            _meter_finish(_meter_rows(), out, "completed", None)
        ok &= _savers_check("an unmetered run writes no attempt sidecar and no "
                    "proxy fields (unchanged)",
                    not os.path.exists(out + ".latest_attempt.meta.json")
                    and "proxy_bytes" not in json.load(
                        open(out + ".meta.json")))

    a = ns(proxy_budget_mb=None)
    setattr(a, "_proxy_meter", metered)
    # Read off the logger calls, not a handler: the root logger's level
    # differs between entry points and would hide an INFO line.
    with mock.patch.object(proxy_meter.logger, "info") as info, \
            mock.patch.object(proxy_meter.logger, "warning") as warn:
        extra = proxy_meter.finish(a, {"site": 1})
    said = [c[0][0] % c[0][1:] for c in
            info.call_args_list + warn.call_args_list]
    ok &= _savers_check(f"the run ends with 'Proxy traffic: X.X MB of Y MB budget' "
                f"({said})",
                "Proxy traffic: 0.1 MB of 10.0 MB budget" in said
                and extra.get("site") == 1 and extra.get("proxy_bytes"))
    return ok


# A request the PAGE makes after the cap. Navigations are stopped by the
# engines' own check() before they are issued; this is what the in-browser
# refusal (a route abort, CDP Network.setBlockedURLs) is for.
_LATE_FETCH = ("fetch('/n').then(r => r.status).catch(e => 'refused')")


def test_proxy_meter_in_a_real_browser(skips):
    """Opt-in (PROXY_METER_BROWSER=1): each installed driver loads a page
    through _ByteCountingProxy, and the meter must agree with what the proxy
    carried. Needs a Chromium; not run in CI, which installs none."""
    _savers_group("Proxy traffic meter in a real browser (opt-in)")
    if os.environ.get("PROXY_METER_BROWSER") != "1":
        print("  not run — set PROXY_METER_BROWSER=1 with a Chromium "
              "installed to measure the meter against a stub proxy")
        return True
    import proxy_meter
    import time as _time
    ok = True
    sizes = {"/": 60_000, "/s.css": 30_000, "/a.js": 90_000, "/n": 40_000}
    chromium = os.environ.get("PROXY_METER_CHROMIUM", "/usr/bin/chromium")
    target = "http://meter.test/"
    ran = 0
    for driver in ("playwright", "pyppeteer", "selenium"):
        try:
            __import__(driver)
        except ImportError:
            continue
        ran += 1
        stub = _ByteCountingProxy(sizes)
        meter = proxy_meter.ProxyMeter(0.15)
        stopped = None
        try:
            if driver == "playwright":
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    b = pw.chromium.launch(headless=True,
                                           proxy={"server": stub.url()})
                    ctx = b.new_context()
                    proxy_meter.install_playwright(ctx, meter)
                    p = ctx.new_page()
                    p.goto(target)
                    p.wait_for_timeout(800)
                    first = (meter.bytes, stub.total)
                    p.evaluate(_LATE_FETCH)
                    p.wait_for_timeout(500)
                    stopped = _base_exception(meter.check)
                    b.close()
            elif driver == "pyppeteer":
                import asyncio
                from pyppeteer import launch

                async def go():
                    b = await launch(headless=True, executablePath=chromium,
                                     args=["--no-sandbox",
                                           f"--proxy-server={stub.url()}"],
                                     handleSIGINT=False, handleSIGTERM=False,
                                     handleSIGHUP=False)
                    p = await b.newPage()
                    await proxy_meter.install_pyppeteer(p, meter)
                    await p.goto(target)
                    await asyncio.sleep(0.8)
                    snap = (meter.bytes, stub.total)
                    await p.evaluate(_LATE_FETCH, force_expr=True)
                    await asyncio.sleep(0.5)
                    await b.close()
                    return snap
                first = asyncio.new_event_loop().run_until_complete(go())
                stopped = _base_exception(meter.check)
            else:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                o = Options()
                o.add_argument("--headless=new")
                o.add_argument("--no-sandbox")
                o.add_argument(f"--proxy-server={stub.url()}")
                o.binary_location = chromium
                proxy_meter.selenium_options(o, meter)
                d = webdriver.Chrome(options=o, service=Service(
                    os.environ.get("PROXY_METER_CHROMEDRIVER",
                                   "/usr/bin/chromedriver")))
                proxy_meter.install_selenium(d, meter)
                d.get(target)
                _time.sleep(0.8)
                proxy_meter.drain_selenium(d)
                first = (meter.bytes, stub.total)
                d.execute_script("return " + _LATE_FETCH)
                _time.sleep(0.5)
                proxy_meter.drain_selenium(d)
                stopped = _base_exception(meter.check)
                d.quit()
        except Exception as e:  # noqa: BLE001 — report by name
            ok &= _savers_check(f"{driver}: real-browser meter run ({e!r:.160})", False)
            stub.close()
            continue
        stub.close()
        metered, carried = first
        drift = abs(metered - carried) / max(1, carried)
        ok &= _savers_check(f"{driver}: page + css + js through the stub proxy: "
                    f"metered {metered} B, the proxy carried {carried} B "
                    f"({drift:.1%}, tolerance 10%)", drift <= 0.10)
        ok &= _savers_check(f"{driver}: past the 0.15 MB budget a request the page "
                    f"makes never reaches the proxy, and check() stops the "
                    f"run before its next navigation (proxy saw {stub.paths})",
                    isinstance(stopped, proxy_meter.ProxyBudgetExceeded)
                    and "/a.js" in stub.paths and "/n" not in stub.paths)
    if not ran:
        print("  no browser driver installed")
    return ok


def _meter_rows():
    rows, seen = [], set()
    for r in P.parse_products(LISTING_HTML, LISTING_URL, page=1):
        if r.sku not in seen:
            seen.add(r.sku)
            rows.append(r)
    return rows[:3]


def _meter_finish(rows, out, stop_reason, proxy_extra):
    extra = {"site_fact": 1}
    if proxy_extra:
        extra.update(proxy_extra)
    return output_writer.finish_run(rows, out, "json", False, blocked=False,
                      stop_reason=stop_reason, pages_requested=2,
                      pages_completed=1 if rows else 0,
                      start_url=_SAVERS_URL, final_url=_SAVERS_URL,
                      mode="listing", extra=extra)


def _engine_meter_installed(engine, args, pool):
    """Open the engine's local session on fakes; True if the meter went in."""
    from unittest import mock
    name = engine.__name__
    with _captured_output():
        if name == "playwright_scraper":
            pw = mock.MagicMock()
            browser = pw.chromium.launch.return_value
            browser.version = "120.0.0.0"
            ctx = browser.new_context.return_value
            engine._launch_local(pw, args, pool)
            return any(c[0][0] == "requestfinished" for c in ctx.on.call_args_list)
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                driver = chrome.return_value
                driver.capabilities = {"browserVersion": "120.0.0.0"}
                driver.get_log.return_value = []
                engine._Session(args, pool).open()
            return getattr(driver, "_proxy_meter_counter", None) is not None \
                and not isinstance(driver._proxy_meter_counter, mock.MagicMock)
        page = mock.MagicMock()
        for attr in ("setUserAgent", "setViewport", "authenticate",
                     "setRequestInterception", "evaluateOnNewDocument",
                     "setExtraHTTPHeaders"):
            setattr(page, attr, mock.AsyncMock())
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        browser = mock.MagicMock()
        browser.newPage = mock.AsyncMock(return_value=page)
        browser.version = mock.AsyncMock(return_value="HeadlessChrome/120.0")

        async def fake_launch(*a, **k):
            return browser
        bridge = engine._AsyncBridge()
        try:
            with mock.patch.object(engine, "launch", fake_launch):
                engine._Session(bridge, args, pool).open()
        finally:
            closer = getattr(bridge, "close", None)
            if closer:
                closer()
        return any(c[0][0] == "Network.enable" for c in cdp.send.await_args_list)


def _engine_session_mocks(engine):
    """Patch out everything in `scrape` that would start a browser."""
    from unittest import mock
    name = engine.__name__
    if name == "playwright_scraper":
        return [mock.patch.object(engine, "sync_playwright"),
                mock.patch.object(engine, "_BrowserSession"),
                mock.patch.object(engine, "_plan_or_chain",
                                  return_value=[_SAVERS_URL + "?page=2",
                                                _SAVERS_URL + "?page=3"])]
    if name == "puppeteer_scraper":
        return [mock.patch.object(engine, "_AsyncBridge"),
                mock.patch.object(engine, "_Session"),
                mock.patch.object(engine, "_next_page_href", return_value=None),
                mock.patch.object(engine.page_flow, "pagination_is_addressable",
                                  return_value=True)]
    return [mock.patch.object(engine, "_Session"),
            mock.patch.object(engine, "_next_page_href", return_value=None),
            mock.patch.object(engine.page_flow, "pagination_is_addressable",
                              return_value=True)]


def _navigation_mock(engine, session):
    name = engine.__name__
    if name == "playwright_scraper":
        return session.page.goto
    if name == "puppeteer_scraper":
        return session.bridge.run
    return session.driver.get


def _drive_budget_run(engine, tmp, rows_on_page_1):
    """scrape() with the page fetch faked: page 1 spends past the budget,
    page 2 goes through the meter's check like a real fetch does."""
    import proxy_meter
    from unittest import mock
    args = _engine_args(engine, tmp, "http://127.0.0.1:" + str(_closed_port()),
                        "--pages", "3", "--proxy-budget-mb", "0.01",
                        "--delay", "0", "--format", "json")
    calls = []

    def fake_fetch(session, a, pool, page_num, url):
        calls.append(page_num)
        proxy_meter.check(a)
        proxy_meter.of(a).add(20_000)
        if not rows_on_page_1:
            proxy_meter.check(a)
        return engine.PageOutcome(page_num=page_num, url=url or _SAVERS_URL,
                                  final_url=_SAVERS_URL,
                                  rows=_meter_rows(), state="ok")

    with contextlib.ExitStack() as stack:
        for patch in _engine_session_mocks(engine):
            stack.enter_context(patch)
        stack.enter_context(mock.patch.object(engine, "_fetch_one_page",
                                              side_effect=fake_fetch))
        stack.enter_context(mock.patch.object(engine.time, "sleep"))
        buf = stack.enter_context(_captured_output())
        try:
            rc = engine.scrape(args)
        except BaseException as e:  # noqa: BLE001 — fail BY NAME
            rc = f"raised {type(e).__name__}"
    out = os.path.join(tmp, "out")
    meta = (json.load(open(out + ".meta.json"))
            if os.path.exists(out + ".meta.json") else {})
    attempt = (json.load(open(out + ".latest_attempt.meta.json"))
               if os.path.exists(out + ".latest_attempt.meta.json") else {})
    return rc, calls, meta, attempt, buf.getvalue()


def test_proxy_meter_in_the_engines(skips):
    _savers_group("Proxy traffic meter: wired into every engine")
    import proxy_meter
    from unittest import mock
    ok = True
    for name in ENGINE_NAMES:
        try:
            engine = __import__(name)
        except ImportError as e:
            skips.append(f"{name} proxy meter driving — {e}")
            print("  SKIP  %s proxy meter — %s" % (name, e))
            continue
        for label, proxy, extra, want in (
                ("through a proxy", "http://127.0.0.1:1", (), True),
                ("without a proxy", None, (), False)):
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy, *extra)
            pool = ProxyPool([proxy]) if proxy else None
            proxy_meter.start(args, pool, remote=False)
            got = _engine_meter_installed(engine, args, pool)
            ok &= _savers_check(f"{name}: meter installed {label} = {want} (got {got})",
                        got is want)

        # The real page fetch refuses to navigate once over budget.
        with tempfile.TemporaryDirectory() as tmp:
            args = _engine_args(engine, tmp, "http://127.0.0.1:1",
                                "--proxy-budget-mb", "0.001")
        meter = proxy_meter.start(args, ProxyPool(["http://127.0.0.1:1"]))
        meter.add(5_000)
        session = mock.MagicMock()
        session.driver.get_log.return_value = []
        with _captured_output():
            err = _base_exception(lambda: engine._fetch_one_page(
                session, args, None, 1, _SAVERS_URL))
        ok &= _savers_check(f"{name}: over budget, the page fetch stops before "
                    f"navigating ({type(err).__name__})",
                    isinstance(err, proxy_meter.ProxyBudgetExceeded)
                    and not _navigation_mock(engine, session).called)

        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, meta, attempt, log = _drive_budget_run(engine, tmp, True)
        ok &= _savers_check(f"{name}: budget crossed on page 1 of 3 → exit 6, rows "
                    f"kept, no third page (rc={rc}, pages={calls})",
                    rc == 6 and calls == [1, 2] and meta.get("products") == 3)
        ok &= _savers_check(f"{name}: ...stop_reason proxy_budget_exceeded and the "
                    f"proxy fields in both sidecars",
                    meta.get("stop_reason") == "proxy_budget_exceeded"
                    and meta.get("proxy_budget_exceeded") is True
                    and meta.get("proxy_bytes") == 20_000
                    and meta.get("proxy_budget_bytes") == 10_000
                    and attempt.get("proxy_bytes") == 20_000)
        ok &= _savers_check(f"{name}: ...and the log says 'Proxy traffic: 0.0 MB of "
                    f"0.0 MB budget'", "Proxy traffic: 0.0 MB of 0.0 MB budget"
                    in log)
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, meta, attempt, log = _drive_budget_run(engine, tmp, False)
        ok &= _savers_check(f"{name}: budget crossed before any row → exit 5, no "
                    f"output, the attempt sidecar says why (rc={rc})",
                    rc == 5 and not meta
                    and attempt.get("stop_reason") == "proxy_budget_exceeded"
                    and attempt.get("proxy_bytes") == 20_000)
    return ok


def test_canary_reports_proxy_traffic():
    _savers_group("Canary: proxy traffic in the step summary, budget enforced")
    import subprocess
    ok = True
    path = os.path.join(str(ROOT), ".github", "workflows", "canary.yml")
    if not os.path.exists(path):
        # The image build copies the code and the suite, not .github.
        print("  not run — no .github/workflows in this tree")
        return True
    text = open(path, encoding="utf-8").read()
    steps = re.split(r"\n\s*- (?:name|uses):", text)
    runs = [s for s in steps if re.search(r"\w+_scraper\.py", s)
            and "--out" in s]
    ok &= _savers_check(f"every canary scrape passes --proxy-budget-mb 10 "
                f"({len(runs)} run step(s))", bool(runs) and all(
                    "--proxy-budget-mb 10" in r for r in runs))
    outs = set()
    for r in runs:
        outs.update(re.findall(r"--out\s+(\S+)", r))
    reporters = [s for s in steps if ".github/proxy_traffic.py" in s
                 and "!cancelled()" in s]
    reported = set()
    for s in reporters:
        reported.update(re.findall(r"proxy_traffic\.py\s+\S+\s+(\S+)", s))
    ok &= _savers_check(f"each run's proxy traffic goes to the step summary, even "
                f"after a failed step (runs {sorted(outs)}, reported "
                f"{sorted(reported)})", bool(outs) and outs <= reported)

    script = os.path.join(str(ROOT), ".github", "proxy_traffic.py")
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        results = {}
        for case, meta in (
                ("over", {"proxy_bytes": 12_300_000,
                          "proxy_budget_bytes": 10_000_000,
                          "proxy_budget_exceeded": True}),
                ("under", {"proxy_bytes": 2_340_000,
                           "proxy_budget_bytes": 10_000_000,
                           "proxy_budget_exceeded": False}),
                ("plain", {"status": "complete"}),
                ("missing", None)):
            out = os.path.join(tmp, case)
            if meta is not None:
                with open(out + ".latest_attempt.meta.json", "w") as f:
                    json.dump(meta, f)
            proc = subprocess.run(
                [sys.executable, script, "live/" + case, out],
                capture_output=True, text=True,
                env={**os.environ, "GITHUB_STEP_SUMMARY": summary})
            results[case] = (proc.returncode, proc.stdout)
        written = open(summary).read() if os.path.exists(summary) else ""
    rc, out = results["over"]
    ok &= _savers_check(f"over budget: '::error::stopped: proxy budget 10 MB "
                f"exceeded' and the step fails (rc={rc})", rc == 1
                and "::error::stopped: proxy budget 10 MB exceeded" in out)
    ok &= _savers_check("under budget: '**live/under**: proxy traffic 2.3 MB of "
                "10 MB' in the summary, step passes",
                results["under"][0] == 0
                and "**live/under**: proxy traffic 2.3 MB of 10 MB" in written
                and "**live/over**: proxy traffic 12.3 MB of 10 MB" in written)
    ok &= _savers_check("no proxy fields / no sidecar: reported, not a crash",
                results["plain"][0] == 0 and results["missing"][0] == 0
                and "not metered" in written and "unknown" in written)
    return ok


def test_proxy_meter_in_the_http_client(skips):
    _savers_group("Proxy traffic meter: http_scraper.py (requests, no browser)")
    import proxy_meter
    import http_scraper
    from unittest import mock
    ok = True
    page = "/categories/mattresses"
    stub = _ByteCountingProxy({page: 30_000})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            args = _engine_args(http_scraper, tmp, stub.url(), "--pages", "1",
                                "--retries", "1", "--proxy-budget-mb", "0")
            # Plain http, so the stub proxy serves the page itself instead of
            # tunnelling it; the pre-check's HEAD goes to the stub too.
            args.url = "http://www.sleepnumber.com" + page
            with _captured_output():
                http_scraper.scrape(args)
            att = os.path.join(tmp, "out.latest_attempt.meta.json")
            meta = json.load(open(att)) if os.path.exists(att) else {}
        carried = stub.total
        got = meta.get("proxy_bytes") or 0
        drift = abs(got - carried) / max(1, carried)
        ok &= _savers_check(f"http_scraper through a stub proxy: metered {got} B, the "
                    f"proxy carried {carried} B including the pre-check "
                    f"({drift:.1%}, tolerance 5%)", got and drift <= 0.05)
        ok &= _savers_check("http_scraper: --proxy-budget-mb 0 is no cap "
                    "(proxy_budget_bytes null, not exceeded)",
                    meta.get("proxy_budget_bytes", "x") is None
                    and meta.get("proxy_budget_exceeded") is False)
    finally:
        stub.close()

    stub = _ByteCountingProxy({page: 30_000})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            args = _engine_args(http_scraper, tmp, stub.url(), "--pages", "3",
                                "--retries", "3", "--retry-delay", "0",
                                "--delay", "0", "--proxy-budget-mb", "0.01")
            args.url = "http://www.sleepnumber.com" + page
            with _captured_output():
                rc = http_scraper.scrape(args)
            att = os.path.join(tmp, "out.latest_attempt.meta.json")
            meta = json.load(open(att)) if os.path.exists(att) else {}
        ok &= _savers_check(f"http_scraper: a 30 kB page past a 10 kB budget, no rows "
                    f"→ exit 5, stop_reason proxy_budget_exceeded, ONE page "
                    f"request (rc={rc}, proxy saw {stub.paths})",
                    rc == 5 and meta.get("stop_reason") == "proxy_budget_exceeded"
                    and stub.paths.count(page) == 1)
    finally:
        stub.close()

    # The real page fetch: over budget, no request (and no retry) is made.
    with tempfile.TemporaryDirectory() as tmp:
        args = _engine_args(http_scraper, tmp, "http://127.0.0.1:1",
                            "--proxy-budget-mb", "0.001")
    proxy_meter.start(args, ProxyPool(["http://127.0.0.1:1"])).add(5_000)
    session = mock.MagicMock()
    with _captured_output():
        err = _base_exception(lambda: http_scraper._fetch_one_page(
            session, args, None, 1, _SAVERS_URL))
    ok &= _savers_check(f"http_scraper: over budget, the page fetch stops before any "
                f"request ({type(err).__name__})",
                isinstance(err, proxy_meter.ProxyBudgetExceeded)
                and not session.get.called)

    # Rows kept: page 1 parses, spends past the budget; page 2 is refused.
    with tempfile.TemporaryDirectory() as tmp:
        args = _engine_args(http_scraper, tmp, "http://127.0.0.1:" + str(_closed_port()),
                            "--pages", "3", "--delay", "0", "--format", "json",
                            "--proxy-budget-mb", "0.01")
        calls = []

        def fake_fetch(session, a, pool, page_num, url):
            calls.append(page_num)
            proxy_meter.check(a)
            proxy_meter.of(a).add(20_000)
            return http_scraper.PageOutcome(page_num=page_num, url=url,
                                            final_url=url, rows=_meter_rows(),
                                            state="ok")
        with mock.patch.object(http_scraper, "_fetch_one_page", side_effect=fake_fetch), \
                mock.patch.object(http_scraper.page_flow, "plan_pages",
                                  return_value=[_SAVERS_URL + "?page=2",
                                                _SAVERS_URL + "?page=3"]), \
                mock.patch.object(http_scraper.time, "sleep"), _captured_output():
            rc = http_scraper.scrape(args)
        out = os.path.join(tmp, "out")
        meta = json.load(open(out + ".meta.json")) if os.path.exists(out + ".meta.json") else {}
    ok &= _savers_check(f"http_scraper: budget crossed on page 1 of 3 → exit 6, rows kept, "
                f"no third page (rc={rc}, pages={calls})",
                rc == 6 and calls == [1, 2] and meta.get("products") == 3
                and meta.get("stop_reason") == "proxy_budget_exceeded"
                and meta.get("proxy_bytes") == 20_000)
    return ok



def test_proxy_meter_stops_concurrent_workers(skips):
    _savers_group("Proxy traffic meter: --concurrency workers share one budget")
    import proxy_meter
    from unittest import mock
    if PW is None:
        return True
    with tempfile.TemporaryDirectory() as tmp:
        args = _engine_args(PW, tmp, "http://127.0.0.1:1", "--proxy-budget-mb", "0.01",
                            "--delay", "0")
    meter = proxy_meter.start(args, ProxyPool(["http://127.0.0.1:1"]))
    fetched = []

    def fake_fetch(session, a, pool, page_num, url):
        proxy_meter.check(a)
        fetched.append(page_num)
        meter.add(20_000)
        # The budget is crossed mid-page: the real fetch's next retry check
        # raises inside the worker thread, which must catch it.
        proxy_meter.check(a)
        return PW.PageOutcome(page_num=page_num, url=url, final_url=url,
                              rows=_meter_rows(), state="ok")
    specs = [(n, f"{_SAVERS_URL}?page={n}") for n in range(2, 8)]
    with mock.patch.object(PW, "sync_playwright"), \
            mock.patch.object(PW, "_BrowserSession"), \
            mock.patch.object(PW, "_fetch_one_page", side_effect=fake_fetch), \
            mock.patch.object(PW.time, "sleep"), _captured_output() as buf, \
            redirect_stderr(buf):
        try:
            results, unattempted, _ = PW._fetch_pages_concurrently(
                args, ProxyPool(["http://127.0.0.1:1", "http://127.0.0.1:2"]), specs, 2)
            raised = None
        except BaseException as e:  # noqa: BLE001 — fail BY NAME
            raised, results, unattempted = e, [], []
    return _savers_check(
        f"two workers stop once the shared budget is spent: fetched {sorted(fetched)}, "
        f"unattempted {unattempted}, nothing escaped ({type(raised).__name__})",
        raised is None and len(fetched) <= 2 and len(unattempted) >= 4
        and "Traceback" not in buf.getvalue()
        and "stopped: proxy budget exceeded" in buf.getvalue())

test_proxy_traffic_savers(_SaversSkips(), _savers_check, _savers_group)
test_proxy_meter_units(_SaversSkips())
test_proxy_meter_in_the_engines(_SaversSkips())
test_proxy_meter_in_the_http_client(_SaversSkips())
test_proxy_meter_stops_concurrent_workers(_SaversSkips())
test_proxy_meter_in_a_real_browser(_SaversSkips())
test_canary_reports_proxy_traffic()


def main() -> int:
    total = len(PASSES) + len(FAILURES)
    print()
    print(f"{len(PASSES)}/{total} checks passed"
          + (f", {len(SKIPS)} group(s) skipped" if SKIPS else ""))
    for group, reason in SKIPS:
        print(f"  skipped: {group} ({reason})")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED:")
        for name, err in FAILURES:
            print(f"  {name}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
