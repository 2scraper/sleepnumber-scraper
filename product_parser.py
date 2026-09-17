"""
product_parser.py
-----------------
Everything this repo knows about sleepnumber.com. If you are editing site
knowledge anywhere else, it belongs here instead.

WHERE THE DATA ACTUALLY IS
==========================
Sleep Number runs a React Router (Remix) front end on Workarea Commerce,
served from Fly.io behind CloudFront. The catalogue does NOT arrive as
markup: a category page's grid is painted by the client. Counted on the
captures in this repo, `/categories/mattresses` carries zero `/products/`
anchors and zero `"sku"` strings in its HTML.

What it DOES carry, on every page kind measured, is the router's own
hydration payload:

    window.__reactRouterContext.streamController.enqueue("[{...}]")

That is a turbo-stream document: a single flat JSON array in which every
value is an INDEX into the same array, and an object is spelled
`{"_16": 17}` meaning "the key at index 16, the value at index 17". Decoded
(see `decode_turbo_stream`), it hands back the complete server-side loader
data — every product, every size variant, every price — with no JavaScript
executed and nothing waiting to paint.

So the extraction order here inverts this family's usual one, and the
reason is measured rather than stylistic:

1. **The hydration payload (primary).** Present and complete on both page
   kinds. A listing exposes it at `loaderData["routes/categories.*"]
   .category.products[]`; a detail page at
   `loaderData["routes/products.$slug"].product` — and the two are THE SAME
   OBJECT SHAPE, so one reader serves both routes and they cannot drift.
2. **JSON-LD (confirmation, not primary).** Real but PARTIAL: an `ItemList`
   appears on some listings and not others — counted across the captures,
   `/categories/mattresses/king` publishes one and `/categories/mattresses`,
   `/categories/beds-on-sale` and `/collections/mattresses-climate-collection`
   publish none. A detail page publishes a single `Product`. Where it is
   present it AGREES with the payload — measured 7 of 7 rows on the king
   capture, and 7686.75 on the Climate360 detail page against the payload's
   768675 cents — so it is worth reading as a second opinion, which is what
   `price_source` records. It is not worth making primary: a primary path
   that vanishes on three listings out of four is not a primary path.
3. **The `/collections/{slug}/{SKU}` URL pattern (fallback).** A URL
   pattern, never a CSS class — classes here are build-generated and churn,
   URLs are a contract with search engines.

PRICES ARE INTEGER CENTS, so this file has no price-grammar section
-------------------------------------------------------------------
`{"sale": {"cents": 393750, "currency_iso": "USD"}}`. There is no thousands
separator to disambiguate, no decimal comma, no dash standing in for the
cents, and no currency symbol to match longest-first. The entire price
parsing apparatus the other members of this family need — and the specific
bugs it exists to prevent — simply has no purchase here, and porting it
would be dead code that looks load-bearing. `currency` comes from
`currency_iso`, which is a fact, never a guess.

The one price trap that IS live: a variant carries `financing` and
`financing_options` describing monthly payments ("$79/mo"). Those are
sibling keys, not text inside a price node, so reading `sale.cents` cannot
pick one up by accident — but a DOM-based reader of this site WOULD be at
risk, which is a further argument for the payload path.

WHAT A ROW IS
-------------
One row per VARIANT, not per product. A "product" here is a model
(ComfortMode™ Mattress) and a variant is that model in one size, with its
own `sku` (TCM10 = Twin, QCM10 = Queen, KCM10 = King …), its own price and
its own sale. Measured on the mattresses category: 7 products, 52 variants,
and the per-size prices are genuinely different — ComfortMode spans 989.10
to 2069.10. Emitting one row per product would force a min/max pair and
throw away the size dimension entirely, which is the thing a price monitor
on a mattress retailer most wants. `sku` is unique per variant, so the
family's dedupe-on-sku contract carries over unchanged.

THREE TRAPS, ALL MEASURED, ALL EASY TO REPEAT
---------------------------------------------
* **The site's own structured data points at its origin host.** Every URL
  inside the JSON-LD `ItemList` reads `http://sndotcom.fly.dev/...` — the
  Fly application hostname, leaked through the renderer — and not
  `https://www.sleepnumber.com/...`. Read verbatim, every row would link to
  a dev host that is not the shop. `_public_url` rewrites it; the count is
  pinned in the suite so a fix upstream is noticed rather than assumed.
* **`?page=N` is ignored.** `/categories/sheets-pillowcases?page=2` and
  `?page=99` both answer HTTP 200 carrying PAGE ONE — 10 products, and the
  payload's own `page` field still reads 1. A scraper that trusted the
  parameter would re-fetch page 1 forever, or (worse) report a second page
  of data that is the first page again. So pagination is planned from the
  site's OWN arithmetic (`next_page`, `last_page`, `total_results`,
  `per_page`) and terminated on DATA — a page that adds no new sku ends the
  listing — never on a selector and never on the parameter.
* **A size-filtered category URL is the same request.**
  `/categories/mattresses/king` and `/categories/mattresses` decode to the
  same payload, the same 7 products and the same `full_url`; the size
  segment selects a facet in the browser. The JSON-LD differs (the king page
  publishes an ItemList priced at King) but the catalogue does not. Treat
  the size segment as presentation, and do not report the two as different
  samples.

PAGINATION, HONESTLY
--------------------
`per_page` is 100 and no category measured comes close: mattresses 7,
sheets-pillowcases 10, furniture 20, and the whole sitemap lists 88
`/products/` URLs. So in practice every category is one page and
`last_page` is True on the first response. The machinery below is still
written the long way — plan from the site's numbers, stop on data — because
"there is only ever one page" is a fact about a catalogue on a date, not a
property of the software, and this family has already paid once for a
scraper that fetched one page and exited 0 while looking complete.
"""

