"""
page_flow.py
------------
Transfermarkt's page-state policy, shared by all three engines.

Four modes, and they do not all paginate the same way — this is the module
that says so in one place instead of three:

    market-values   a ranking table, paginated, but the pagination is NOT a
                     plain query parameter — see "The gallery pagination
                     trap" below. Sequential only.
    club-squad      one club's roster. A single page; there is no page 2.
    player          one profile page. A single page.
    transfers       a ranking-shaped table, paginated with a plain and
                     independently-addressable `?page=N` — the one mode
                     here that supports --concurrency.

Why this module exists at all, when part of this family keeps each engine
self-contained: a page here can answer four different ways and three of them
want a different response.

    content    parse it
    blocked    a network- or vendor-level refusal before any content arrived
    captcha    a challenge widget — the paid path
    empty      a real page with nothing to parse on it — an out-of-range
               page number, or a squad URL for a club with no current season
               data — report it as such, do not retry it and do not go
               looking for a proxy problem

Three copies of that triage would drift, exactly as this family's other
members found — see CLAUDE.md's own note on `page_flow.py` and on
`output_writer.finish_run()`.

The gallery pagination trap — and correcting it
-------------------------------------------------
First measured 2026-09-10 against
`/spieler-statistik/wertvollstespieler/marktwertetop`, and originally
written up here as THE shape of this mode's pagination:

  * The page's own `<link rel="next">` and its "2" pagination link both
    carry a DOUBLED path segment — `.../galerie/{uuid}/page/2//page/2` —
    which 404s verbatim.
  * Fixing the doubled segment by hand (`.../galerie/{uuid}/page/2`, single
    occurrence) does NOT fix the problem either: fetched as a fresh,
    cookie-less request, it returns PAGE 1's content again, not page 2.

That capture was real, and is kept as a fixture (see smoke_test.py) — this
is not a retraction of the observation. What WAS wrong is what this module
concluded from ONE capture: that this is the deterministic shape of every
market-values request, and that `pagination_is_addressable()` should
therefore hardcode `False` for this mode unconditionally, the one mode this
file special-cased out of the "verify first" check every other paginated
mode gets.

**Re-measured 2026-09-10 in a wider session: 28 consecutive fresh fetches of
the SAME URL all returned the plain, undoubled `?page=N` shape — identical
in form to `/statistik/neuestetransfers` — and only the original single
capture showed the `galerie/{uuid}` shape.** So the gallery shape is real
but INTERMITTENT (roughly 1 in ~29 in that sample — treat that ratio as
indicative, not precise; nothing here explains WHY the site serves one
shape or the other, only that it does), not the page's normal behavior.
And critically: in the plain-shape case, a stateless fetch of the
CONSTRUCTED `?page=2` genuinely works — measured 0 shared player ids
against page 1, 25 rows each, exactly the same independence
`/statistik/neuestetransfers` has always had.

So this mode does not need — and, as originally written, wrongly refused —
the SAME "verify first" mechanism `transfers` already used:
`pagination_is_addressable()` now runs the identical check for every
paginated mode, no special case. When page 1's own next-link agrees with
what `page_url()` would build (the common case, per the re-measurement
above), this mode gets the same fast, independently-addressable path
`transfers` does. When it does not agree (the rare gallery case), the
function correctly returns False and the caller falls back to chaining the
site's own link one page at a time within a single session — the ORIGINAL
safe behavior, now used only when it is actually needed rather than on
every run. Nothing about the fallback path changed; only the gate deciding
when to use it did.

`--concurrency` remains `transfers`-only regardless (see
`CONCURRENCY_CAPABLE_MODES` in each engine) — not because market-values
pagination is unaddressable (it usually is, per above), but because this
project has not tested N parallel workers each independently hitting a page
that intermittently switches shape. The planned-vs-chained decision above
is safe to make per run because it happens once, from page 1's own answer;
running several workers concurrently against a mode that can silently
change shape mid-run is a different, untested risk this repo is not taking
on speculatively. Revisit if a live run's data supports it.

`/statistik/neuestetransfers` was and remains simpler: its `<link
rel="next">` reads `?page=2`, a bare query parameter, and a stateless fetch
of `?page=2` measured 0 shared transfer ids against page 1 (25 rows each) —
genuinely, independently addressable on every capture taken of it so far,
which is the one mode `--concurrency` is enabled for.

What this module deliberately does NOT contain
------------------------------------------------
No scrolling, no lazy-load hydration. Every page captured for this repo
(a ranking page, a squad page, a transfer list, a player profile) arrived
as complete server-rendered HTML with every row already in it — there is no
JSON-LD anywhere on this site (checked on all of the above; the only
structured data present is a `schema.org/Person` microdata block on a
player's own profile header, read directly rather than through a script
tag), but there is also nothing to wait for that a DOM-ready check does not
already cover.

The functions here are either pure or driven through small callables, so
each engine passes its own driver's primitives — see the sibling repos'
`page_flow.py` docstrings for why no JavaScript crosses this boundary.
"""

