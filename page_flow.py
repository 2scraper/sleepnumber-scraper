"""
page_flow.py
------------
Sleep Number's page-state policy and pagination rules, shared by all three
engines so they cannot quietly disagree about whether a page is worth
retrying or worth paying for.

Two modes:

    listing   a /categories/… or /collections/… page. One row per size
              variant of every product on it.
    product   a /products/… detail page. Same rows, one model's worth.

Why this module exists rather than three copies of an if-chain: a request
here can come back five different ways and four of them want a different
response.

    content    the application rendered and its payload holds products
    blocked    CloudFront refused before the request reached Sleep Number
    notfound   the site's OWN 404 — a real page, correctly answered
    empty      a real category with nothing in it
    unknown    served, but nothing recognisable — worth one more try

`STATE_POLICY` states the triage as DATA. Three engines reading the same
table cannot drift the way three if-chains do — which is the failure this
family has already paid for, in the shape of one engine reporting exit 3
where its twin reported exit 0 on the same page.

WHAT THIS MODULE DELIBERATELY DOES NOT CONTAIN
-----------------------------------------------
No scrolling and no lazy-load hydration wait. Sleep Number's catalogue does
not arrive by XHR after paint: it is inlined in the server's response as the
React Router hydration payload, complete, before any script runs. Measured
on every capture in this repo — a category page fetched with a plain HTTP
client parses to exactly the same rows a browser produces, 52 of 52 on the
mattresses category.

That is worth stating plainly because it inverts the usual reason for this
family's three engines. What a browser buys HERE is a normal TCP/TLS
fingerprint and a cookie jar, not rendering. See the README's "Do you need a
browser at all?".

No JavaScript crosses this boundary either: Selenium's `execute_script`
takes a function BODY with an explicit `return` while Playwright and
pyppeteer take `() => expr`, so the shared module names the OPERATION and
each engine passes its own driver's primitive in.
"""

import logging
import re
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from product_parser import (detect_page_state, page_url, pagination,
                            has_next_page, parse_products, CANONICAL_HOST)

logger = logging.getLogger("page_flow")


MODES = ("listing", "product")

# Only a listing has more than one page even in principle. A detail page is
# one model; asking for --pages 3 of it is a usage error, not a fetch.
PAGINATED_MODES = ("listing",)

# NONE. Not an oversight — a measurement.
#
# `--concurrency` hands page N to a worker that has not fetched pages
# 1..N-1, which requires page N to have its own address. On this site it does
# not: `?page=2` and `?page=99` both answer HTTP 200 carrying page ONE (10
# products, and the payload's own `page` field still reads 1). Workers would
# therefore fetch the same page in parallel and the run would report N pages
# of the first page's data.
#
# It is also moot at the current catalogue size: `per_page` is 100 and the
# largest category measured holds 20 products, so every listing is one page.
# The flag is accepted and REFUSED WITH THE REASON rather than silently
# ignored — a flag that looks available and does nothing is the shape of bug
# this family keeps finding.
CONCURRENCY_CAPABLE_MODES: Sequence[str] = ()

# What "this page has painted" means per mode, for the engines' readiness
# wait. Anchored on the hydration payload's own script rather than on a CSS
# class: the classes here are build-generated and churn, and the payload is
# what extraction actually reads — so waiting for anything else would be
# waiting for the wrong thing.
#
# NOTE FOR ALL THREE ENGINES: never hand this to a function that EVALUATES A
# STRING. tokopedia-scraper's run died with `EvalError … violates the
# following Content Security Policy directive` because Playwright's
# `wait_for_function` evaluates a string, and the site's CSP had no
# `unsafe-eval`. Sleep Number's CSP has not been measured as hostile, but the
# cheap habit is to poll `querySelectorAll` through the protocol instead,
# which works under any CSP and spells the same in all three drivers.
READY_SELECTORS = {
    "listing": "script",
    "product": "script",
}

# How many matches mean "rendered". Must be > 1: waiting for a single match
# resolves on an unrelated element long before the thing you want exists.
# Every page this site serves carries at least five `<script>` tags, and a
# CloudFront refusal carries none at all.
MIN_ROW_MATCHES = {"listing": 3, "product": 3}

CONTENT_TIMEOUT_MS = {"listing": 20_000, "product": 20_000}


