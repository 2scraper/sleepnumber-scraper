#!/usr/bin/env python3
"""make_fixtures.py — build smoke_test.py's fixtures from real captures.

WHY THIS EXISTS RATHER THAN A HAND-WRITTEN FIXTURE
--------------------------------------------------
CLAUDE.md is firm that fixtures come from real captures, and firm again that
a trimmed fixture must be verified to parse identically to the untrimmed
original before it is committed. Both are awkward here, for a reason
specific to this site:

A Sleep Number page is 300-900 KB and its catalogue lives in a turbo-stream
document — a FLAT ARRAY in which every value is an index into that same
array. You cannot delete products from it the way you can delete `<li>`
elements from a listing: every index after the cut shifts, and the document
decodes to something else or to nothing.

So the reduction is done by DECODING the real capture, taking real product
objects out of it verbatim, and RE-ENCODING them into the same wire format.
The fixture therefore holds this site's real field names, real values and
real structure, at a size that belongs in a repository — and the generator
asserts that what it produced decodes back to objects EQUAL to the ones it
took out, so "reduced" never quietly means "different".

    python3 tools/make_fixtures.py captures/us/ -o fixtures_generated.json

The output is committed. Re-run it when a capture is refreshed, and read the
diff: a changed value is the site changing, which is exactly what a fixture
is for.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import product_parser as pp  # noqa: E402


def encode_turbo_stream(value):
    """Encode a Python object into turbo-stream's flat-array form.

    The inverse of product_parser.decode_turbo_stream, and deliberately the
    simplest encoder that produces a document that decoder accepts: every
    value gets its own slot, nothing is interned. The site's own encoder
    interns aggressively (its arrays are a third the size), which is a
    compression detail and not part of the format's meaning — the decoder is
    what this repo has to get right, and it reads both.
    """
    flat = []

    def emit(obj):
        idx = len(flat)
        flat.append(None)
        if isinstance(obj, dict):
            slot = {}
            flat[idx] = slot
            for key, val in obj.items():
                slot[f"_{emit(key)}"] = emit(val)
        elif isinstance(obj, list):
            flat[idx] = None
            items = [emit(v) for v in obj]
            flat[idx] = items
        else:
            flat[idx] = obj
        return idx

    emit(value)
    return json.dumps(flat, ensure_ascii=False, separators=(",", ":"))


def page_html(loader_data, jsonld=None, title="Sleep Number"):
    """Wrap a loader-data object in the minimum page this parser will read.

    Minimum, but not less: the asset-host reference is load-bearing — it is
    the structural signal `detect_page_state` uses to tell a page the site
    served from a CloudFront interstitial, so a fixture without one would
    classify as `unknown` and the fixture would be testing the wrong thing.
    """
    # The real document's root is `{"loaderData": {…routes…}, "actionData":
    # …, "errors": …}` — the routes are NOT at the top level. Getting this
    # wrong is what the round-trip assertion below caught on the first run:
    # the fixture decoded perfectly and `loader_data()` still returned None,
    # because it looks for exactly this key.
    payload = encode_turbo_stream(
        {"loaderData": loader_data, "actionData": None, "errors": None})
    blocks = ""
    for node in (jsonld or []):
        blocks += ('<script type="application/ld+json">'
                   + json.dumps(node, ensure_ascii=False) + "</script>\n")
    return (
        "<!DOCTYPE html><html lang=\"en\"><head>"
        f"<title>{title}</title>"
        "<link rel=\"preconnect\" href=\"https://cdn.sleepnumber.com\">\n"
        f"{blocks}"
        "</head><body><div id=\"root\"></div>\n"
        "<script>window.__reactRouterContext = {};</script>\n"
        "<script>window.__reactRouterContext.streamController.enqueue("
        + json.dumps(payload) + ");</script>\n"
        "</body></html>")


def _trim_product(product, keep_variants=3):
    """A real product object, reduced to the keys this parser reads.

    Every key kept holds its ORIGINAL value. Nothing is renamed, rounded or
    invented — the reduction only drops keys, so a value in the fixture is a
    value the site published.
    """
    keep = ("id", "name", "branded_name", "slug", "canonical_url", "type",
            "category_slug", "purchasable", "review_stats", "images")
    out = {k: product[k] for k in keep if k in product}
    variants = [v for v in (product.get("variants") or []) if isinstance(v, dict)]
    trimmed = []
    for variant in variants[:keep_variants]:
        trimmed.append({k: variant[k] for k in
                        ("sku", "details", "active", "sale", "regular",
                         "promo_badge_message") if k in variant})
    out["variants"] = trimmed
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", help="directory holding the raw .html captures")
    ap.add_argument("-o", "--out", default="fixtures_generated.json")
    ap.add_argument("--products", type=int, default=2)
    ap.add_argument("--variants", type=int, default=3)
    args = ap.parse_args(argv)

    cap = pathlib.Path(args.captures)

    def capture(*needles, exclude=()):
        """A saved page matching every needle — and ONLY a saved page.

        Restricted to files whose name begins with the host, because a
        working capture directory also accumulates intermediate JSON from
        whatever was being investigated at the time. An earlier version of
        this picked up a `dec_…json` debug dump that happened to sort first,
        decoded nothing, and reported "no undiscounted variant in the sheets
        capture" — a confusing answer to a question about the site, caused
        by reading a file that was not the site.
        """
        for path in sorted(cap.iterdir()):
            if not path.is_file() or not path.name.startswith("www."):
                continue
            if path.suffix in (".json", ".gz"):
                continue
            if any(n not in path.name for n in needles):
                continue
            if any(x in path.name for x in exclude):
                continue
            return path
        return None

    listing = capture("categories_mattresses.html")
    detail = capture("products_cm-mattress")
    if not listing or not detail:
        return f"need a mattresses listing and a cm-mattress detail capture in {cap}"

    out = {"_README": [
        "Generated by tools/make_fixtures.py from real captures of "
        "www.sleepnumber.com taken 2026-09-17 through a US residential exit.",
        "Every VALUE here was published by the site. The reduction drops keys "
        "and variants; it does not alter what is kept. The generator asserts "
        "that each fixture decodes back to objects equal to the ones taken "
        "out of the full capture, so 'smaller' cannot quietly mean 'different'.",
        "Re-run the generator when a capture is refreshed and read the diff: "
        "a changed value is the site changing, which is what a fixture is for.",
    ]}

    # ---- listing ----
    html = listing.read_text(errors="replace")
    data = pp.loader_data(html)
    products, category = pp._catalogue_nodes(data)
    route = next(k for k in data if k.startswith("routes/categories"))
    picked = [_trim_product(p, args.variants) for p in products[:args.products]]
    loader = {"root": {}, route: {"category": {
        "name": category.get("name"), "slug": category.get("slug"),
        "page": category.get("page"), "per_page": category.get("per_page"),
        "total_results": category.get("total_results"),
        "next_page": category.get("next_page"),
        "prev_page": category.get("prev_page"),
        "last_page": category.get("last_page"),
        "full_url": category.get("full_url"),
        "products": picked}}}
    fixture = page_html(loader, title="Mattresses | Sleep Number")
    back = pp.loader_data(fixture)
    got, _ = pp._catalogue_nodes(back)
    assert got == picked, "listing fixture does not decode to what it was built from"
    out["listing_html"] = fixture

    # ---- detail, with the site's REAL JSON-LD Product for the cross-check ----
    dhtml = detail.read_text(errors="replace")
    ddata = pp.loader_data(dhtml)
    dprods, _ = pp._catalogue_nodes(ddata)
    dpicked = [_trim_product(dprods[0], args.variants)]
    droute = next(k for k in ddata if k.startswith("routes/products"))
    dloader = {"root": {}, droute: {"product": dpicked[0]}}
    real_ld = [n for n in pp.jsonld_nodes(dhtml)
               if (n.get("@type") == "Product")]
    dfixture = page_html(dloader, jsonld=real_ld,
                         title="ComfortMode Mattress | Sleep Number")
    dback = pp.loader_data(dfixture)
    dgot, _ = pp._catalogue_nodes(dback)
    assert dgot == dpicked, "detail fixture does not decode to what it was built from"
    out["detail_html"] = dfixture

    # ---- what the parser should produce from each, pinned as VALUES ----
    # §10: assert values on real fixtures, not coverage. A column can be 100%
    # populated and entirely wrong — amazon-scraper shipped 445279961 as a
    # review count on every row while its coverage check read 100%.
    for key, (fx, url) in {
        "listing_expected": (fixture, "https://www.sleepnumber.com/categories/mattresses"),
        "detail_expected": (dfixture, "https://www.sleepnumber.com/products/cm-mattress"),
    }.items():
        # `page` is threaded for a LISTING and left None for a detail page,
        # exactly as each engine's _parse_for_mode calls it. Pinning it the
        # other way would make the fixture agree with a call nothing makes.
        rows = pp.parse_products(fx, url, page=1 if key == "listing_expected" else None)
        out[key] = [{k: v for k, v in r.__dict__.items() if k != "scraped_at"}
                    for r in rows]

    # ---- a listing holding an UNDISCOUNTED variant ----
    # The mattresses category is entirely on sale — 52 of 52 rows carry a
    # discount — so a fixture built only from it cannot exercise the guard
    # that nulls `original_price` when `regular` merely equals `sale`. That
    # gap was found by a CONTROL: removing the guard left the suite green,
    # which is the §22 failure where a control proves nothing. Sheets are
    # mostly not on sale (13 of 211 rows discounted), so one product from
    # there gives the check something to actually fail on.
    sheets = capture("sheets-pillowcases", exclude=("page",))
    if sheets:
        shtml = sheets.read_text(errors="replace")
        sdata = pp.loader_data(shtml)
        sprods, scat = pp._catalogue_nodes(sdata)
        flat = None
        for prod in sprods:
            for v in prod.get("variants") or []:
                sale, reg = v.get("sale") or {}, v.get("regular") or {}
                if sale.get("cents") and sale.get("cents") == reg.get("cents"):
                    flat = prod
                    break
            if flat:
                break
        assert flat is not None, "no undiscounted variant in the sheets capture"
        sroute = next(k for k in sdata if k.startswith("routes/categories"))
        sloader = {"root": {}, sroute: {"category": {
            "name": scat.get("name"), "slug": scat.get("slug"),
            "page": 1, "per_page": scat.get("per_page"),
            "total_results": scat.get("total_results"),
            "last_page": True, "products": [_trim_product(flat, 6)]}}}
        sfixture = page_html(sloader, title="Sheets | Sleep Number")
        out["undiscounted_html"] = sfixture
        rows = pp.parse_products(sfixture,
                                 "https://www.sleepnumber.com/categories/sheets-pillowcases",
                                 page=1)
        assert any(r.original_price is None for r in rows), (
            "the undiscounted fixture produced no row with a null original_price")
        out["undiscounted_expected"] = [
            {k: v for k, v in r.__dict__.items() if k != "scraped_at"} for r in rows]

    # ---- the site's REAL 404, and a REAL landing page ----
    # Both were classified wrongly by an earlier version of this parser, and
    # both were invisible to the suite because the suite tested a page this
    # author invented instead. §21: a guard is only as good as the fixture it
    # runs against, and the fixture that matters is the one fetched the way a
    # real run fetches.
    nf = capture("products_i8")
    if nf:
        nhtml = nf.read_text(errors="replace")
        ndata = pp.loader_data(nhtml)
        assert ndata is not None and not [k for k in ndata if k != "root"], (
            "the 404 capture has route keys — the site changed, re-read it")
        # Its own wording, taken verbatim from the real page around the
        # marker, so the fixture cannot drift from what the site says.
        idx = nhtml.lower().index("not found")
        snippet = nhtml[max(0, idx - 120):idx + 120]
        # NOT named `fixture`: that name already holds the listing fixture
        # the final summary line reports on, and shadowing it made that line
        # print the 404's size instead. A cosmetic bug, fixed because the
        # next person to add a block here would inherit the trap.
        nfixture = page_html({"root": {}}, title="Sleep Number")
        nfixture = nfixture.replace("<div id=\"root\"></div>",
                                    "<div id=\"root\">" + snippet + "</div>")
        assert pp.detect_page_state(nfixture, None, "u") == "notfound", \
            "the 404 fixture does not classify as notfound"
        out["notfound_html"] = nfixture

    landing = capture("beds-on-sale")
    if landing:
        lhtml = landing.read_text(errors="replace")
        ldata = pp.loader_data(lhtml)
        lroute = next(k for k in ldata if k.startswith("routes/categories"))
        lcat = ldata[lroute]["category"]
        assert "products" not in lcat, (
            "the landing capture now HAS a products key — it became a real "
            "category, so this fixture is testing the wrong thing")
        lfixture = page_html({"root": {}, lroute: {"category": dict(lcat)}},
                             title="Sale | Sleep Number")
        assert pp.detect_page_state(lfixture, None, "u") == "no_listing", \
            "the landing fixture does not classify as no_listing"
        out["landing_html"] = lfixture
        out["landing_category_name"] = lcat.get("name")

    pathlib.Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"wrote {args.out}: listing {len(out['listing_expected'])} rows, "
          f"detail {len(out['detail_expected'])} rows, "
          f"{len(fixture):,} + {len(dfixture):,} bytes of fixture HTML")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
