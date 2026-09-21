"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers, the run-metadata sidecar, and the
status/exit-code mapping used by all four entry points.

Two modes, one row shape
------------------------
    --mode listing   /categories/{slug} or /collections/{slug}
    --mode product   /products/{slug}

Both yield the same class, and that is a measured fact rather than a
convenience: in Sleep Number's own loader data a listing's
`category.products[i]` and a detail page's `product` are THE SAME OBJECT
SHAPE. One reader serves both routes, so the two cannot drift apart.

Where the columns come from
---------------------------
Sleep Number server-renders no product MARKUP — a category page carries zero
product anchors and zero `"sku"` strings as elements, because the grid is
painted client-side. What it does carry, on every page kind, is the React
Router hydration payload: a turbo-stream document holding the complete
server-side loader data, inlined before any script runs.

So every column here is read out of that payload, and `product_parser.py`
has the detail of how. JSON-LD is present on some pages and read as a SECOND
OPINION, recorded per row in `price_source`, never as the primary source —
it publishes an `ItemList` on some listings and none at all on others.

A ROW IS A SIZE VARIANT
-----------------------
`QCM10` is one model in Queen and `KCM10` the same model in King, at
genuinely different prices — ComfortMode spans $989.10 to $2,069.10 across
seven sizes. `sku` is the variant id, so the family's dedupe-and-diff-on-sku
contract works unchanged and price moves are reported per size.