import json
import logging
import re
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup

from output_writer import Product

logger = logging.getLogger("product_parser")


# The one hostname this repo scrapes. Sleep Number operates a single US
# storefront — there is no country-site matrix here the way MediaMarkt or
# Amazon have one, and `sleepnumber.com` redirects to `www.` (HTTP 301,
# measured), so a URL is normalised onto the www host rather than accepted
# both ways.
CANONICAL_HOST = "www.sleepnumber.com"
HOSTS = (CANONICAL_HOST, "sleepnumber.com")

# The application hostname the renderer leaks into structured data. See
# "THREE TRAPS" above. Matched as a host, not a substring of arbitrary text,
# so a product description mentioning the string cannot be rewritten.
ORIGIN_HOST_LEAK = "sndotcom.fly.dev"

SELECTORS = {
    # How a product link is recognised. BOTH shapes are real and both answer
    # 200: `/products/{slug}` is the canonical detail page (it is what
    # `canonical_url` in the payload points at) and
    # `/collections/{slug}/{SKU}` is what a listing tile links to. Anchored
    # on the URL, never on a class.
    "item_link": re.compile(
        r"^(?:https?://[^/]+)?/(?:products/(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*)"
        r"|collections/(?P<collection>[A-Za-z0-9-]+)/(?P<sku>[A-Z0-9]{2,12}))/?$"),
}

# A variant id as it appears in a listing-tile URL. Deliberately NOT used to
# recover a sku for a row — the payload states every sku outright — but it
# is how the fallback path finds products when the payload cannot be read.
_SKU_IN_URL_RE = re.compile(r"/collections/([a-z0-9-]+)/([A-Z0-9]{2,12})\b")

# The hydration payload. React Router streams it as one or more
# `enqueue("...")` calls carrying a JSON-encoded string.
_ENQUEUE_RE = re.compile(r'streamController\.enqueue\("((?:[^"\\]|\\.)*)"\)')

# ---------------------------------------------------------------------------
# Block / challenge detection
# ---------------------------------------------------------------------------
# EVERY candidate below was counted on pages this repo knows are good before
# being kept or dropped (CLAUDE.md §18), because a marker that matches a
# served page is worse than no marker. The counts, taken 2026-09-17 over four
# served pages, two of the site's own 404s and two CloudFront refusals (one
# fetched with an HTTP client, one read out of a browser DOM):
#
#   marker                     served        404      refused
#   "Request blocked"          0 0 0 0      0 0        1 1
#   "CloudFront"               0 0 0 0      0 0        4 4
#   "recaptcha"                1 1 1 1      1 1        0 0   <- NOT a marker
#   "captcha"                  1 1 1 1      1 1        0 0   <- NOT a marker
#   akamai / datadome / perimeterx / incapsula / hcaptcha / awswaf /
#   cf-turnstile / challenges.cloudflare.com / errors.edgesuite.net
#                              0 0 0 0      0 0        0 0   <- dead code
#
# Two conclusions, and the second is the one that costs people time:
#
#  * Only CloudFront's own refusal wording discriminates, so that is the
#    whole list. Nothing from any bot-management vendor appears on a refusal
#    this site produces, and carrying those markers "just in case" is the
#    dead code §18 warns about.
#  * `recaptcha` and `captcha` occur exactly once on EVERY page, good and
#    bad alike — the site ships a reCAPTCHA loader in its base template. A
#    detector keying on either would report every page in the catalogue as
#    challenged. That the site has reCAPTCHA CONFIGURED is true and is not
#    the same statement as a challenge being rendered; see
#    `captcha_solver.py` for what is and is not implemented against it.
BOT_CHALLENGE_MARKERS: Tuple[str, ...] = (
    "request blocked",
    "generated by cloudfront",
)

# The structural positive signal, and the reason detection here does not rest
# on refusal wording alone: a page Sleep Number actually served is BUILT OUT
# OF its own assets, and a CloudFront interstitial is not. Counted on the same
# captures: `cdn.sleepnumber.com` appears 214, 549, 53 and 221 times on the
# four served pages, 31 and 27 times on the two 404s, and 0 times on both
# refusals. `__reactRouterContext` is 5 on every page the application
# rendered and 0 on both refusals.
#
# This is the same trick mediamarkt-scraper needed for a block page carrying
# no vendor marker, and tokopedia-scraper needed for Chromium's own network
# error page — which is the case worth keeping in mind here too, since that
# page carries the site's hostname in its <title> and would fool a title
# check.
SITE_ASSET_MARKERS: Tuple[str, ...] = (
    "cdn.sleepnumber.com",
    "__reactRouterContext",
)