import logging
from typing import Callable, Dict, Optional

from product_parser import detect_page_state, page_url

logger = logging.getLogger("page_flow")


# What "the page has painted" means, per mode. All four modes are
# server-rendered in full on first response (measured — see module
# docstring), so these anchors exist to confirm the right table/header
# arrived, not to wait out a hydration step.
READY_SELECTORS = {
    "market-values": "table.items tbody tr",
    "club-squad": "table.items tbody tr",
    "transfers": "table.items tbody tr",
    "player": "header.data-header",
}

# How many rows must appear before a LISTING counts as loaded rather than as
# a lucky single match — must be > 1, per the family rule, so a wait cannot
# resolve on one unrelated row. 3 against a measured floor of 25 (transfers)
# and 30 (market-values, one ranking page) leaves room for a short final
# page without waiting out the timeout on it. club-squad has no fixed floor
# (a small club's extended squad can be under a dozen), so it also uses 3.
MIN_ROW_MATCHES = {"market-values": 3, "club-squad": 3, "transfers": 3, "player": 0}

CONTENT_TIMEOUT_MS = {"market-values": 20000, "club-squad": 15000,
                      "transfers": 20000, "player": 15000}


def ready_selector(mode: str) -> str:
    return READY_SELECTORS.get(mode, READY_SELECTORS["market-values"])


def min_matches(mode: str) -> int:
    return MIN_ROW_MATCHES.get(mode, 3)


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20000)


