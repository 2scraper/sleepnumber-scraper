# Troubleshooting

Ordered by how often each thing actually happens, not by severity.

## Every page comes back exit 3 (blocked)

Check `--dump-html PATH` first — it writes the exact bytes the parser was
given, whether the run succeeded or not, so you can see what actually
arrived instead of guessing.

Whether transfermarkt.com treats a datacentre or VPN address differently
from a residential one is **genuinely unmeasured for v0.1** — see README's
"Known limitations". This repo's own build environment could not complete a
live browser connection to the site at all to check (the failure traced back
to the *build sandbox's own* egress allowlist, not the site), and a single
**proxied** fetch of each page kind this repo covers came back clean with no
challenge markers. So a red exit 3 from a plain CI runner or a VPS is itself
useful information, not a known-and-expected outcome the way it is for this
family's e-commerce members. Try again with `--proxy` set to a residential
exit; if that clears it, please report back (an issue, or a note in a PR) —
that measurement belongs in the README for the next person.

If a proxy does not help, check `detect_bot_challenge`'s markers
(`product_parser.BOT_CHALLENGE_MARKERS`) against the dumped HTML — they
cover Cloudflare, DataDome, PerimeterX/HUMAN, reCAPTCHA and hCaptcha, but
were not measured against a real challenge from this site, so a genuinely
new one could go unrecognised (falling through to the generic `"blocked"`
bucket via a non-2xx status).

## Exit 4 (zero rows) on a URL that plainly has data in a browser

**`--mode club-squad`** — check the club id and `--season`. A season with no
squad data yet, or a wrong id, answers 200 with a page that has no table on
it, which is a correct "empty" answer, not a bug.

**`--mode market-values` / `--mode transfers`** past page 1 — you may be one
page past the end of the ranking/listing. Both modes stop and report a
`no_new_products`/exhausted result rather than erroring, which is a
*complete* answer, not a truncated one — check `stop_reason` in the
`.meta.json` sidecar.

**`--mode player`** — the header selector
(`header.data-header[itemtype="https://schema.org/Person"]`) did not match.
Either the player id is wrong, or the profile page's header markup has
moved; `--dump-html` tells you which.

## The row count is right but a column is empty

**`price`/`currency` is `null` on a real player.** For `market-values` and
`club-squad`, that is very likely a genuine "no market value published"
player (a young academy player, a very old veteran, a free agent) —
Manchester City's 2025/26 squad measured 36 of 43 priced, and the 7 nulls
were real. Check the player's own profile page before assuming a bug:
`"-"` in the market-value cell parses to `None`, never `0`, on purpose (see
`parse_market_value`'s docstring).

**`joined_date`/`contract_until`/`agent` are all `null` on a `--mode
player` row.** This project found and fixed exactly this bug once — the
profile header's `data-header__label` class appears on both `<li>` and
`<span>` tags, and an earlier version of `_label_map` matched only `li`. If
you see it again after a site change, check which tag the label moved to.

**`from_club`/`to_club` look wrong or doubled on a transfer.** See
`_club_cell_with_league`'s docstring — a transfer row's club cell can hold
two `<a>` tags (an image-only wrapper and the real text-bearing anchor), and
reading the wrong one produces exactly this kind of garbled name. Also
possible: the site's own `title` attribute is genuinely doubled for the
"Without Club" free-agent placeholder, which `_detitle()` exists to catch.

**`fee_type` is `"unknown"` on a lot of rows.** That is the fallback for a
Fee cell this parser does not recognise — check whether the site added a
new fee-presentation format beyond disclosed/loan/free/undisclosed ("?").

## `--mode market-values --pages N` reports `partial` (exit 6) past page 1

Read this together with README's "the gallery pagination trap" section
first — it may not be a bug at all. This mode is USUALLY independently
addressable (a plain `?page=N`, same as transfers — confirmed on 28 of 29
consecutive fetches in the 2026-09-10 re-measurement), but on the rare run
where page 1's own next-link disagrees with that construction (the
`galerie/{uuid}` shape, whose stateless page-2 fetch replays page 1), this
scraper correctly falls back to chaining the site's own pagination link one
page at a time inside a single browser session. If that link is ever
missing, stale, or the fallback runs out mid-chain, the honest answer is a
partial result with a `stop_reason` naming why — not a silently duplicated
or skipped page. If you see `partial` on EVERY run rather than occasionally,
that is worth reporting — it would mean the intermittent shape has become
the norm, which this project has not seen but has not ruled out either.

## "Blocked" on a page that clearly rendered

The Scraping Browser API's auto-solve extension injects its own scripts
into every page it loads, and those can contain a vendor string
(`cf-turnstile` is the one this family has hit before) that would otherwise
look like a real challenge marker on an otherwise clean page. This repo
strips `chrome-extension://`/`moz-extension://`-sourced script tags before
matching (see `product_parser._EXTENSION_TAG_RE`). If you see this anyway,
the stripping pattern may need widening for a new extension version.

## HTTP 500 from the Scraping Browser endpoint

A profile (`pid`) allows one live connection at a time. Another run is
probably still holding it — wait for it to finish, or use a different `pid`.

## `ERR_TUNNEL_CONNECTION_FAILED` over `--cdp-endpoint`

The remote browser has no exit to tunnel through. Check the account's
`proxyMode` with `tools/browser_profile_client.py accounts` — `"none"` means
no proxy is attached, and the profile's `"inherit"` inherits that. This is
not a network problem on your machine, and not the site.

The CDP connection itself succeeding proves nothing here: authenticating to
the Scraping Browser and having an exit to browse through are two separate
things, which is why `Captcha.setAutoSolve enabled` can appear in the log
immediately before every navigation fails.

`use` refuses to write an endpoint for an account at `none` and says so.
Pick an account whose mode is `our_proxy`, or attach a proxy to this one.

## `401 deny_no_user` or `401 Wrong user name format`

The login in the connection string is malformed or stale. Fetch a fresh
`connectionUri` rather than editing the string:

```bash
python3 tools/browser_profile_client.py use --account-id N --write-env
```

Hand-assembling that login is what produces these. Measured 2026-09-17
against the live endpoint, three encodings of the same custom proxy in the
`-proxy-{base64url}` segment: padding stripped gave `401 deny_no_user`,
padding kept as `=` and padding as `%3D` both gave
`401 Wrong user name format`. Two distinct errors, and the last two agree,
so the complaint is about the username as a whole rather than the base64
inside it. The API builds a correct string itself; take that one.

## Selenium: `--cdp-endpoint` or `--proxy` does not work

Both are expected limits, not bugs — see README's Engines section.
chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
put credentials, and `--proxy-server=` accepts none either. Use
`playwright_scraper.py` or `puppeteer_scraper.py` for an authenticated
remote endpoint or an authenticated proxy.

## `pip check` complains after installing two engines

Expected — see requirements.txt. playwright and pyppeteer pin incompatible
`pyee` versions; pyppeteer and selenium want different `urllib3` ranges.
Install one engine per virtualenv if you need more than one.

## Something else

Run `python3 smoke_test.py` first — if it fails on a clean clone with no
`.env`, that is itself the bug and worth reporting as such. Otherwise open
an issue with the exact command (credentials replaced by `***`), the exit
code, and `--dump-html` output if the row count or a field looks wrong.