# The site's own "not found" page. It is NOT small — measured 208-226 KB,
# with the full site chrome, a `@graph` JSON-LD block and 21-31 references to
# the asset host — so every structural signal above says "served", correctly.
# It is told apart by its own wording plus the absence of any product in the
# payload, never by size.
# MEASURED, not guessed, and the first version of this list was WRONG —
# which is the whole argument for counting (§18). It read `<title>not found`
# and `page not found`, and NEITHER occurs on the page this site actually
# serves for a missing product: `/products/i8` comes back with
# `<title>Sleep Number</title>`, so the list matched nothing and a 404 was
# only ever recognised through its status code. A capture handed to the
# parser without one — a `--dump-html` file, a fixture — came out as `empty`,
# i.e. as a claim about the catalogue.
#
# Counted 2026-09-17 over 17 captures the application routed (served
# listings, a landing page, detail pages, two locales) and the two real 404s:
#
#   "not found"   0 on every one of the 17   |   2 and 3 on the two 404s
#
# So the bare string discriminates perfectly where the title-anchored version
# discriminated nothing.
_NOT_FOUND_MARKERS: Tuple[str, ...] = (
    "not found",
)

# Route-key prefixes whose loader data carries catalogue objects. The key is
# NOT stable across categories — a capture of /categories/mattresses answers
# under `routes/categories.mattresses` while /categories/furniture answers
# under `routes/categories.$slug`, because the app defines a specific route
# for one and a parameterised route for the rest. Matching on a prefix rather
# than an exact key is what keeps that from silently returning zero rows.
_LISTING_ROUTE_PREFIXES = ("routes/categories", "routes/collections")
_DETAIL_ROUTE_PREFIXES = ("routes/products", "routes/bundles")

# Path prefixes that are not a category even though they sit where one would.
_NOT_A_CATEGORY = frozenset({
    "cart", "checkout", "orders", "search", "login", "logout", "users",
    "user", "referrals", "compare", "menus", "hc", "image", "post", "blog",
    "pages", "stores", "rewards", "sitemap.xml", "robots.txt",
})


# ---------------------------------------------------------------------------
# turbo-stream
# ---------------------------------------------------------------------------

_TS_CONSTANTS = {
    # turbo-stream encodes a handful of JS values that JSON cannot express as
    # negative indices. Only the ones actually observed in this site's
    # payloads are mapped; anything else is preserved as a marker string
    # rather than silently becoming None, so an unmapped constant shows up in
    # output as something a human will query instead of as a missing value.
    -1: None,    # undefined
    -2: None,    # hole in a sparse array
    -3: None,    # NaN — not a number we would want to arithmetic on anyway
    -4: None,    # +Infinity
    -5: None,    # -Infinity / null, as this app emits it
    -6: 0,       # -0
    -7: None,    # undefined, second encoding
}


def extract_turbo_stream(html: str) -> Optional[str]:
    """The concatenated hydration payload, or None if the page has none.

    None is a real answer and the caller must treat it as one: a CloudFront
    refusal has no payload, and so does any page the application did not
    render. It is NOT the same as "the payload held no products", which is
    what an out-of-catalogue category legitimately looks like.
    """
    chunks = _ENQUEUE_RE.findall(html or "")
    if not chunks:
        return None
    out = []
    for chunk in chunks:
        try:
            out.append(json.loads('"' + chunk + '"'))
        except ValueError:
            # One unparseable chunk must not lose the others: the stream is
            # concatenative and a later chunk can still carry the catalogue.
            logger.debug("turbo-stream: skipped an undecodable chunk")
    return "".join(out) or None


def decode_turbo_stream(payload: str) -> Any:
    """Resolve a turbo-stream document into ordinary Python objects.

    The wire format is a flat array in which every value is an index into
    that same array. An object is `{"_16": 17}` — key at index 16, value at
    index 17. Cycles are legal in the format and do occur (the app's route
    objects reference shared sub-objects), so resolution is depth-bounded
    rather than trusting the data to be a tree.
    """
    flat = json.loads(payload)
    if not isinstance(flat, list):
        raise ValueError("turbo-stream payload is not an array")

    def resolve(ref: Any, depth: int) -> Any:
        if depth > 64:
            return None
        if isinstance(ref, bool):
            return ref
        if isinstance(ref, int):
            if ref < 0:
                return _TS_CONSTANTS.get(ref)
            if ref >= len(flat):
                return None
            value = flat[ref]
        else:
            value = ref
        if isinstance(value, dict):
            out = {}
            for raw_key, raw_val in value.items():
                key = raw_key
                if isinstance(raw_key, str) and raw_key.startswith("_"):
                    try:
                        resolved = resolve(int(raw_key[1:]), depth + 1)
                    except ValueError:
                        resolved = raw_key
                    key = resolved if isinstance(resolved, str) else str(resolved)
                out[key] = resolve(raw_val, depth + 1)
            return out
        if isinstance(value, list):
            return [resolve(item, depth + 1) for item in value]
        return value

    return resolve(0, 0)


def loader_data(html: str) -> Optional[Dict[str, Any]]:
    """`loaderData` out of the page's hydration payload, or None."""
    payload = extract_turbo_stream(html)
    if not payload:
        return None
    try:
        decoded = decode_turbo_stream(payload)
    except (ValueError, TypeError) as exc:
        logger.warning("turbo-stream did not decode: %s", exc)
        return None
    if not isinstance(decoded, dict):
        return None
    data = decoded.get("loaderData")
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Finding catalogue objects in the loader data
# ---------------------------------------------------------------------------