Every figure quoted in this file was measured on 2026-09-17 through a US
residential exit, and names its date because it is a snapshot of one run
rather than a property of the site (CLAUDE.md §13).
"""
import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The host a row came from. Sleep Number is one US storefront on one host,
# so unlike some siblings this genuinely does not vary — but the column stays,
# in the family's position and under the family's name, so a consumer reading
# several of these repos reads the same first five columns (§9).
SOURCE_DEFAULT = "sleepnumber.com"

@dataclass
class Product:
    """One SIZE VARIANT of one Sleep Number product.

    A row is a variant, not a model: `QCM10` is the ComfortMode™ Mattress in
    Queen and `KCM10` is the same model in King, and they carry genuinely
    different prices (measured: ComfortMode spans 989.10 to 2069.10 across
    seven sizes). `sku` is the variant id, so the family's dedupe-on-sku
    contract works unchanged. See product_parser.py's docstring for the
    argument in full.
    """

    # --- the family prefix, byte-identical and in order across the family ---
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # `/collections/{collection}/{SKU}` where the collection is known, which
    # names the exact variant, else the model's own `canonical_url`. Both
    # answer HTTP 200 (measured). NEVER copied verbatim out of the site's
    # structured data: that publishes `http://sndotcom.fly.dev/...`, the Fly
    # application host, on every ItemList entry — see product_parser's
    # `_public_url`.
    url: str = ""
    # The variant sku — `QCM10`, `K1C360`, `T8`. Sleep Number's own id,
    # unique per size, and the join key for diff_runs.py. A variant with no
    # sku is dropped rather than emitted, because a row that cannot be
    # identified cannot be diffed and is worse than a missing one.
    sku: Optional[str] = None
    # The model name as the site prints it, e.g. "ComfortMode™ Mattress".
    # Shared by every size of that model — `size` is what separates them.
    title: Optional[str] = None

    # --- the product -----------------------------------------------------
    # A constant, not a read field: Sleep Number sells only its own brand,
    # and the site's JSON-LD states `brand.name = "Sleep Number"` on every
    # entry measured. Kept as a column because the family has one and a
    # consumer merging repos expects it; see product_parser.BRAND for why it
    # is not read per row.
    brand: Optional[str] = None

    # --- price -----------------------------------------------------------
    # `variant.sale.cents / 100`, or `variant.regular.cents / 100` when
    # nothing is on sale. INTEGER CENTS at source, so there is no price
    # grammar to get wrong here and this repo ships none — no thousands
    # separator, no decimal comma, no dash standing in for the cents.
    price: Optional[float] = None
    # `currency_iso`, which is a stated fact rather than a symbol to guess
    # from. Null if the payload omits it; never a defaulted "USD".
    currency: Optional[str] = None
    # `variant.regular.cents / 100`, AND ONLY WHEN THE ITEM IS ACTUALLY ON
    # SALE. When `sale` is absent the regular price IS the price, so it moves
    # into `price` and this stays null rather than duplicating it — the
    # mistake that would otherwise give every undiscounted row a 0% discount.
    original_price: Optional[float] = None
    # Computed from the two, never read from the badge. Null — not 0 and
    # never negative — when the figures are not what they seem.
    discount_pct: Optional[float] = None
    # WHICH view built the price (§8: never present a guess as a fact).
    #
    #   payload                   the hydration payload only. The normal case
    #                             on a listing that publishes no ItemList.
    #   payload+jsonld            the payload AND the page's JSON-LD agree.
    #                             Measured 7 of 7 on the king capture.
    #   payload_jsonld_disagree   the two views disagree about this sku. The
    #                             payload is kept and the parser warns; an
    #                             uncorrected row beats an overwritten one.
    #   url-fallback              the payload was unreadable and the row came
    #                             from the URL pattern. NO PRICE on these.
    #
    # diff_runs.py reports a price difference that comes with a price_source
    # difference as `source_changed`, not `changed`.
    price_source: Optional[str] = None

    # --- rating -----------------------------------------------------------
    # `review_stats.average_overall_rating`, and null WITH `review_count`
    # when nothing has been reviewed. The site reports 0 for both on an
    # unreviewed product, and a 0 written through drags every average a
    # consumer computes — the trap bbb-scraper hit with ungraded businesses.
    rating: Optional[float] = None
    review_count: Optional[int] = None

    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: Optional[str] = None

    # --- Sleep Number specific, appended so the family prefix stays stable --
    # "Queen", "King", "Split King"… — from `variant.details.Size`. This is
    # the column that makes a variant row mean something, and the reason a
    # row is a variant at all.
    size: Optional[str] = None
    # Workarea's internal product id (`AF1E5D37DF`) and url slug. The id is
    # stable across a rename; the slug is what the canonical URL uses.
    product_id: Optional[str] = None
    product_slug: Optional[str] = None
    # Which collection the model belongs to, e.g. `mattresses-climate-collection`.
    collection_slug: Optional[str] = None
    # `mattress`, `base`, `pillow`, `other`… — the site's own `type`.
    product_type: Optional[str] = None
    # The site's promotional badge, e.g. "$1,312.50 OFF". Kept verbatim as
    # TEXT rather than parsed into a number: `discount_pct` is computed from
    # the two prices and is the figure to trust, and a badge is marketing
    # copy whose format is the site's to change.
    promo_badge: Optional[str] = None
    # Which listing page this row came from (1-based) and its position within
    # that page. Without `page`, `position` is ambiguous — it restarts at 1 on
    # every page. smoke_test.py asserts the pair is unique across a run.
    page: Optional[int] = None
    position: Optional[int] = None


# Both modes yield the same class, and that is a measured fact rather than a
# convenience: a listing's `category.products[i]` and a detail page's
# `product` are THE SAME OBJECT SHAPE in this site's own loader data, so one
# reader serves both and they cannot drift apart.
ROW_CLASS_BY_MODE = {"listing": Product, "product": Product}

# Modes whose rows are one-per-sku after the dedupe in `save()`, and
# therefore safe to hand to diff_runs.py. Both qualify: a variant sku is
# unique within a run whichever route produced it.
UNIQUE_BY_SKU_MODES = ("listing", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. This site needs that more
    than its siblings do: a scroll batch re-parses the WHOLE feed, cards
    already read included, so every batch after the first arrives mostly
    duplicate by design. A batch that drops all of its rows is the signal
    that the feed is exhausted, which is §7's data-based terminating
    condition and the only one available here.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    All three of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no stories; a tag whose feed
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because all three browser engines return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5
# The same code under the name the engines and the rest of this family use.
# Kept as an alias rather than a rename so a consumer reading two repos in
# this family finds whichever spelling it learned first.
EXIT_REMOTE_API_ERROR = EXIT_API_ERROR


class RemoteAPIError(RuntimeError):
    """A 2Captcha product call failed on its OWN terms — not the site
    blocking a page.

    Raised by fingerprint_client.get_fingerprint and by each engine's
    --cdp-endpoint connect path; caught once at each engine's entry point and
    mapped to EXIT_REMOTE_API_ERROR, so the three engines cannot drift on
    which of 1 (crash) / 3 (blocked) / 5 (remote API error) a given failure
    gets. Without it both paths raise a bare RuntimeError that nothing
    catches, so either failure reaches the interpreter as an unhandled
    exception and exits 1 — a raw traceback with no run-metadata sidecar —
    whichever one it actually was.
    """

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are both recorded, and on this site BOTH of them
    genuinely vary. `mode`, because a tag row and an archive row populate
    different columns: reading time and word count come from a payload the
    tag feed does not carry at all (21 fields per post against the archive's
    84), so diffing one against the other would report both as having
    appeared from nowhere. `source`, because a row must say which host
    domain, and two runs that landed on different hosts describe the same
    catalogue through different addresses. diff_runs.py refuses a pair whose
    modes differ.

    `extra` carries facts about the run that are not about any single row.
    This repo puts the scroll trace there, plus `rows_new_per_batch`, and —
    in archive mode — the DAYS a run covered and any day that redirected.
    A redirect is the thing worth recording: a tag day with no stories does
    not 404, it redirects up to the month view, which is a different
    renderer holding a different set of stories (FINDINGS.md §4).

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the correct output.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected outcome.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "single_page_mode" is a COMPLETE reason, and leaving it out was a real bug
# here: `--mode product` reads a detail page, which HAS no second page, and
# reporting it `partial` (exit 6) made a correct run look like a failure and
# would have made diff_runs.py refuse to compare two product runs. Found by
# running the mode rather than by reading the code.
#
# "no_new_products" is this site's data-side termination condition and the
# reason §7's rule matters here specifically: `?page=N` is IGNORED by Sleep
# Number. `/categories/sheets-pillowcases?page=2` and `?page=99` both answer
# HTTP 200 carrying page ONE — the same ten products, with the payload's own
# `page` field still reading 1. A count-based loop would therefore never
# terminate, and a selector-based one has nothing to read. "This page added
# no sku we had not already seen" is the only condition that is true of the
# catalogue rather than of the markup.
#
# "pagination_exhausted" means the site's own arithmetic said there was no
# further page — `last_page: true`, which is what every category measured on
# this site returns on its first response, since `per_page` is 100 and the
# largest category holds 20 products.
#
# All three are COMPLETE, and the distinction is the point of this tuple: a
# run that asked for 5 pages of a 1-page category and stopped at 1 read the
# whole category. Reporting that `partial` would put the canary permanently
# red, which teaches everyone to ignore the canary (§11).
#
# What must NOT reach here is a run that stopped because it was REFUSED.
# CloudFront's refusal and an empty category are different facts with
# different exit codes (§8), and page_flow.STATE_POLICY is what keeps them
# apart before this tuple is ever consulted.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted",
                         "no_new_products", "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are genuinely different things and a pipeline branches on
        # them (§8: blocked is not empty is not partial):
        #
        #   blocked          something stood between the run and the content
        #   did not complete we never reached the site — a dead proxy, a
        #                    load timeout, a refused batch
        #   completed        we asked, and the site's reply was nothing
        #
        # The middle one used to fall through to EXIT_NO_PRODUCTS, and that
        # was measured rather than reasoned about in a sibling repo: an
        # unreachable proxy produced exit 4 — "ran fine, found nothing" — on
        # a feed with hundreds of rows, while the sidecar beside it said
        # `status: failed`, `pages_completed: 0`. A consumer branching on the
        # exit code, which is what this family says exit codes are for, would
        # have recorded an empty catalogue.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Failed run: 0 of {pages_requested} page(s) were "
                  f"fetched ({stop_reason}). This is NOT an empty result — "
                  f"nothing was read from the site at all.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
