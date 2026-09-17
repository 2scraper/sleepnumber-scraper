#!/usr/bin/env python3
"""smoke_test.py — the offline suite.

One file of plain functions with inline fixtures, plus the real captures in
`fixtures_generated.json` (CLAUDE.md §10). No pytest, no conftest, no
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
  back (§18).
* The site's own JSON-LD points at `sndotcom.fly.dev`, its Fly origin.
  Read verbatim, every row links to a dev host (§4's "product URL under
  offers.url" with a different face).
* `?page=2` and `?page=99` both return page ONE, HTTP 200. A count-based
  pagination loop would never terminate (§7).
* `regular` equals `sale` on everything not discounted, so copying it into
  `original_price` asserts a was-price on every undiscounted row (§4).
* `review_stats` reports 0 for an unreviewed product, and a 0 written
  through drags every average a consumer computes (§21).
* A CloudFront refusal reaches a parser in two encodings — HTTP-client bytes
  and browser DOM — and the suite pins both (§20).
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
    """price_source records WHICH views agreed, and the detail fixture
    carries the site's own real JSON-LD Product so this is a real agreement
    and not a synthetic one."""
    rows = P.parse_products(DETAIL_HTML, DETAIL_URL)
    sources = {r.price_source for r in rows}
    assert "payload+jsonld" in sources, (
        f"no row was confirmed against JSON-LD; saw {sources}")
    assert "payload_jsonld_disagree" not in sources


@check
def test_no_original_price_at_or_below_its_price():
    """The one-line canary assertion from §4, which costs nothing and catches
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
    left the suite green, which is §22's "a control that proves nothing".
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
    silently lost data in a sibling repo (CLAUDE.md §4)."""
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
# 4. block detection — §18's counts, pinned
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

# The SAME refusal after a browser has parsed and re-serialised it. §20: a
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
    """§18, and the reason this repo's marker list is two entries long.

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
    (tokopedia-scraper, §18)."""
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

    §21: a guard is only as good as the fixture it runs against, and the
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
    sends them to the parser. They are different facts (§20)."""
    html = ('<html><body><a href="/collections/mattresses-climate-collection/QT8">x</a>'
            '</body></html>')
    assert page_flow.is_broken_parse("content", html, 0) is True
    assert page_flow.is_broken_parse("content", html, 5) is False
    assert page_flow.is_broken_parse("empty", html, 0) is False
    # A genuinely empty category links to no product and is NOT this.
    assert page_flow.is_broken_parse("content", "<html><body>none</body></html>", 0) is False


@check
def test_classify_takes_status_as_a_keyword():
    """farfetch-scraper shipped `classify(html, status, url)` positionally
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
    """§22: a statement after return/raise/break/continue in the SAME block.

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
    """§10: `compileall` proves a file PARSES, not that its names RESOLVE.

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
    """§17's check #1, with §22's correction.

    It earns its keep: a sibling shipped `classify(html, status, url)` while
    two of three engines called it `classify(html, url=…)`, and BOTH crashed
    on their first fetch — invisible to import, --help, compileall, the
    undefined-name walk above and four hundred green assertions, because none
    of those calls a function the way a live run does.

    §22's correction is the important part: resolving the callee with
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
    """§17: a policy constant nothing reads is the same defect as dead code,
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
    """§12. The phrases are ASSEMBLED from pieces rather than written out, so
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
                f"'Scraping Browser API' instead (CLAUDE.md §12).")


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
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    read = set(env_config.ENV_KEYS)
    assert documented == read, (
        f"documented but never read: {sorted(documented - read)}; "
        f"read but never documented: {sorted(read - documented)}")


@check
def test_a_copied_env_example_reads_as_unset():
    """§17: `cp .env.example .env` then a run must NOT send `{login}-zone-…`
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
    """§3: the loader only fills UNSET values, so a variable mapped onto a
    flag with a non-empty default is silently inert — a setting that looks
    configurable and is not."""
    assert "SLEEPNUMBER_OUT" not in env_config.ENV_KEYS, (
        "--out has a non-empty default; a variable mapped onto it can never "
        "take effect")


@check
def test_no_credentials_committed():
    """The repo's own scan, invoked from here as well as from CI — one
    implementation, two callers.

    §17: this family shipped TWO sources of truth for this, one dead (a
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
    """§17: two sources of truth, one dead and one holed. The workflow must
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
    """§10/§11: three repos in this family shipped an image that died with
    ModuleNotFoundError on EVERY invocation, `--help` included, because one
    module the engine imports at module level was missing from the COPY list.
    Nothing built the image, so nothing noticed. This needs no Docker."""
    dockerfile = ROOT / "Dockerfile"
    if not dockerfile.exists():
        return
    # Join backslash continuations FIRST. Read line by line, a COPY whose
    # file list wraps — which every readable one does — loses everything
    # after the first line, and the check then reports the modules on the
    # continuation as missing. That is the §22 question asked of this check
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
# Flags every engine must take. §9's contract plus the five this family's own
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
# IS the documentation: §20's lesson is that this check must fail in BOTH
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
    """§20: written only after it was skipped once and then found TWELVE
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
    """§10: for "the suite passes with no engine installed" to MEAN anything,
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
    """§8, and an EXCEPTION MESSAGE is a log. Masking must be GLOBAL: a
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