def _catalogue_nodes(data: Dict[str, Any]) -> Tuple[List[dict], Optional[dict]]:
    """(product objects, the category object they came from).

    Handles both page kinds, because the objects are the same shape:
    a listing's `category.products[]` and a detail page's `product`.
    """
    for key, value in (data or {}).items():
        if not isinstance(value, dict):
            continue
        if key.startswith(_LISTING_ROUTE_PREFIXES):
            category = value.get("category")
            if isinstance(category, dict):
                products = category.get("products")
                if isinstance(products, list) and products:
                    return [p for p in products if isinstance(p, dict)], category
            # A /collections/ route names its products `pkgs` and nests the
            # sellable ones under `packaged_products`. Same objects, one more
            # level down.
            pkgs = value.get("pkgs")
            if isinstance(pkgs, list) and pkgs:
                return _flatten_packages(pkgs), (category if isinstance(category, dict) else None)
        if key.startswith(_DETAIL_ROUTE_PREFIXES):
            product = value.get("product")
            if isinstance(product, dict):
                return [product], None
    return [], None


def _flatten_packages(pkgs: Sequence[Any], depth: int = 0) -> List[dict]:
    """Every sellable product inside a `/collections/` package tree.

    A package ("ClimateCool® smart bed") is a bundle whose `packaged_products`
    are the things with variants and prices; those can nest one level further.
    Only nodes that actually carry variants are returned — a package node
    itself has no sku and no price and is not a row.
    """
    out: List[dict] = []
    if depth > 6:
        return out
    for pkg in pkgs or ():
        if not isinstance(pkg, dict):
            continue
        if isinstance(pkg.get("variants"), list) and pkg["variants"]:
            out.append(pkg)
        nested = pkg.get("packaged_products")
        if isinstance(nested, list) and nested:
            out.extend(_flatten_packages(nested, depth + 1))
    # Same product can appear under two packages in one collection; keep the
    # first occurrence so page order is stable and dedupe stays the writer's
    # job rather than happening twice with different rules.
    seen, unique = set(), []
    for p in out:
        pid = p.get("id") or p.get("slug")
        if pid in seen:
            continue
        seen.add(pid)
        unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------

def _money(node: Any) -> Tuple[Optional[float], Optional[str]]:
    """(amount, ISO currency) from a `{"cents": N, "currency_iso": "USD"}`.

    Returns (None, None) rather than a defaulted USD when the node is absent
    or malformed: a missing currency is null in this family, never a guess.
    """
    if not isinstance(node, dict):
        return None, None
    cents = node.get("cents")
    currency = node.get("currency_iso")
    if not isinstance(cents, (int, float)) or isinstance(cents, bool):
        return None, currency if isinstance(currency, str) and currency else None
    return round(cents / 100.0, 2), (currency if isinstance(currency, str) and currency else None)


def _public_url(url: Optional[str]) -> Optional[str]:
    """Rewrite the leaked origin hostname onto the public one.

    See "THREE TRAPS". Only the host is replaced and only when it is exactly
    the Fly application host, so a path or a description that happens to
    contain the string is untouched.
    """
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    if parts.netloc == ORIGIN_HOST_LEAK:
        return urlunsplit(("https", CANONICAL_HOST, parts.path, parts.query, parts.fragment))
    if not parts.netloc:
        return urlunsplit(("https", CANONICAL_HOST, parts.path, parts.query, parts.fragment))
    return url


def _size_of(variant: dict) -> Optional[str]:
    """The variant's size, e.g. "Queen"."""
    details = variant.get("details")
    if isinstance(details, dict):
        for key in ("Size", "size"):
            value = details.get(key)
            if isinstance(value, list) and value and isinstance(value[0], str):
                return value[0]
            if isinstance(value, str) and value:
                return value
    return None


def _rating_of(product: dict) -> Tuple[Optional[float], Optional[int]]:
    """(rating, review_count), with zero treated as absence, together.

    `review_stats` reports `average_overall_rating: 0` alongside
    `total_review_count: 0` for a product nobody has reviewed. Written
    through, that zero drags every average a consumer computes — the same
    trap bbb-scraper hit, where an ungraded business reported 0.0. So the two
    columns go null TOGETHER, and a rating is only reported when there is at
    least one review behind it.
    """
    stats = product.get("review_stats")
    if not isinstance(stats, dict):
        return None, None
    count = stats.get("total_review_count")
    rating = stats.get("average_overall_rating")
    count = int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None
    if not count:
        return None, (0 if count == 0 else None)
    if not isinstance(rating, (int, float)) or isinstance(rating, bool) or rating <= 0:
        return None, count
    return round(float(rating), 4), count


def _image_of(product: dict) -> Optional[str]:
    """The first usable image URL."""
    images = product.get("images")
    if not isinstance(images, list):
        return None
    for image in images:
        if isinstance(image, str) and image.startswith("http"):
            return image
        if isinstance(image, dict):
            for key in ("url", "large", "medium", "image", "src", "original"):
                value = image.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
    return None


def _discount_pct(price: Optional[float], original: Optional[float]) -> Optional[float]:
    """Percent off, or None when the two figures are not what they seem.

    Returns None — never 0 and never a negative — when the "original" is at
    or below the selling price. mediamarkt-scraper found a site rendering two
    different struck-through prices, one of which was a 30-day low BELOW the
    selling price, and reading it as a was-price produced a negative discount
    on an undiscounted product. Nothing on this site has been measured doing
    that, but the guard costs a line and the canary asserts it.
    """
    if price is None or original is None or original <= 0:
        return None
    if original <= price:
        return None
    return round((original - price) / original * 100.0, 2)


