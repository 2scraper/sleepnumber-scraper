#!/usr/bin/env python3
"""capture.py — fetch Sleep Number pages and report what a parser could read.

CLAUDE.md §15 step 2 says to capture the target page BEFORE writing any
parser, and to answer three questions from the dump rather than from
expectation: how many JSON-LD blocks are there, what does pagination look
like in the served markup, and does the URL carry the product id. §21 adds a
fourth that costs one request and has twice been the answer: what does the
site's own FRONT END call? A page that is gated does not mean its data is.

So this does both halves in one pass — it saves the bytes, and it prints the
counts. It exists as a repo tool rather than a notebook because every figure
the README quotes about this site has to arrive with the command that
reproduces it (§13); a number nobody can re-measure rots silently.

    python3 tools/capture.py --out captures/            # the default URL set
    python3 tools/capture.py --url https://www.sleepnumber.com/c/beds
    python3 tools/capture.py --direct                   # ignore any proxy

THE EXIT IT MEASURES
--------------------
By default this goes through SLEEPNUMBER_PROXY when .env sets one, because
"can this address reach the site" is a question about an EXIT. The proxy is
resolved by env_config.py — the same loader every engine uses, so precedence
is the documented one — and reaches requests through the `https_proxy`
ENVIRONMENT variable rather than a command-line flag, because `ps` reads
argv. Only a masked host and port are ever printed.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import env_config  # noqa: E402

try:
    import requests
except ImportError:
    sys.exit("pip install -r requirements.txt first (this needs `requests`)")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("pip install -r requirements.txt first (this needs `beautifulsoup4`)")


# A browser's header set. Not cover — the CloudFront refusal measured on this
# site ignores headers entirely (see README "Access") — but a request that
# looks like a browser is the one whose RESPONSE is worth comparing against a
# browser's, which is the point of capturing at all.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

# Starting points, deliberately spanning more than one page KIND: §15 says one
# dump teaches you one locale, and §20 adds that a DETAIL page may publish a
# different JSON-LD @type than the listing that links to it — porting a
# listing parser to a detail page then returns zero rows in silence.
DEFAULT_URLS = [
    # Listings. `/categories/...` is what the site's own navigation links to;
    # `/collections/...` also resolves and is the shape a headless commerce
    # front end publishes. Which of the two is canonical is a question for the
    # first capture that gets a 200, not for a guess.
    "https://www.sleepnumber.com/categories/mattresses",
    "https://www.sleepnumber.com/categories/mattresses/king",
    "https://www.sleepnumber.com/categories/beds-on-sale",
    "https://www.sleepnumber.com/collections/mattresses-climate-collection",
    # Detail pages — a DIFFERENT page kind on purpose. §20: a detail page may
    # publish a different JSON-LD @type than the listing that links to it, and
    # porting the listing parser to it returns zero rows in silence.
    "https://www.sleepnumber.com/products/i8",
    "https://www.sleepnumber.com/products/climate360-smart-bed",
    # A size variant. The size rides in the query string rather than the path,
    # so a "product" may be one row or one row per size — decided from a
    # capture, since it changes what `sku` has to mean.
    "https://www.sleepnumber.com/products/climate360-smart-bed?size=Queen",
    # Crawl surface: robots.txt names the sitemaps, and a sitemap is the
    # cheapest complete list of product URLs a site will ever hand you.
    "https://www.sleepnumber.com/robots.txt",
    "https://www.sleepnumber.com/sitemap.xml",
]

_CRED_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def mask(url):
    """Strip credentials from a URL, keeping host and port — which exit was
    used is the point of the log and is not the secret (CLAUDE.md §8)."""
    return _CRED_RE.sub("://***:***@", url or "")


def _jsonld_blocks(soup):
    """Every <script type="application/ld+json">, parsed, with its @type.

    The count may be ZERO and that is a real answer, not a failure: this
    family has measured zero on Amazon's search pages and on Transfermarkt
    entirely, and in that case the primary extraction path has to be the
    site's own data attribute instead (§4).
    """
    out = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            out.append(("<unparseable>", len(raw)))
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                out.append((f"<{type(node).__name__}>", len(raw)))
                continue
            t = node.get("@type") or ("@graph" if "@graph" in node else "<no @type>")
            if isinstance(t, list):
                t = "+".join(str(x) for x in t)
            out.append((str(t), len(raw)))
    return out


# What the front end might be calling. §21: grep a dump for these in the same
# breath as counting JSON-LD, then try the endpoint from a bare shell — on BBB
# that one request found an UNGATED JSON API behind a fully gated HTML site.
_STATE_HINTS = (
    "__NEXT_DATA__", "__PRELOADED_STATE__", "__NUXT__", "__APOLLO_STATE__",
    "window.__INITIAL_STATE__", "self.__next_f", "application/graphql",
)
_ENDPOINT_RE = re.compile(
    r"""["'](/(?:api|graphql|rest|bff|_next/data)/[^"'\s?#]{0,120})["']""")


def report(url, status, body, headers):
    print(f"\n=== {mask(url)}")
    print(f"    HTTP {status}   {len(body):,} bytes   "
          f"server={headers.get('server','?')}   "
          f"type={headers.get('content-type','?').split(';')[0]}")
    for h in ("x-amzn-waf-action", "x-cache", "cf-mitigated", "x-akamai-request-id"):
        if h in headers:
            print(f"    {h}: {headers[h]}")

    if not body.lstrip().lower().startswith(("<!doctype", "<html")):
        print("    (not HTML — no markup analysis)")
        return

    soup = BeautifulSoup(body, "html.parser")
    blocks = _jsonld_blocks(soup)
    print(f"    JSON-LD blocks: {len(blocks)}"
          + (f"  ->  {', '.join(t for t, _ in blocks)}" if blocks else
             "   (zero — the primary path must be the site's own data attribute)"))

    found = [h for h in _STATE_HINTS if h in body]
    print(f"    inlined state: {', '.join(found) if found else 'none of the known shapes'}")

    endpoints = sorted({m.group(1) for m in _ENDPOINT_RE.finditer(body)})
    if endpoints:
        print(f"    endpoint-shaped strings ({len(endpoints)}), first 15:")
        for e in endpoints[:15]:
            print(f"        {e}")
    else:
        print("    endpoint-shaped strings: none")

    # Which URL shapes repeat. An anchor for extraction is a URL PATTERN, never
    # a CSS class: classes are build-generated hashes that churn, URLs are a
    # contract with search engines (§4).
    paths = [a["href"] for a in soup.find_all("a", href=True)]
    seg = {}
    for p in paths:
        m = re.match(r"^(?:https?://[^/]+)?(/[^/?#]+)/", p)
        if m:
            seg[m.group(1)] = seg.get(m.group(1), 0) + 1
    top = sorted(seg.items(), key=lambda kv: -kv[1])[:12]
    print(f"    {len(paths)} links; top first-segments: "
          + ", ".join(f"{k}({v})" for k, v in top))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", action="append", default=[],
                    help="a URL to capture; repeatable. Defaults to a built-in set.")
    ap.add_argument("--out", default="captures",
                    help="directory to write the raw bytes into (default: captures)")
    ap.add_argument("--direct", action="store_true",
                    help="ignore SLEEPNUMBER_PROXY and measure this machine's own exit")
    ap.add_argument("--timeout", type=float, default=40.0)
    args = ap.parse_args(argv)

    env_config.load_env()
    proxy = None if args.direct else env_config.env_value("SLEEPNUMBER_PROXY")
    if proxy:
        # Through the environment, never argv — see the module docstring.
        os.environ["https_proxy"] = proxy
        os.environ["http_proxy"] = proxy
        print(f"exit: via proxy {mask(proxy)}")
    else:
        print("exit: this machine (no proxy configured)"
              + (" — --direct" if args.direct else ""))

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    urls = args.url or DEFAULT_URLS
    served = 0
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=args.timeout,
                             allow_redirects=True)
        except requests.RequestException as exc:
            # requests puts the FULL URL, query string included, into the text
            # of its exceptions — mask before printing (§8).
            print(f"\n=== {mask(url)}\n    FAILED: "
                  f"{_CRED_RE.sub('://***:***@', str(exc))}")
            continue

        name = re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[-1]).strip("_")[:120]
        path = outdir / f"{name}.html"
        path.write_bytes(r.content)
        report(url, r.status_code, r.text, {k.lower(): v for k, v in r.headers.items()})
        print(f"    saved: {path}")
        if r.status_code == 200:
            served += 1

    print(f"\n{served} of {len(urls)} URLs answered 200.")
    return 0 if served else 3


if __name__ == "__main__":
    raise SystemExit(main())