def ready_selector(mode: str) -> str:
    return READY_SELECTORS.get(mode, "script")


def min_matches(mode: str) -> int:
    return MIN_ROW_MATCHES.get(mode, 3)


def ready_count(mode: str) -> int:
    """Alias kept because every engine in this family calls it by this name.

    Thin on purpose: smoke_test.py binds each engine's call sites against
    these signatures, and a name that exists in one engine and not the
    shared module is exactly the drift that check exists to catch.
    """
    return min_matches(mode)


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20_000)


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, want: int, timeout_ms: int,
                   poll_ms: int = 400) -> int:
    """Poll `count(selector)` until it reaches `want` or the budget runs out.

    Driven through a callable so no JavaScript crosses this boundary, and
    bounded because every remote call in this family is. Returns the last
    count seen — the CALLER decides what a shortfall means, because "fewer
    than expected" is a different fact from "nothing at all".
    """
    waited, seen = 0, 0
    while waited < timeout_ms:
        try:
            seen = count(selector)
        except Exception:                      # a driver error mid-poll
            seen = 0
        if seen >= want:
            return seen
        sleep(poll_ms)
        waited += poll_ms
    return seen


# How much of a page's rows must carry a price before the engines stop
# treating the shortfall as normal.
#
# Set high — higher than most of this family — because it is measured high:
# every sellable variant on this site publishes a price, 52 of 52 on
# /categories/mattresses, 211 of 211 on /categories/sheets-pillowcases and
# 169 of 169 on /categories/furniture. There is no legitimate unpriced row
# here the way a football transfer legitimately has no fee, so a dip below
# this floor is a parsing regression and not the catalogue's own doing.
#
# 0.98 rather than 1.0 leaves room for a single genuinely odd row (a
# discontinued size the site still lists) to warn rather than cry wolf.
PRICE_COVERAGE_FLOOR = 0.98


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(html: Optional[str], *, status: Optional[int] = None,
             url: str = "", headers: Optional[Dict[str, str]] = None) -> str:
    """One of content | blocked | notfound | empty | unknown.

    `status` is keyword-only ON PURPOSE. farfetch-scraper shipped
    `classify(html, status, url)` taking status positionally while two of its
    three engines called it `classify(html, url=…)`, and both crashed on
    their FIRST fetch — invisible to import, --help, compileall and four
    hundred green assertions, because none of those calls a function the way
    a live run does. Keyword-only makes that particular mistake impossible to
    make silently, and smoke_test.py binds every engine's call site against
    this signature besides.
    """
    if html is None:
        return "unknown"
    return detect_page_state(html, status, url, headers)


STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content": {"retry": False, "solve": False, "blocked": False},
    # A CloudFront refusal is worth retrying because it is a property of the
    # exit address, and this family rotates exits between attempts. It is NOT
    # worth solving: the refusal measured on this site carries no
    # `x-amzn-waf-action` header and no challenge widget of any kind — there
    # is nothing on the page for any solver to work on. See captcha_solver.py
    # for what that does and does not license as a claim.
    "blocked": {"retry": True, "solve": False, "blocked": True},
    # Kept even though no challenge has been observed on this site, because
    # detection stays broad in this family by deliberate decision: a different
    # geo or a different traffic pattern surfaces different defences.
    "captcha": {"retry": True, "solve": True, "blocked": True},
    # NOT retried. The site answered correctly and the answer was "no such
    # page". Retrying it spends the user's retry budget on a certainty.
    "notfound": {"retry": False, "solve": False, "blocked": False},
    # NOT retried. A real category with nothing in it is a correct answer to
    # the question that was asked.
    "empty": {"retry": False, "solve": False, "blocked": False},
    # Served, but unrecognised. Worth one more try — it is the only state
    # where a transient really is the likeliest explanation.
    "unknown": {"retry": True, "solve": False, "blocked": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


# ---------------------------------------------------------------------------
# A parsed page that should not have been empty
# ---------------------------------------------------------------------------

def is_broken_parse(state: str, html: str, row_count: int) -> bool:
    """A page that was SERVED, links to products, and parsed to zero rows.

    That is our bug, not an empty category, and reporting it as "0 products"
    sends the reader to check the URL instead of the parser. farfetch-scraper
    gave it its own stop_reason for exactly this reason.

    It can genuinely fire here: the URL-pattern fallback emits a row for
    every `/collections/{slug}/{SKU}` link it finds, so zero rows beside a
    page carrying such links means the payload did not decode AND the
    fallback found nothing — two failures, not an empty shelf.
    """
    if state != "content" or row_count:
        return False
    return bool(re.search(r"/collections/[a-z0-9-]+/[A-Z0-9]{2,12}", html or ""))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def comparable(url: str) -> str:
    """A URL reduced to what makes two fetches the SAME fetch.

    Used to answer "did the site's own next-link agree with the convention
    `page_url()` would build". Scheme and host are normalised away because
    the site's structured data leaks its origin hostname (see
    product_parser), and a trailing slash is not a different page.
    """
    parts = urlsplit(url or "")
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True))
    path = (parts.path or "/").rstrip("/") or "/"
    return urlunsplit(("", "", path, urlencode(query), ""))