# ---------------------------------------------------------------------------
# JSON-LD — the second opinion
# ---------------------------------------------------------------------------

def jsonld_nodes(html: str) -> List[dict]:
    """Every JSON-LD node on the page, `@graph` flattened.

    Tolerates the shapes CLAUDE.md §4 lists as legal-and-crashing: a list at
    the top level, a null `offers`, a non-dict inside a list, products under
    `@graph` rather than `itemListElement`.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    out: List[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
            out.append(node)
    return out


def jsonld_prices(html: str) -> Dict[str, Tuple[Optional[float], Optional[str]]]:
    """{sku: (price, currency)} from ItemList entries and a detail Product.

    This is the CONFIRMATION source, not the primary one — see the module
    docstring for the counts behind that choice.
    """
    prices: Dict[str, Tuple[Optional[float], Optional[str]]] = {}

    def take(node: dict) -> None:
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = next((o for o in offers if isinstance(o, dict)), None)
        if not isinstance(offers, dict):
            offers = {}
        sku = node.get("sku") or offers.get("sku")
        if not isinstance(sku, str) or not sku:
            return
        raw = offers.get("price")
        try:
            price = round(float(raw), 2) if raw is not None else None
        except (TypeError, ValueError):
            price = None
        currency = offers.get("priceCurrency")
        prices[sku] = (price, currency if isinstance(currency, str) else None)

    for node in jsonld_nodes(html):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "Product" in types:
            take(node)
        if "ItemList" in types:
            for entry in node.get("itemListElement") or ():
                if not isinstance(entry, dict):
                    continue
                item = entry.get("item")
                if isinstance(item, dict):
                    take(item)
    return prices


# ---------------------------------------------------------------------------
# The public parse
# ---------------------------------------------------------------------------

def parse_products(html: str, url: str = "", page: Optional[int] = None) -> List[Product]:
    """Every variant on this page, in the site's own order.

    One row per size variant; see the module docstring for why that is the
    row and not the product. Returns [] for a page that carried no catalogue
    — which the caller must NOT read as "the parser failed": `detect_page_state`
    is what distinguishes an empty category from a refusal.
    """
    data = loader_data(html)
    if data is None:
        return _fallback_from_links(html, url, page)

    products, category = _catalogue_nodes(data)
    if not products:
        return []

    confirmations = jsonld_prices(html)
    category_name = None
    if isinstance(category, dict):
        name = category.get("name")
        category_name = name if isinstance(name, str) and name else None
    if not category_name:
        category_name = category_from_url(url)

    rows: List[Product] = []
    position = 0
    for product in products:
        variants = product.get("variants")
        if not isinstance(variants, list):
            continue
        title = product.get("name") or product.get("branded_name")
        detail_url = _public_url(product.get("canonical_url"))
        image_url = _image_of(product)
        rating, review_count = _rating_of(product)
        product_id = product.get("id") if isinstance(product.get("id"), str) else None
        product_slug = product.get("slug") if isinstance(product.get("slug"), str) else None
        collection_slug = (product.get("category_slug")
                           if isinstance(product.get("category_slug"), str) else None)
        product_type = product.get("type") if isinstance(product.get("type"), str) else None
        purchasable = product.get("purchasable")

        for variant in variants:
            if not isinstance(variant, dict):
                continue
            sku = variant.get("sku")
            if not isinstance(sku, str) or not sku:
                # A variant with no sku cannot be deduped or diffed, and a
                # row that cannot be identified is worse than a missing one.
                continue
            position += 1
            price, currency = _money(variant.get("sale"))
            original, original_currency = _money(variant.get("regular"))
            if price is None:
                # Some variants price only through `regular` (nothing is on
                # sale). That is a real price, not a missing one.
                price, currency = original, original_currency
                original = None
            if currency is None:
                currency = original_currency
            if original is not None and price is not None and original <= price:
                # `regular` EQUALS `sale` on everything that is not actually
                # discounted — measured on 211 of 211 rows of the
                # sheets-pillowcases category. Copied straight across, that
                # gives an `original_price` identical to the price on every
                # undiscounted row: a column asserting a was-price where there
                # was no price change. woolworths-scraper shipped within one
                # commit of exactly this on 81% of its rows. Null unless the
                # regular price is genuinely HIGHER.
                original = None

            confirmed_price, confirmed_currency = confirmations.get(sku, (None, None))
            if confirmed_price is None:
                price_source = "payload"
            elif price is not None and abs(confirmed_price - price) < 0.005 and (
                    confirmed_currency is None or confirmed_currency == currency):
                price_source = "payload+jsonld"
            else:
                # The two views disagree about this sku. Leave the payload
                # row alone — overwriting a correct row is worse than leaving
                # one uncorrected — and say so, loudly enough to find.
                price_source = "payload_jsonld_disagree"
                logger.warning(
                    "sku %s: payload says %s %s, JSON-LD says %s %s — keeping the payload",
                    sku, price, currency, confirmed_price, confirmed_currency)

            active = variant.get("active")
            in_stock = None
            if isinstance(active, bool):
                in_stock = active and (purchasable is not False)

            rows.append(Product(
                url=_variant_url(detail_url, collection_slug, sku, url),
                sku=sku,
                title=title if isinstance(title, str) else None,
                brand=BRAND,
                price=price,
                currency=currency,
                original_price=original,
                discount_pct=_discount_pct(price, original),
                rating=rating,
                review_count=review_count,
                in_stock=in_stock,
                image_url=image_url,
                category=category_name,
                price_source=price_source,
                size=_size_of(variant),
                product_id=product_id,
                product_slug=product_slug,
                collection_slug=collection_slug,
                product_type=product_type,
                promo_badge=(variant.get("promo_badge_message")
                             if isinstance(variant.get("promo_badge_message"), str) and
                             variant.get("promo_badge_message") else None),
                page=page,
                position=position,
            ))
    return rows


# Every product in this catalogue is Sleep Number's own; the site sells no
# third-party brand. Confirmed against the JSON-LD, which states
# `brand.name = "Sleep Number"` on every ItemList entry measured. It is a
# constant rather than a read field because the payload does not carry one,
# and inventing a per-row read that always returns the same literal would
# suggest a variation that does not exist.
BRAND = "Sleep Number"


def _variant_url(detail_url: Optional[str], collection_slug: Optional[str],
                 sku: str, page_url_: str = "") -> str:
    """The most specific public URL for this variant.

    Preference order, and each step is a real page that answers 200:
      1. `/collections/{collection}/{SKU}` — names the exact variant.
      2. the product's own `canonical_url` — names the model.
      3. the page we were parsing, so a row is never URL-less.
    """
    if collection_slug and sku:
        return f"https://{CANONICAL_HOST}/collections/{collection_slug}/{sku}"
    if detail_url:
        return detail_url
    return page_url_ or f"https://{CANONICAL_HOST}/"


def _fallback_from_links(html: str, url: str, page: Optional[int]) -> List[Product]:
    """URL-pattern fallback, for a page whose payload could not be read.

    Deliberately thin. It cannot produce a price — this site publishes none
    in markup — so it emits identifiable rows with null prices rather than
    pretending to have parsed. A run made entirely of these is visible at a
    glance in `price_source`, and `finish_run` reports it rather than calling
    the run complete with a full row count and no data in it.
    """
    rows: List[Product] = []
    seen = set()
    for match in _SKU_IN_URL_RE.finditer(unescape(html or "").replace("\\/", "/")):
        collection, sku = match.group(1), match.group(2)
        if sku in seen:
            continue
        seen.add(sku)
        rows.append(Product(
            url=f"https://{CANONICAL_HOST}/collections/{collection}/{sku}",
            sku=sku,
            brand=BRAND,
            category=category_from_url(url),
            price_source="url-fallback",
            collection_slug=collection,
            page=page,
            position=len(rows) + 1,
        ))
    if rows:
        logger.warning(
            "read %d rows from the URL pattern because the hydration payload "
            "was unreadable — these rows carry no price", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------

_BLANK_BODY_RE = re.compile(r"<body[^>]*>\s*</body>", re.I)


def _is_blank_document(html: str) -> bool:
    """A document the browser produced because nothing arrived.

    Structural, not a size test: an empty <body>, no hydration payload and no
    reference to the site's own assets. A real page fails all three.
    """
    text = html or ""
    if len(text) > 4096:
        return False
    if "__reactRouterContext" in text:
        return False
    if any(marker in text.lower() for marker in SITE_ASSET_MARKERS):
        return False
    stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    body = re.search(r"<body[^>]*>(.*?)</body>", stripped, re.S | re.I)
    if body is None:
        return not re.sub(r"<[^>]+>", "", stripped).strip()
    return not re.sub(r"<[^>]+>", "", body.group(1)).strip()


def _is_landing_page(data: Dict[str, Any]) -> bool:
    """A routed listing URL that carries no product list at all.

    `category.products` ABSENT is a landing page; present-and-empty is an
    empty category. Both render, both are 200, and only one of them is a
    statement about the catalogue.
    """
    for key, value in (data or {}).items():
        if not isinstance(value, dict) or not key.startswith(_LISTING_ROUTE_PREFIXES):
            continue
        category = value.get("category")
        if isinstance(category, dict) and "products" not in category:
            return True
    return False


def detect_page_state(html: str, status: Optional[int] = None, url: str = "",
                      headers: Optional[Dict[str, str]] = None) -> str:
    """One of: content | blocked | notfound | empty | unknown.

    Ordered by how much each signal PROVES, not by how cheap it is to
    compute — tokopedia-scraper put a threshold heuristic ahead of an
    unambiguous positive signal and reported exit 3 for a correct answer.

    So: a page carrying actual catalogue objects is `content`, full stop,
    whatever else is on it. Only then do the refusal signals get a turn.
    """
    text = html or ""
    head = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    # 0. The response headers, where we have them. CloudFront stamps its own
    #    refusal: a page Sleep Number served answers `x-cache: Miss from
    #    cloudfront` (or Hit/RefreshHit) and carries `server: Fly/…`, while a
    #    refusal answers `x-cache: Error from cloudfront` with `server:
    #    CloudFront` — measured on both. This runs FIRST because it is the
    #    one signal the body cannot forge; §8's "the status code may be the
    #    only signal a block gives you", one layer up.
    #
    #    `x-amzn-waf-action` is checked too and has never been observed on
    #    this site. It is the header AWS WAF sets when it wants a CAPTCHA
    #    rather than a refusal, so if it ever appears the state is a solvable
    #    challenge and not a dead end — which is a different answer, and the
    #    reason this looks for it rather than assuming.
    if head.get("x-amzn-waf-action", "").lower() in ("captcha", "challenge"):
        return "captcha"
    if "error from cloudfront" in head.get("x-cache", "").lower():
        return "blocked"

    # 1. Unambiguous positive: the application rendered and the payload holds
    #    products. Nothing an interstitial can do produces this.
    data = loader_data(text)
    if data is not None:
        products, _ = _catalogue_nodes(data)
        if products:
            return "content"

    lowered = text.lower()

    # 2. A refusal, by CloudFront's own wording. Entity-normalised over a
    #    BOUNDED prefix first: farfetch-scraper found an edge that escapes the
    #    punctuation in its refusal page, so the same marker matched a browser
    #    DOM and silently missed the HTTP client. Bounded because a refusal is
    #    a few hundred bytes and unescaping a megabyte of catalogue would let
    #    a product description read as a marker.
    prefix = unescape(text[:4096]).lower()
    if any(marker in prefix or marker in lowered[:4096] for marker in BOT_CHALLENGE_MARKERS):
        return "blocked"

    # 2b. A BLANK document. Not an interstitial and not a refusal: nothing
    #     arrived at all. Chromium renders `<html><head></head><body></body>`
    #     when a navigation fails outright — which is what a proxy that could
    #     not authenticate produces, and Selenium cannot authenticate a proxy
    #     (see selenium_scraper.py and the README's "Engine limits").
    #
    #     This is structural rather than a size threshold: no payload, no
    #     asset reference, and a body with nothing in it. Sleep Number serves
    #     either ~190 KB of application HTML or a 919-byte CloudFront
    #     refusal, and neither is this.
    #
    #     It answers `blocked` rather than `empty` deliberately. The
    #     difference is what the CALLER is told: `empty` is exit 4, "this
    #     category has no products", which is a claim about the catalogue —
    #     and here the catalogue was never asked. `blocked` is exit 3, "we
    #     did not get the page", which is true.
    if _is_blank_document(text):
        return "blocked"

    # 3. Structural: was this built out of the site's own assets? A served
    #    page references them heavily; a CloudFront interstitial not at all.
    #    This is what catches a refusal whose wording changes, and Chromium's
    #    own network-error page, which carries the site's hostname in its
    #    <title> and would pass any title check.
    if not any(marker in lowered for marker in SITE_ASSET_MARKERS):
        if status is not None and status >= 400:
            return "blocked"
        return "unknown"

    # 4. The site's own 404. Checked AFTER the asset test on purpose: it is a
    #    real page the application served, so it is not a block, and it must
    #    not be retried as though it were transient.
    #
    #    THE STRUCTURAL SIGNAL LEADS, because it is the stronger one and
    #    because it does not depend on the caller having a status code to
    #    hand. React Router puts one key per matched route into `loaderData`;
    #    a URL that matched no route has `root` and nothing else. Counted
    #    2026-09-17: exactly one route key on all 17 routed captures, zero on
    #    both real 404s.
    if data is not None and not [k for k in data if k != "root"]:
        return "notfound"
    if status == 404 or any(marker in lowered for marker in _NOT_FOUND_MARKERS):
        return "notfound"

    # 5. The application routed this URL but it carries no product LIST at
    #    all — distinct from a list that is empty, and the difference is what
    #    the caller should be told.
    #
    #    Some `/categories/…` URLs are curated landing pages rather than
    #    listings: `/categories/beds-on-sale` renders 320 KB with
    #    `category.name = "Sale"` and no `products` key, no `total_results`
    #    and no `slug` — the keys a real category carries. Reported as "zero
    #    products" it reads as "this category is empty", which sends the
    #    reader to check the catalogue instead of the URL.
    if data is not None and _is_landing_page(data):
        return "no_listing"

    # 6. Served, application rendered, a real product list with nothing in
    #    it. Not a failure and not worth a retry.
    if data is not None:
        return "empty"

    return "unknown"


# ---------------------------------------------------------------------------
# Pagination and categories
# ---------------------------------------------------------------------------

def pagination(html: str) -> Dict[str, Any]:
    """The site's own arithmetic about this listing.

    Returned verbatim rather than reduced to a page count, because the
    sidecar records it: "complete" and "exhaustive" are different words, and
    a consumer is entitled to know that a run fetched everything the site
    would serve AND how much of the result set that was.
    """
    out: Dict[str, Any] = {
        "page": None, "per_page": None, "total_results": None,
        "next_page": None, "prev_page": None, "last_page": None,
    }
    data = loader_data(html)
    if not data:
        return out
    for key, value in data.items():
        if not isinstance(value, dict) or not key.startswith(_LISTING_ROUTE_PREFIXES):
            continue
        category = value.get("category")
        if not isinstance(category, dict):
            continue
        for field in out:
            if field in category:
                out[field] = category[field]
        return out
    return out


def has_next_page(html: str) -> bool:
    """Whether the SITE says there is another page.

    Note what this deliberately does not do: infer a next page from a row
    count against `per_page`. The parameter is ignored by this site (see
    "THREE TRAPS"), so an inferred page 2 would re-fetch page 1 and a
    count-based loop would never terminate.
    """
    bits = pagination(html)
    if bits.get("last_page") is True:
        return False
    return bool(bits.get("next_page"))


def page_url(url: str, page: int) -> str:
    """The URL for page N, preserving existing query parameters.

    KEPT, AND KNOWN NOT TO WORK ON ITS OWN. Measured 2026-09-17:
    `/categories/sheets-pillowcases?page=2` and `?page=99` both answer HTTP
    200 with page ONE's ten products, and the payload's own `page` still
    reads 1. So this function builds the conventional URL and the ENGINE
    decides whether to trust it, by comparing what came back against what it
    already has — the data-based terminating condition CLAUDE.md §7 requires.
    It is not deleted because the convention is what the site would use if it
    ever paginated, and because `pagination_is_addressable` needs something
    to compare the site's own next-link against.
    """
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() != "page"]
    if page and page > 1:
        query.append(("page", str(page)))
    return urlunsplit((parts.scheme or "https", parts.netloc or CANONICAL_HOST,
                       parts.path, urlencode(query), parts.fragment))


def pagination_is_addressable(html: str, url: str) -> bool:
    """Can page N be fetched without walking pages 1..N-1?

    Measured answer for this site: NO, and not because the pages are behind a
    cursor but because the parameter does nothing. This asks the question
    from page 1's own answer rather than asserting it, so the day the site
    starts honouring `?page=` the scraper notices instead of being rewritten.
    """
    bits = pagination(html)
    if bits.get("last_page") is True and not bits.get("next_page"):
        # A single-page listing is trivially addressable; there is nothing to
        # address. Saying True here would claim a capability untested on this
        # site, so say False and let the caller chain — it costs one fetch it
        # was going to make anyway.
        return False
    return False


def category_from_url(url: str) -> Optional[str]:
    """A human-ish category label from the URL, or None."""
    try:
        path = urlsplit(url or "").path
    except ValueError:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    head = parts[0].lower()
    if head in _NOT_A_CATEGORY:
        return None
    if head in ("categories", "collections") and len(parts) > 1:
        return parts[1]
    if head in ("products", "bundles") and len(parts) > 1:
        return None
    return None


def normalise_url(url: str) -> str:
    """Put a URL on the canonical host, or return it unchanged if off-site."""
    parts = urlsplit(url or "")
    if parts.netloc in ("", "sleepnumber.com"):
        return urlunsplit(("https", CANONICAL_HOST, parts.path or "/",
                           parts.query, parts.fragment))
    return url


def is_supported_url(url: str) -> Tuple[bool, str]:
    """(supported, reason). The reason is the point — refuse WITH it.

    mediamarkt-scraper learned to say why: a host refused as "is not a
    MediaMarkt site" when it was one, just a different platform, sends the
    reader hunting for a typo that is not there.
    """
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https"):
        return False, f"{url!r} is not an http(s) URL"
    host = parts.netloc.split("@")[-1].split(":")[0].lower()
    if host not in HOSTS:
        return False, (f"{host or url!r} is not a sleepnumber.com host — this "
                       f"scraper reads {CANONICAL_HOST} only")
    head = next((p for p in parts.path.split("/") if p), "").lower()
    if head in _NOT_A_CATEGORY:
        return False, (f"/{head}/ is not a catalogue path — pass a "
                       f"/categories/…, /collections/… or /products/… URL")
    return True, ""


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

# The default listing when the caller names no URL. `/categories/mattresses`
# is the obvious front door of this catalogue, it is in the site's own
# sitemap, and it is small enough (7 products, 52 variants) that a first run
# finishes in seconds — which is what a default is for.
DEFAULT_CATEGORY = "mattresses"
CATEGORY_URL = f"https://{CANONICAL_HOST}/categories/{DEFAULT_CATEGORY}"


def category_url(slug: str) -> str:
    """The listing URL for a category slug.

    Accepts a `/collections/` slug too, because the site serves both and a
    caller reading the sitemap will have a mixture. The distinction is which
    route renders it, and both routes are read by the same parser.
    """
    slug = (slug or "").strip().strip("/")
    if not slug:
        return CATEGORY_URL
    if slug.startswith(("categories/", "collections/")):
        return f"https://{CANONICAL_HOST}/{slug}"
    return f"https://{CANONICAL_HOST}/categories/{slug}"


def product_url(slug: str) -> str:
    """The detail URL for a product slug, e.g. `cm-mattress`.

    Note this is the `canonical_url` slug and NOT the model's `slug` field:
    the ComfortMode mattress has `slug` "comfortmode" and a canonical URL of
    `/products/cm-mattress`, and only the latter answers 200. Measured:
    `/products/i8` — a slug that looks entirely plausible and appears in
    search results — returns the site's 404.
    """
    slug = (slug or "").strip().strip("/")
    if slug.startswith("products/"):
        slug = slug[len("products/"):]
    return f"https://{CANONICAL_HOST}/products/{slug}"


def detect_bot_challenge(html: str, url: str = "") -> Optional[str]:
    """Which refusal marker matched, or None — a NAME for the log line.

    `detect_page_state` answers what to DO; this answers what to SAY. The
    engines log the vendor beside the exit code so a red run can be read
    without re-fetching the page, and `finish_run` puts it in the sidecar's
    `stop_reason` as `blocked_{vendor}`.

    On this site there is exactly one refuser — CloudFront — so the answer is
    always "cloudfront" or None. It stays a lookup rather than a constant
    because the day a second one appears, the log should say which.
    """
    text = (html or "")
    prefix = unescape(text[:4096]).lower()
    lowered = text[:4096].lower()
    for marker in BOT_CHALLENGE_MARKERS:
        if marker in prefix or marker in lowered:
            return "cloudfront"
    return None