def ready_count(mode: str) -> int:
    """How many matches the readiness wait must SEE before it stops.

    `min_matches` is a FLOOR a page must clear -- "more than three rows", not
    "three" -- which is what makes it immune to resolving on one unrelated
    row. `wait_for_count` below is written the other way round, waiting until
    it has seen at least `minimum`, because that is the shape the rest of the
    family uses. This converts between the two in ONE place rather than
    leaving a `+ 1` in each of the three engines, where the first one to lose
    it would wait on a different threshold than the other two and nothing
    offline would notice.
    """
    return min_matches(mode) + 1


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll `selector` until `minimum` elements match, or the budget runs out.

    Polls a COUNT rather than waiting on an evaluated string. Playwright's
    `wait_for_function` and pyppeteer's `waitForFunction` both hand the
    browser a STRING to evaluate, which a site whose CSP lacks `unsafe-eval`
    refuses outright -- on a sibling repo (tokopedia-scraper) that was an
    `EvalError` and exit 1 on the site's most obvious URL, reproducing on one
    of its two listing routes and not the other, because the two are served
    by different renderers with different headers.

    Transfermarkt's own CSP was not the reason this was changed and is not
    being worked around here; this is the eval simply not being invited back
    in when a header changes. `querySelectorAll` through the protocol is a
    CDP call, works under any CSP, and spells the same in all three drivers
    -- which is why all three `_driver()` adapters already expose `count`.

    `count` is expected to swallow its own driver errors and return 0: this
    polls a page that may be navigating under it, and an exception from the
    500th millisecond of a 20-second wait should read as "nothing there yet",
    not take the run down.

    Returns the last count seen, so a caller can tell "painted" from "timed
    out with three of them".
    """
    waited = 0
    seen = count(selector)
    while seen < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        seen = count(selector)
    if seen < minimum:
        logger.info("readiness wait ended at %d/%d matches for %s after %dms",
                    seen, minimum, selector, waited)
    return seen


# A page holding less than this fraction of page 1's own row count is logged
# as thin -- INFORMATIONAL ONLY, never a retry trigger and never a reason to
# change the exit code. A short final page is a correct answer (measured:
# both paginated listings return exactly 25 rows per full page, so a last
# page of a handful is expected, not a fault) -- what this catches is the
# case CLAUDE.md's own family lessons warn about: a page beyond the site's
# real depth, or a markup regression, coming back with only a few rows
# instead of a clean "no new products" the sku-freshness check would have
# caught outright. This repo deliberately does NOT add a numeric PAGE_CAP
# (as catawiki-scraper does) on top of this: the sku-based "no new rows"
# stop condition already terminates on DATA rather than a guessed page
# count, which is the family's own stated preference (CLAUDE.md §7) -- a
# hardcoded cap here would be redundant insurance for a failure mode this
# repo's fresher, sku-driven check already covers.
THIN_PAGE_RATIO = 0.4


def is_thin_page(row_count: int, first_page_count: int) -> bool:
    if first_page_count <= 0:
        return False
    return row_count < first_page_count * THIN_PAGE_RATIO


# A market value is published for almost every row on market-values/
# club-squad (only a free agent or a very old veteran prints "-"), so
# coverage below this floor across a page is a parsing regression to flag,
# not the site's own doing -- matching the family's own "DOM-confirmation
# coverage... warning below 90%" convention (CLAUDE.md §4), loosened slightly
# here since there is no second source to reconcile against on this site (see
# product_parser.py's docstring) and a genuinely elderly/reserve-heavy squad
# page can legitimately run a little under 100%. Deliberately NOT applied to
# --mode transfers: a fee is legitimately absent for a loan, a free transfer
# or an undisclosed fee (see output_writer.Transfer.fee_type), so low price
# coverage there is normal, not a signal.
PRICE_COVERAGE_FLOOR = 0.85


def classify(html: Optional[str], *, status: Optional[int] = None,
             url: Optional[str] = None,
             headers: Optional[Dict[str, str]] = None) -> str:
    """The page's state, as the engines see it. A thin wrapper over
    product_parser.detect_page_state, for the same reason the sibling repos
    keep one: every engine reaches the policy through one name.

    `status` and `headers` are both OPTIONAL because not every engine can
    supply them, and the classifier has to stay correct for the ones that
    cannot. Playwright exposes the response object, and the Scraper API
    returns `{"status", "headers", "body"}` — those two can pass both.
    Selenium and pyppeteer read the rendered DOM through the driver and have
    no response object to ask, so they pass neither, and for them the vendor
    markers are the ONLY thing standing between an AWS WAF captcha and a run
    that reports it as an empty listing. That asymmetry is why A1's markers,
    not this header, are the load-bearing half of the fix.

    Keyword-only after `html` on purpose: `classify(html, status, url)` with
    `status` positional is the exact signature that crashed two of three
    engines in a sibling repo on their first fetch (§17, check 1), because a
    caller wrote `classify(html, url=...)` and bound `url` to `status`.
    """
    return detect_page_state(html or "", status=status, url=url, headers=headers)


# What each state means for the run. Kept as data, not as three copies of an
# if-chain, for the reason CLAUDE.md gives: an engine cannot then quietly
# disagree with its twins about whether a page is worth retrying or paying
# for.
STATE_POLICY = {
    "content": {"retry": False, "solve": False, "blocked": False},
    "blocked": {"retry": True, "solve": False, "blocked": True},
    "captcha": {"retry": True, "solve": True, "blocked": True},
    # NOT retried: an out-of-range page or an empty squad URL is a correct
    # answer to the question that was asked.
    "empty": {"retry": False, "solve": False, "blocked": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("retry", False)


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("solve", False)


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("blocked", False)


def comparable(url: str) -> str:
    """`url` reduced to the parts that decide WHICH PAGE it addresses.

    Unlike the sibling repos, Transfermarkt's own links were not observed to
    carry tracking parameters worth stripping (none were seen across any of
    the captured pages), so this only drops the fragment and normalises
    query-parameter ORDER — a next-link and a constructed URL that agree on
    every parameter but its order must still compare equal.
    """
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    parts = urlparse(url)
    kept = parse_qsl(parts.query, keep_blank_values=True)
    return urlunparse(parts._replace(query=urlencode(sorted(kept)), fragment=""))


def _query_only(url: str) -> str:
    """`url` reduced to JUST its query parameters, order-independent.

    See pagination_is_addressable's "A site can rename its own listing path"
    note for why the PATH is deliberately excluded here even though
    `comparable()` (used elsewhere for exhausted-pagination checks) keeps it.
    """
    from urllib.parse import urlparse, parse_qsl, urlencode
    query = parse_qsl(urlparse(url).query, keep_blank_values=True)
    return urlencode(sorted(query))


def pagination_is_addressable(mode: str, page1_url: str,
                              site_next_url: Optional[str]) -> bool:
    """Whether page N can be fetched without first fetching page N-1.

    The SAME check for every paginated mode, including `market-values` as of
    the first fix here — see "Correcting the gallery pagination trap" in the
    module docstring for why this used to hardcode False for that mode and
    no longer does. True when page 1's own next-link agrees with what
    `page_url()` would build for page 2; a next-link that disagrees means
    the site is not on the plain `?page=N` path THIS run, and this returns
    False so the caller chains the site's own link one page at a time
    instead of silently repeating a page under the wrong label. `club-squad`
    and `player` have no page 2 to ask about and are not routed through this
    function.

    `mode` is kept in the signature for callers and for the case a THIRD
    pagination shape turns up on some other mode later, even though no mode
    currently branches on it here.

    A site can rename its own listing path without changing how pagination
    works
    ----------------------------------------------------------------------
    Measured live 2026-09-12, two days after the gallery-trap
    re-measurement above: `/statistik/neuestetransfers`'s own `<link
    rel="next">` had moved to a DIFFERENT PATH —
    `/transfers/neuestetransfers/statistik?page=2` — while the query string
    was still the plain `?page=2` this function expects. Fetching BOTH the
    old constructed URL and the new next-link agreed byte-for-byte (the same
    25 transfer ids, 0 overlap with page 1 either way), so the site had
    simply exposed a second path for the same listing, not changed the
    pagination contract. The ORIGINAL version of this function compared the
    FULL url (via `comparable()`, which keeps the path) and would have
    called this unaddressable, silently falling back to slow link-chaining
    for a mode that was, in fact, still perfectly addressable. So only the
    QUERY is compared below, never the path.

    The one thing still checked against the path is the gallery trap's own
    signature: the page number encoded IN the path (`.../page/8/page/3//
    page/2`) rather than passed as a `?page=N` query parameter. A next-link
    shaped like that is never trusted as equivalent to a constructed URL
    regardless of what its query string says (it usually has none at all).
    """
    if not site_next_url:
        return True
    from urllib.parse import urlsplit
    if "/page/" in urlsplit(site_next_url).path:
        return False
    return _query_only(site_next_url) == _query_only(page_url(page1_url, 2))