def pagination_is_addressable(mode: str, page1_html: str, page1_url: str) -> bool:
    """Can page N be fetched without walking 1..N-1?

    Asked from page 1's own answer rather than asserted, so the day Sleep
    Number starts honouring `?page=` this notices instead of needing a
    rewrite. Today it answers False for every mode, and that is measured
    rather than assumed — see CONCURRENCY_CAPABLE_MODES.
    """
    if mode not in PAGINATED_MODES:
        return False
    bits = pagination(page1_html or "")
    next_page = bits.get("next_page")
    if not next_page:
        return False
    # The site states a next page. Does the convention reproduce its address?
    built = page_url(page1_url, 2)
    stated = next_page if isinstance(next_page, str) else page_url(page1_url, 2)
    return comparable(built) == comparable(stated)


def plan_pages(page1_html: str, page1_url: str, want: int) -> List[str]:
    """URLs for pages 2..N, planned from the SITE's own arithmetic.

    `total_results` and `per_page` come from the payload, so the page count
    is the site's statement rather than our inference — the same "let the
    site do the arithmetic" layer bbb-scraper used against a result set the
    site capped at 15 pages.

    Returns [] when the site says there is no page 2, which on this site's
    current catalogue is every category.
    """
    if want <= 1:
        return []
    bits = pagination(page1_html or "")
    per_page = bits.get("per_page")
    total = bits.get("total_results")
    available = None
    if isinstance(per_page, int) and per_page > 0 and isinstance(total, int):
        available = max(1, -(-total // per_page))          # ceil
    if not has_next_page(page1_html or ""):
        return []
    last = min(want, available) if available else want
    return [page_url(page1_url, n) for n in range(2, last + 1)]


def listing_arithmetic(page1_html: str) -> Dict[str, Any]:
    """What the SITE says about the size of this result set.

    Recorded in the run's sidecar because "complete" and "exhaustive" are
    different words: a consumer is entitled to know that a run fetched
    everything the site would serve AND how much of the catalogue that was.
    """
    bits = pagination(page1_html or "")
    per_page = bits.get("per_page")
    total = bits.get("total_results")
    pages_available = None
    if isinstance(per_page, int) and per_page > 0 and isinstance(total, int):
        pages_available = max(1, -(-total // per_page))
    return {
        "total_results": total,
        "per_page": per_page,
        "pages_available": pages_available,
        "page_param_honoured": False,   # measured; see CONCURRENCY_CAPABLE_MODES
    }


# How much smaller than page 1 a later page may be before it is worth
# mentioning. Informational only: it NEVER changes an exit code, because a
# short last page is the normal shape of a listing that has run out, and a
# threshold is a weaker signal than a marker (§17's classification-order
# lesson, applied to a warning rather than to a state).
THIN_PAGE_RATIO = 0.4


def is_thin_page(row_count: int, first_page_count: int) -> bool:
    """Is this page suspiciously emptier than page 1?

    Worth a log line and nothing more. On this site it is close to
    unreachable — every category measured fits on one page — but the
    machinery for a catalogue that grows should not have a hole in it where
    the check that notices a partial page would go.
    """
    if not first_page_count or row_count >= first_page_count:
        return False
    return row_count < first_page_count * THIN_PAGE_RATIO
